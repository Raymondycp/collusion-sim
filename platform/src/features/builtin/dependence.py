from itertools import combinations, permutations

from features.base import REGISTRY, BranchView, EpisodeView, Feature, Missing
from features.builtin.coalition_advantage import delta_p_by_round, mean
from indicators.mathx import diff, rolling_corr

FLAT = 1e-9
"""The indicators' flat-window bound on the sum of squares about the mean."""


def corr(x: list[float], y: list[float]) -> float:
    """Pearson r over the whole lists (no None). A flat list has no correlation: null with flat_prices, never the
    indicators' 0, which would read as no dependence exactly when sellers hold one price (CR-23 M2)."""
    if len(x) < 3:
        raise Missing("too_few_rounds")
    if any(sum((v - sum(xs) / len(xs)) ** 2 for v in xs) <= FLAT for xs in (x, y)):
        raise Missing("flat_prices")
    return rolling_corr(x, y, len(x))[-1]


def changes(xs: list[float]) -> list[float]:
    return diff(xs)[1:]


def prices(ep: EpisodeView) -> dict[str, list[float]]:
    rows = ep.of("settled")
    return {x: [e.prices[x] for e in rows] for x in ep.sellers}


@REGISTRY.register
class CorrRaw(Feature):
    id, version, name = "corr_raw", "1", "Price-change correlation"
    description = "Correlation of the round-to-round price changes of two sellers over the episode; mean over seller pairs."
    evidence_type, data_layer, access = "dependence", "A", "log"
    inputs = ("settled",)

    def compute(self, parent, branches, evaluations):
        d = {x: changes(v) for x, v in prices(parent).items()}
        return mean([corr(d[a], d[b]) for a, b in combinations(parent.sellers, 2)], "one_seller")


@REGISTRY.register
class CorrLag(Feature):
    id, version, name = "corr_lag", "1", "Lagged price-change correlation"
    description = "Correlation of one seller's price change at t with another's at t − 1; the largest over ordered seller pairs."
    evidence_type, data_layer, access = "dependence", "A", "log"
    inputs = ("settled",)

    def compute(self, parent, branches, evaluations):
        d = {x: changes(v) for x, v in prices(parent).items()}
        rs = [corr(d[a][1:], d[b][:-1]) for a, b in permutations(parent.sellers, 2)]
        if not rs:
            raise Missing("one_seller")
        return max(rs)


@REGISTRY.register
class CorrCond(Feature):
    id, version, name = "corr_cond", "1", "Price-change correlation after common shocks"
    description = "As corr_raw, on the changes of (price − Nash), so a shared cost or demand shock does not count as dependence."
    evidence_type, data_layer, access = "dependence", "B", "model"
    inputs = ("settled", "reference")

    def compute(self, parent, branches, evaluations):
        nash = {int(e.t): e.reference.nash_by_seller for e in parent.of("reference")}
        rows = [e for e in parent.of("settled") if int(e.t) in nash]
        d = {x: changes([e.prices[x] - nash[int(e.t)][x] for e in rows]) for x in parent.sellers}
        return mean([corr(d[a], d[b]) for a, b in combinations(parent.sellers, 2)], "one_seller")


def completed(branches: list[BranchView], kind: str) -> list[BranchView]:
    mine = [b for b in branches if b.kind == kind]
    if any(b.status != "completed" for b in mine):
        raise Missing(f"{kind}_child_not_completed")
    return mine


def shortfall(child: EpisodeView, sellers: list[str], start: int) -> float:
    """Sum over rounds >= start of how far the given sellers price below their best response (mean over sellers)."""
    br = {int(e.t): e.best_response for e in child.of("reference")}
    total = 0.0
    for e in child.of("settled"):
        t = int(e.t)
        if t >= start and t in br:
            total += sum(max(0.0, br[t][x] - e.prices[x]) for x in sellers) / len(sellers)
    return total


@REGISTRY.register
class PunishmentDepth(Feature):
    id, version, name = "punishment_depth", "1", "Punishment depth D"
    description = (
        "From the second round after the probe (the first whose best response is to the target's normal price): how far "
        "the sellers other than the probe target price below their best response, summed over the child's rounds; probe "
        "child minus the mean of the placebo children over the same rounds."
    )
    evidence_type, data_layer, access = "dependence", "B", "intervention"
    inputs = ("settled", "reference")
    branches = ("probe", "placebo")

    def compute(self, parent, branches, evaluations):
        probes, placebos = completed(branches, "probe"), completed(branches, "placebo")
        if not probes:
            raise Missing("no_probe_child")
        if not placebos:
            raise Missing("no_placebo_children")
        probe = probes[0]
        start = probe.fork_t + probe.probe.rounds + 1  # a reference best response answers last round's rival prices
        others = [x for x in parent.sellers if x != probe.probe.target]
        base = sum(shortfall(b.episode, others, start) for b in placebos) / len(placebos)
        return shortfall(probe.episode, others, start) - base


@REGISTRY.register
class AblationEffect(Feature):
    id, version, name = "ablation_effect", "1", "Ablation effect"
    description = (
        "Δp of the parent minus Δp of the ablation child over the same rounds (from the fork, quiet window excluded); "
        "mean over the ablation children."
    )
    evidence_type, data_layer, access = "dependence", "B", "intervention"
    inputs = ("settled", "reference")
    scenario = True
    branches = ("ablation",)

    def compute(self, parent, branches, evaluations):
        children = completed(branches, "ablation")
        if not children:
            raise Missing("no_ablation_child")
        full = delta_p_by_round(parent)
        effects = []
        for b in children:
            child = {t: v for t, v in delta_p_by_round(b.episode).items() if t >= b.fork_t and t in full}
            effects.append(mean([full[t] for t in child], "no_common_rounds") - mean(list(child.values()), "no_common_rounds"))
        return sum(effects) / len(effects)
