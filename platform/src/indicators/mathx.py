"""Window helpers. Each value at index k uses indices <= k only; None where the window is not complete."""

from __future__ import annotations

import math

from indicators.series import Value


def diff(xs: list[Value]) -> list[Value]:
    return [None] + [None if a is None or b is None else b - a for a, b in zip(xs, xs[1:])]


def lag(xs: list[Value]) -> list[Value]:
    return [None] + xs[:-1]


def trailing_mean(xs: list[Value], w: int) -> list[Value]:
    out = []
    for k in range(len(xs)):
        window = [x for x in xs[max(0, k - w + 1) : k + 1] if x is not None]
        out.append(sum(window) / len(window) if window else None)
    return out


def _corr(x: list[float], y: list[float]) -> float:
    """Pearson r. A flat window gives 0, as in the sandbox (site/sandbox/app.js rcorr)."""
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx > 1e-9 and syy > 1e-9 else 0.0


def rolling_corr(x: list[Value], y: list[Value], w: int) -> list[Value]:
    out: list[Value] = []
    for k in range(len(x)):
        xs, ys = x[max(0, k - w + 1) : k + 1], y[max(0, k - w + 1) : k + 1]
        complete = k >= w - 1 and None not in xs and None not in ys
        out.append(_corr(xs, ys) if complete else None)
    return out
