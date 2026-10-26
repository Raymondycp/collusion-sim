"""BudgetGuard: one per run, shared by its LLM sellers. Checked before each uncached call.

A run of an experiment has a `batch` guard too (one per experiment, shared by all its runs): a call needs room in both.
A call with no price (cost None) is counted in `unpriced`; the USD cap cannot see it, the call and token caps still do.
A call holds its estimate from `reserve` until `spend` (or `cancel`), so calls in flight at the same time cannot pass a cap.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from simcore.agents import BudgetExceeded, RateWindowFull

WINDOW_S = 5 * 3600


@dataclass(frozen=True)
class Hold:
    usd: float
    tokens: int


@dataclass
class BudgetGuard:
    max_usd: float | None = None
    max_calls: int | None = None
    max_tokens: int | None = None
    spent_usd: float = 0.0
    calls: int = 0
    tokens: int = 0
    unpriced: int = 0
    held_usd: float = 0.0
    held_tokens: int = 0
    batch: "BudgetGuard | None" = None
    _warned: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self, estimate_usd: float | None, estimate_tokens: int) -> Hold:
        """Hold one call of at most estimate_usd (None: unpriced, no USD check) and estimate_tokens, or raise BudgetExceeded."""
        hold = Hold(estimate_usd or 0.0, estimate_tokens)
        with self._lock:
            if self.max_calls is not None and self.calls + 1 > self.max_calls:
                raise BudgetExceeded("max_calls")
            if self.max_tokens is not None and self.tokens + self.held_tokens + hold.tokens > self.max_tokens:
                raise BudgetExceeded("max_tokens")
            if self.max_usd is not None and estimate_usd is not None and self.spent_usd + self.held_usd + hold.usd > self.max_usd:
                raise BudgetExceeded("max_usd")
            if self.batch is not None:
                self.batch.reserve(estimate_usd, estimate_tokens)
            self.calls += 1
            self.held_usd += hold.usd
            self.held_tokens += hold.tokens
        return hold

    def room(self, calls: int, usd: float, tokens: int) -> bool:
        """True when `calls` more calls of `usd` and `tokens` in all fit every cap, with what is spent and held now. Not
        the batch guard: other runs share it (DECISIONS-22 entry 5.6, parallel phases)."""
        with self._lock:
            return (
                (self.max_calls is None or self.calls + calls <= self.max_calls)
                and (self.max_tokens is None or self.tokens + self.held_tokens + tokens <= self.max_tokens)
                and (self.max_usd is None or self.spent_usd + self.held_usd + usd <= self.max_usd)
            )

    def _drop(self, hold: Hold) -> None:
        with self._lock:
            self.held_usd = max(0.0, self.held_usd - hold.usd)
            self.held_tokens -= hold.tokens

    def cancel(self, hold: Hold) -> None:
        """Undo a reserve whose call was never sent."""
        if self.batch is not None:
            self.batch.cancel(hold)
        self._drop(hold)
        with self._lock:
            self.calls -= 1

    def charge(self, cost_usd: float | None, tokens: int) -> list[str]:
        """Count a call this run already paid for (a replayed reply of a round that was not stored). No cap check."""
        with self._lock:
            self.calls += 1
        if self.batch is not None:
            with self.batch._lock:
                self.batch.calls += 1
        return self.spend(cost_usd, tokens)

    def restore(self, spent_usd: float, tokens: int = 0, unpriced: int = 0) -> None:
        """Set what this run spent before a resume. Not forwarded: the batch guard counted it when it was spent."""
        with self._lock:
            self.spent_usd, self.tokens, self.unpriced = spent_usd, tokens, unpriced
            if self.max_usd and spent_usd >= 0.8 * self.max_usd:
                self._warned.add("p80")

    def spend(self, cost_usd: float | None, tokens: int, hold: Hold | None = None) -> list[str]:
        """Record a cost and free the call's hold. Returns the warning levels crossed now ("p80")."""
        if self.batch is not None:
            self.batch.spend(cost_usd, tokens, hold)
        if hold is not None:
            self._drop(hold)
        with self._lock:
            self.tokens += tokens
            if cost_usd is None:
                self.unpriced += 1
                return []
            self.spent_usd += cost_usd
            if self.max_usd and self.spent_usd >= 0.8 * self.max_usd and "p80" not in self._warned:
                self._warned.add("p80")
                return ["p80"]
            return []


class RateWindow:
    """Paid calls of one profile in the last 5 hours, for all runs of this data root on this machine.

    The provider's own quota may also count calls from other machines or tools; this window cannot see them.
    """

    def __init__(self, limit: int, stamps: Iterable[float] = (), clock: Callable[[], float] = time.time) -> None:
        self.limit, self.clock = limit, clock
        self._stamps: deque[float] = deque(sorted(stamps))
        self._lock = threading.Lock()

    def _drop_old(self, now: float) -> None:
        while self._stamps and self._stamps[0] <= now - WINDOW_S:
            self._stamps.popleft()

    def used(self) -> int:
        with self._lock:
            self._drop_old(self.clock())
            return len(self._stamps)

    def room(self) -> int:
        return max(0, self.limit - self.used())

    def take(self) -> None:
        """Count one paid call now, or raise RateWindowFull."""
        with self._lock:
            now = self.clock()
            self._drop_old(now)
            if len(self._stamps) >= self.limit:
                raise RateWindowFull("max_calls_per_5h")
            self._stamps.append(now)
