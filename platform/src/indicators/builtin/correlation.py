from itertools import combinations, permutations

from pydantic import BaseModel, ConfigDict, Field

from indicators.base import REGISTRY, Indicator, OutputSpec
from indicators.mathx import diff, lag, rolling_corr


class WindowParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: int = Field(8, ge=3, le=30, title="Window (rounds)")


@REGISTRY.register
class CorrRaw(Indicator):
    id, version, name = "corr_raw", "1", "Correlation (raw)"
    description = "Rolling correlation of the round-to-round price changes of two sellers. A flat window gives 0."
    inputs = ("price.*",)
    data_layer, pane = "A", "separate"
    outputs = (OutputSpec(name="corr_raw.{pair}", kind="line", unit="r"),)
    Params = WindowParams

    def compute(self, s, p):
        d = {x: diff(s[f"price.{x}"]) for x in s.sellers}
        return {f"corr_raw.{a}_{b}": rolling_corr(d[a], d[b], p.window) for a, b in combinations(s.sellers, 2)}


@REGISTRY.register
class CorrCond(Indicator):
    id, version, name = "corr_cond", "1", "Correlation after common shocks"
    description = "As corr_raw, on the changes of (price − Nash), so a shared cost or demand shock does not count as dependence."
    inputs = ("price.*", "nash.*")
    data_layer, pane = "B", "separate"
    outputs = (OutputSpec(name="corr_cond.{pair}", kind="line", unit="r"),)
    Params = WindowParams

    def compute(self, s, p):
        d = {x: diff([None if q is None or n is None else q - n for q, n in zip(s[f"price.{x}"], s[f"nash.{x}"])]) for x in s.sellers}
        return {f"corr_cond.{a}_{b}": rolling_corr(d[a], d[b], p.window) for a, b in combinations(s.sellers, 2)}


@REGISTRY.register
class CorrLag(Indicator):
    id, version, name = "corr_lag", "1", "Lagged correlation"
    description = "corr_lag.A_B: rolling correlation of A's price change at t with B's price change at t − 1 (A follows B)."
    inputs = ("price.*",)
    data_layer, pane = "A", "separate"
    outputs = (OutputSpec(name="corr_lag.{pair}", kind="line", unit="r"),)
    Params = WindowParams

    def compute(self, s, p):
        d = {x: diff(s[f"price.{x}"]) for x in s.sellers}
        return {f"corr_lag.{a}_{b}": rolling_corr(d[a], lag(d[b]), p.window) for a, b in permutations(s.sellers, 2)}
