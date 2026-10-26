from pydantic import BaseModel, ConfigDict, Field

from indicators.base import REGISTRY, Indicator, OutputSpec
from indicators.mathx import trailing_mean


def _ratio(p, nash, joint):
    return None if None in (p, nash, joint) or joint == nash else (p - nash) / (joint - nash)


class SmoothParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smooth: int = Field(1, ge=1, le=10, title="Smoothing window (rounds)")


@REGISTRY.register
class DeltaP(Indicator):
    id, version, name = "delta_p", "1", "Δp (normalised price index)"
    description = "(price − Nash) / (joint optimum − Nash), per seller and for the mean price, per round. 0 = Nash, 1 = joint optimum."
    inputs = ("price.*", "nash.*", "joint_optimum.*")
    data_layer, pane = "B", "separate"
    outputs = (OutputSpec(name="delta_p.{seller}", kind="line", unit="ratio"), OutputSpec(name="delta_p", kind="line", unit="ratio"))
    Params = SmoothParams

    def compute(self, s, p):
        cols = {k: {x: s[f"{k}.{x}"] for x in s.sellers} for k in ("price", "nash", "joint_optimum")}
        out = {
            f"delta_p.{x}": trailing_mean([_ratio(*v) for v in zip(cols["price"][x], cols["nash"][x], cols["joint_optimum"][x])], p.smooth)
            for x in s.sellers
        }

        def mean(kind, k):
            vals = [cols[kind][x][k] for x in s.sellers]
            return None if None in vals else sum(vals) / len(vals)

        out["delta_p"] = trailing_mean(
            [_ratio(mean("price", k), mean("nash", k), mean("joint_optimum", k)) for k in range(len(s.t))], p.smooth
        )
        return out


class BaselineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: float = Field(0.0, ge=-10, le=10, title="Single-agent baseline (price units)")


@REGISTRY.register
class BrGapNet(Indicator):
    id, version, name = "br_gap_net", "1", "Net BR gap"
    description = (
        "Price minus the best response to the rivals' prices of the round before, minus the single-agent baseline. "
        "Positive = the seller gives up short-run profit. Empty in round 1 only: a branch starts from the parent's prices of the "
        "round before its fork."
    )
    inputs = ("price.*", "best_response.*")
    data_layer, pane = "B", "separate"
    outputs = (OutputSpec(name="br_gap_net.{seller}", kind="histogram", unit="price"),)
    Params = BaselineParams

    def compute(self, s, p):
        return {
            f"br_gap_net.{x}": [
                None if br is None else price - br - p.baseline for price, br in zip(s[f"price.{x}"], s[f"best_response.{x}"])
            ]
            for x in s.sellers
        }
