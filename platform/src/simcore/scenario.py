"""Frozen scenarios: a composition of a demand model with params, grid, sellers, horizon and events.

params(t) folds the active shocks over the base params; announced(t) feeds View.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from simcore.demand import DEMAND, DemandModelSpec
from simcore.market import Market, PriceGrid
from simcore.shocks import Shock, path_in_schema
from simschema.base import DemandParams, Frozen

SELLER_IDS = "ABCDEF"


class ScenarioHashError(ValueError):
    pass


class Event(Frozen):
    """news_only: news text only, no parameter change (a placebo or a rumour). param_shock: shocks to the demand params
    from round effect for length rounds, announced at round announce. kind defaults from whether there are shocks."""

    kind: Literal["news_only", "param_shock"]
    announce: int = Field(ge=1)
    effect: int | None = None
    length: int | None = Field(default=None, ge=1)
    shocks: list[Shock] = Field(default_factory=list)
    visible: list[str]
    news: str = ""
    news_source: Literal["human", "ai"] = "human"
    misleading: bool = Field(default=False, description="The news direction disagrees with the shocks on purpose")

    @model_validator(mode="before")
    @classmethod
    def _default_kind(cls, data: Any) -> Any:
        if isinstance(data, dict) and "kind" not in data:
            return {**data, "kind": "param_shock" if data.get("shocks") else "news_only"}
        return data

    @model_validator(mode="after")
    def _shape(self) -> "Event":
        if self.kind == "news_only" and (self.shocks or self.effect is not None or self.length is not None):
            raise ValueError("a news-only event has no shocks, effect or length")
        if self.kind == "param_shock" and (not self.shocks or self.effect is None or self.length is None):
            raise ValueError("a parameter shock needs shocks, effect and length")
        if self.kind == "param_shock" and self.effect < self.announce:
            raise ValueError("effect must not come before the announcement")
        return self

    def active(self, t: int) -> bool:
        return bool(self.shocks) and self.effect <= t < self.effect + self.length


class DemandRef(Frozen):
    """Registry id and version plus params. The params are stored normalised (defaults filled), so the hash is canonical."""

    id: str
    version: str | None = None
    params: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _resolve(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("params"), dict):
            return data
        try:
            spec = DEMAND.get(data["id"], data.get("version"))
        except KeyError as e:
            raise ValueError(str(e)) from None
        return {**data, "version": spec.version, "params": spec.parse(data["params"]).model_dump(mode="json")}

    @property
    def spec(self) -> DemandModelSpec:
        return DEMAND.get(self.id, self.version)


class MarketSpec(Frozen):
    demand: DemandRef
    grid: PriceGrid = PriceGrid()

    def build(self) -> Market:
        return Market(self.demand.spec.build(), self.grid)


class Horizon(Frozen):
    min_rounds: int = Field(default=30, ge=1)
    max_rounds: int = 40
    stop_prob: float = Field(default=0.1, ge=0, le=1)

    def stop_round(self, seed: int) -> int:
        """Same seed, same stop round, in every condition. Agents never see it."""
        rng = random.Random(f"horizon:{seed}")
        t = self.min_rounds
        while t < self.max_rounds and rng.random() >= self.stop_prob:
            t += 1
        return t


class Check(Frozen):
    id: str
    ok: bool
    message: str
    event_index: int | None = None


class CheckReport(Frozen):
    ok: bool
    checks: list[Check]


UP = re.compile(r"\b(ris\w*|rose|up|increas\w*|higher|jump\w*|surg\w*|soar\w*|climb\w*|grow\w*|boost\w*)\b", re.I)
DOWN = re.compile(r"\b(fell|fall\w*|drop\w*|down|decreas\w*|lower\w*|cut\w*|declin\w*|slump\w*|plung\w*|shrink\w*|eas\w*)\b", re.I)


def news_direction(text: str) -> int | None:
    up, down = bool(UP.search(text)), bool(DOWN.search(text))
    return None if up == down else (1 if up else -1)


class Scenario(Frozen):
    id: str
    name: str
    description: str = ""
    market: MarketSpec
    n_sellers: int = Field(default=2, ge=2, le=6)
    seller_ids: list[str] = Field(default_factory=list)
    horizon: Horizon = Horizon()
    quiet_window: tuple[int, int] = (18, 25)
    events: list[Event] = Field(default_factory=list)
    provenance: dict[str, Any] | None = Field(
        default=None, description="Where the scenario came from: generator id and version, inputs, seed; never a key"
    )
    status: Literal["draft", "frozen"] = "draft"
    hash: str | None = None
    approved_by: str | None = None
    frozen_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_sellers(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("seller_ids") and isinstance(n := data.get("n_sellers", 2), int):
            return {**data, "seller_ids": list(SELLER_IDS[:n])}
        return data

    @model_validator(mode="after")
    def _sellers(self) -> "Scenario":
        if len(self.seller_ids) != self.n_sellers or len(set(self.seller_ids)) != self.n_sellers:
            raise ValueError("seller_ids must list n_sellers distinct ids")
        return self

    def params(self, t: int) -> DemandParams:
        data = self.market.demand.params
        for e in self.events:
            if e.active(t):
                for shock in e.shocks:
                    data = shock.apply(data)
        return self.market.demand.spec.parse(data)

    def announced(self, t: int, seller: str) -> list[Event]:
        return [e for e in self.events if e.announce == t and seller in e.visible]

    def content(self) -> dict:
        return self.model_dump(mode="json", exclude={"status", "hash", "approved_by", "frozen_at"})

    def content_hash(self) -> str:
        return _hash(self.content())

    def check(self) -> CheckReport:
        checks = [
            self._check_shock_paths(),
            self._check_stability(),
            self._check_quiet_window(),
            *self._check_news(),
            self._check_visible(),
            self._check_horizon(),
        ]
        return CheckReport(ok=all(c.ok for c in checks), checks=checks)

    def frozen(self, approved_by: str) -> "Scenario":
        report = self.check()
        if not report.ok:
            raise ValueError(f"checks failed: {[c.message for c in report.checks if not c.ok]}")
        return self.model_copy(
            update={
                "status": "frozen",
                "hash": self.content_hash(),
                "approved_by": approved_by,
                "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

    def write(self, path: Path) -> None:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
        path.write_text(text, encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if is_legacy(data):
            data = upgrade_legacy(data)
        s = cls.model_validate(data)
        if s.status != "frozen" or s.hash is None:
            raise ScenarioHashError(f"{path} is not a frozen scenario")
        if s.hash != s.content_hash():
            raise ScenarioHashError(f"{path}: stored hash does not match the content")
        return s

    def _check_shock_paths(self) -> Check:
        schema = self.market.demand.spec.params_schema()
        for i, e in enumerate(self.events):
            for shock in e.shocks:
                if not path_in_schema(schema, shock.path):
                    return Check(
                        id="shock_paths",
                        ok=False,
                        event_index=i,
                        message=f"Event {i}: {shock.path} is not a number field of {self.market.demand.id}",
                    )
        return Check(id="shock_paths", ok=True, message=f"Every shock path is a number field of {self.market.demand.id}")

    def _check_stability(self) -> Check:
        spec, m = self.market.demand.spec, self.market.build()
        for t in range(1, self.horizon.max_rounds + 1):
            try:
                p = self.params(t)
            except ValueError as e:
                return Check(id="stability", ok=False, message=f"Round {t}: {e}")
            problems = spec.checks(p, self.seller_ids)
            if problems:
                return Check(id="stability", ok=False, message=f"Round {t}: " + "; ".join(problems))
            if spec.research_allowed:
                ref = m.reference(p, self.seller_ids)
                for name, prices in (("Nash", ref.nash_by_seller), ("joint optimum", ref.joint_by_seller)):
                    if not all(m.grid.min <= v <= m.grid.max for v in prices.values()):
                        return Check(id="stability", ok=False, message=f"Round {t}: {name} is outside the price grid")
        return Check(id="stability", ok=True, message=f"{spec.id} checks pass in every round; Nash and joint optimum inside the grid")

    def _check_quiet_window(self) -> Check:
        lo, hi = self.quiet_window
        for i, e in enumerate(self.events):
            changes = [e.announce] + ([e.effect, e.effect + e.length] if e.shocks else [])
            if any(lo <= t <= hi for t in changes):
                return Check(
                    id="quiet_window", ok=False, event_index=i, message=f"Event {i} is announced, starts or ends in rounds {lo}–{hi}"
                )
        return Check(id="quiet_window", ok=True, message=f"No announcement and no parameter change in rounds {lo}–{hi}")

    def _check_news(self) -> list[Check]:
        out = []
        base = self.market.demand.params
        for i, e in enumerate(self.events):
            if not e.news.strip():
                out.append(Check(id="news_direction", ok=False, event_index=i, message=f"Event {i} has no news text"))
            elif e.shocks and not e.misleading:
                signs = {shock.direction(base) for shock in e.shocks} - {0}
                real = signs.pop() if len(signs) == 1 else None
                said = news_direction(e.news)
                if real is None or said != real:
                    word = {None: "no clear direction", 1: "a rise", -1: "a fall"}[said]
                    change = "mixed directions" if real is None else ("a rise" if real > 0 else "a fall")
                    out.append(
                        Check(
                            id="news_direction",
                            ok=False,
                            event_index=i,
                            message=(
                                f"Event {i}: the shocks give {change}, but the news text gives {word} (set misleading: true if "
                                f"this is on purpose)"
                            ),
                        )
                    )
        return out or [
            Check(id="news_direction", ok=True, message="News direction agrees with every shock (or the event is marked misleading)")
        ]

    def _check_visible(self) -> Check:
        known = set(self.seller_ids)
        for i, e in enumerate(self.events):
            unknown = sorted(set(e.visible) - known)
            if unknown:
                return Check(id="visible_ids", ok=False, event_index=i, message=f"Event {i} names unknown sellers {unknown}")
        return Check(id="visible_ids", ok=True, message="All event sellers exist")

    def _check_horizon(self) -> Check:
        h, (_, hi) = self.horizon, self.quiet_window
        if h.min_rounds > h.max_rounds:
            return Check(id="horizon", ok=False, message="min_rounds is above max_rounds")
        if hi >= h.min_rounds:
            return Check(id="horizon", ok=False, message=f"Quiet window ends at {hi}, not before min_rounds {h.min_rounds}")
        return Check(id="horizon", ok=True, message="Horizon is valid")


def _hash(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_legacy(data: dict) -> bool:
    return "params" in data.get("market", {})


def upgrade_legacy(data: dict) -> dict:
    """Scenario files from before DECISIONS-6 (market.params, events with param/seller/delta) to the generic form.

    The stored hash is checked on the old content first. The upgraded file gets a new hash, and provenance names the old one.
    """
    old_hash = _hash({k: v for k, v in data.items() if k not in ("status", "hash", "approved_by", "frozen_at")})
    if data.get("status") == "frozen" and data.get("hash") != old_hash:
        raise ScenarioHashError(f"{data.get('id')}: stored hash does not match the legacy content")
    market = data["market"]
    params = dict(market["params"])
    params["c_offset"] = {s: v - params["c"] for s, v in params.pop("c_by_seller", {}).items()}
    events = []
    for e in data.get("events", []):
        e = dict(e)
        param, seller, delta = e.pop("param", None), e.pop("seller", None), e.pop("delta", None)
        if param is not None:
            e["shocks"] = [{"path": {"a": "/a", "c": "/c", "c_i": f"/c_offset/{seller}"}[param], "op": "add", "value": delta}]
        events.append(e)
    out = {
        **data,
        "market": {"demand": {"id": market.get("type", "linear"), "version": "1", "params": params}, "grid": market.get("grid", {})},
        "events": events,
        "provenance": {"converted_from_hash": old_hash},
    }
    if data.get("status") == "frozen":
        out["hash"] = Scenario.model_validate({**out, "status": "draft", "hash": None}).content_hash()
    return out
