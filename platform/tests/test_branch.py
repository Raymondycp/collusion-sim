import pytest

import simcore.scripted as scripted
from simcore.agents import Action, PolicyContext
from simcore.branch import BranchError, BranchRequest, plan_branches
from simcore.recorder import MemorySink, Recorder
from simcore.scripted import Scripted, make_scripted
from simkit import BR_TO_JOINT, JOINT, NASH, capability, of, prices, punishment_depth, run
from simschema.events import AblationIntervention, PlaceboIntervention, ProbeIntervention

GRIM_VS_COOPERATOR = {"A": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}), "B": ("constant", {"price": JOINT})}
BR_VS_COOPERATOR = {"A": ("best_response", {}), "B": ("constant", {"price": JOINT})}
PROBE_B = ProbeIntervention(target="B")


def branch(parent, policies, request, child_ids=None):
    sink, _, sp, scenario, _ = parent
    children = plan_branches(
        sp, scenario, sink.snapshots, request, child_ids or [f"run_CHILD{i}" for i in range(request.n)], created_at="2026-10-21T01:00:00Z"
    )
    market = scenario.market.build()
    out = []
    for child in children:
        built = {
            s: make_scripted(k, PolicyContext(seller_id=s, sellers=tuple(sorted(policies)), market=market, params=scenario.params), **p)
            for s, (k, p) in policies.items()
        }
        csink = MemorySink()
        child.run(scenario, built, Recorder(child.spec.run_id, [csink], now=lambda: "2026-10-21T01:00:00.000Z")).execute()
        out.append(csink)
    return out


def test_probe_on_a_grim_trigger_seller_gives_punishment_depth_above_zero():
    parent = run(GRIM_VS_COOPERATOR)
    (child,) = branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=5, intervention=PROBE_B))
    assert prices(child, "B")[10] == NASH
    assert punishment_depth(child, "A", range(11, 14)) == pytest.approx(
        2 * (BR_TO_JOINT - NASH)
    )  # BR in round 11 answers the probed Nash price
    assert prices(child, "A")[14] == JOINT


def test_probe_on_a_best_responder_gives_punishment_depth_zero():
    parent = run(BR_VS_COOPERATOR)
    (child,) = branch(parent, BR_VS_COOPERATOR, BranchRequest(at=10, rounds=5, intervention=PROBE_B))
    assert prices(child, "B")[10] == NASH
    assert punishment_depth(child, "A", range(11, 15)) == 0


def test_placebo_children_replay_the_parent_exactly_even_mid_punishment():
    deviates = {
        "A": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}),
        "B": ("price_schedule", {"prices": {10: NASH}, "default": JOINT}),
    }
    parent = run(deviates)
    children = branch(parent, deviates, BranchRequest(at=12, rounds=6, n=3, intervention=PlaceboIntervention()))
    parent_prices = {s: prices(parent[0], s) for s in "AB"}
    assert parent_prices["A"][12] == NASH and parent_prices["A"][14] == JOINT
    for i, child in enumerate(children):
        assert sorted(prices(child, "A")) == list(range(12, 18))
        for s in "AB":
            assert prices(child, s) == {t: parent_prices[s][t] for t in range(12, 18)}
        assert of(child, "run_started")[0].header.branch.sample_index == i + 1
        assert punishment_depth(child, "A", range(15, 18)) == 0


def test_a_child_is_a_framed_run_that_starts_at_the_fork():
    parent = run(GRIM_VS_COOPERATOR)
    (child,) = branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=5, intervention=PROBE_B))
    header = of(child, "run_started")[0].header
    assert header.run_id == "run_CHILD0" and header.branch.parent_run_id == "run_TEST" and header.branch.fork_t == 10
    assert header.max_rounds == 14 and header.seller_order == of(parent[0], "run_started")[0].header.seller_order
    assert [e.seq for e in child.events] == list(range(len(child.events)))
    assert [e.t for e in of(child, "round_started")] == list(range(10, 15))
    (iv,) = of(child, "intervened")
    assert iv.t == 10 and iv.effective_price == NASH
    assert of(child, "acted", agent_id="B", t=10)[0].forced


def test_a_probe_shows_as_set_by_system_in_the_targets_own_history_only():
    parent = run(GRIM_VS_COOPERATOR)
    (child,) = branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=3, intervention=PROBE_B))
    assert of(child, "observed", agent_id="B", t=11, purpose="act")[0].observation.own.last_price_set_by_system
    assert not of(child, "observed", agent_id="A", t=11, purpose="act")[0].observation.own.last_price_set_by_system


class Reader(Scripted):
    kind = "reader"

    def talk(self, obs, tools):
        tools.call("read_channel", channel_id="private_A_B")
        return []

    def act(self, obs, tools):
        return Action(price=NASH)


@pytest.fixture
def talkers():
    scripted.SCRIPTED["reader"] = Reader
    yield {
        "A": ("scripted_talker", {"price": NASH, "offer_text": "Let us both hold 10.5.", "channel_id": "private_A_B"}),
        "B": ("reader", {}),
    }
    del scripted.SCRIPTED["reader"]


def _reads(sink):
    return {e.t: e.result for e in of(sink, "tool_called", agent_id="B", tool="read_channel")}


@pytest.mark.parametrize("mode", ["clear", "shuffle"])
def test_ablation_masks_the_channel_from_the_fork_on(talkers, mode):
    caps = {s: capability(channels=["private_A_B"]) for s in "AB"}
    parent = run(talkers, caps=caps, private_pairs=True)
    (child,) = branch(
        parent, talkers, BranchRequest(at=8, rounds=4, intervention=AblationIntervention(channel_id="private_A_B", mode=mode))
    )
    before, after = _reads(parent[0]), _reads(child)
    assert all(len(before[t]) == t - 1 for t in range(8, 12))
    if mode == "clear":
        assert all(after[t] == [] for t in range(8, 12))
    else:
        assert all(sorted(m["text"] for m in after[t]) == sorted(m["text"] for m in before[t]) for t in range(8, 12))
    assert prices(child, "A") == {t: prices(parent[0], "A")[t] for t in range(8, 12)}


def test_a_fork_at_round_one_starts_from_an_empty_state():
    parent = run(GRIM_VS_COOPERATOR)
    (child,) = branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=1, rounds=3, intervention=PlaceboIntervention()))
    assert prices(child, "A") == {1: JOINT, 2: JOINT, 3: JOINT}


@pytest.mark.parametrize(
    "request_",
    [
        BranchRequest(at=31, rounds=2, intervention=PlaceboIntervention()),
        BranchRequest(at=5, rounds=2, intervention=ProbeIntervention(target="Z")),
        BranchRequest(at=5, rounds=2, intervention=ProbeIntervention(target="B", rounds=3)),
        BranchRequest(at=5, rounds=2, intervention=AblationIntervention(channel_id="public", mode="clear")),
    ],
)
def test_impossible_branches_are_refused(request_):
    parent = run(GRIM_VS_COOPERATOR)
    with pytest.raises(BranchError):
        branch(parent, GRIM_VS_COOPERATOR, request_)


def test_a_snapshot_that_does_not_match_its_hash_is_refused():
    parent = run(GRIM_VS_COOPERATOR)
    h, blob = parent[0].snapshots[9]
    parent[0].snapshots[9] = (h, blob.replace('"price":10.5', '"price":5.75', 1))
    with pytest.raises(BranchError, match="hash"):
        branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=2, intervention=PlaceboIntervention()))


def test_child_ids_must_match_the_sample_count():
    parent = run(GRIM_VS_COOPERATOR)
    with pytest.raises(BranchError):
        branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=2, n=3, intervention=PlaceboIntervention()), child_ids=["run_X"])


@pytest.mark.parametrize(
    ("intervention", "keeps"),
    [(PROBE_B, True), (PlaceboIntervention(), True), (AblationIntervention(channel_id="private_A_B", mode="clear"), False)],
)
def test_probe_and_placebo_children_keep_the_labels_and_ablation_children_carry_only_the_parents(intervention, keeps):
    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    parent = run(GRIM_VS_COOPERATOR, caps=caps, research=True, private_pairs=True)
    labels = parent[2].labels
    (child,) = branch(parent, GRIM_VS_COOPERATOR, BranchRequest(at=10, rounds=3, intervention=intervention))
    header = of(child, "run_started")[0].header
    if keeps:
        assert (header.labels, header.parent_labels, header.label_source) == (labels, None, "condition_config")
    else:
        assert (header.labels, header.parent_labels, header.label_source) == (None, labels, None)
        assert header.branch.intervention == intervention


def test_an_ablation_of_an_ablation_child_keeps_the_original_labels_as_provenance():
    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    sink, state, sp, scenario, built = run(GRIM_VS_COOPERATOR, caps=caps, research=True, private_pairs=True)
    ablate = BranchRequest(at=10, rounds=3, intervention=AblationIntervention(channel_id="private_A_B", mode="clear"))
    (child,) = plan_branches(sp, scenario, sink.snapshots, ablate, ["run_CHILD"], created_at="2026-10-21T01:00:00Z")
    (grandchild,) = plan_branches(child.spec, scenario, sink.snapshots, ablate, ["run_GRAND"], created_at="2026-10-21T01:00:00Z")
    assert (grandchild.spec.labels, grandchild.spec.parent_labels) == (None, sp.labels)
