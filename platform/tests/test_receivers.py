"""posted.receivers (contract 0.21.0): the agents other than the sender that a message is delivered to."""

from pathlib import Path

import pytest

import simcore.scripted as scripted
from simcore.agents import Post
from simcore.branch import BranchRequest
from simcore.recorder import Recorder
from simcore.scripted import Scripted
from simkit import JOINT, capability, frozen_scenario, of, run
from simschema.events import SIM_EVENT, AblationIntervention
from test_branch import branch, talkers  # noqa: F401 (fixture)

STORED = Path(__file__).parent / "data" / "episode-pre-0.18.0.jsonl"  # an episode.v4, recorded at 8d9af94 (contract 0.17.0)

READS = {s: capability(channels=["private_A_B"]) for s in "AB"}
SENDER_ONLY = {"A": capability(channels=["private_A_B"]), "B": capability()}


def receivers(sink) -> set[tuple]:
    """Posts delivered within the run (a last-round post has none: test_a_post_in_the_last_round_has_no_receivers)."""
    end = of(sink, "run_ended")[0].t
    return {(e.agent_id, tuple(e.receivers)) for e in of(sink, "posted") if e.deliver_t <= end}


def test_a_reader_is_a_receiver_and_the_sender_is_not(talkers):  # noqa: F811
    sink = run(talkers, caps=READS, private_pairs=True)[0]
    assert of(sink, "posted") and receivers(sink) == {("A", ("B",))}


def test_a_participant_without_a_read_grant_is_not_a_receiver(talkers):  # noqa: F811
    sink = run(talkers, caps=SENDER_ONLY, private_pairs=True)[0]
    header = of(sink, "run_started")[0].header
    assert next(c for c in header.channels if c.id == "private_A_B").participants == ["A", "B"]
    assert of(sink, "posted") and receivers(sink) == {("A", ())}


def test_c3_isolation_hides_the_channel_from_act_calls_only_so_the_reader_still_receives(talkers):  # noqa: F811
    sink = run(talkers, caps=READS, private_pairs=True, c3_isolation=True)[0]
    assert of(sink, "posted") and receivers(sink) == {("A", ("B",))}


@pytest.mark.parametrize(("mode", "want"), [("clear", ()), ("shuffle", ("B",))])
def test_an_ablation_child_has_no_receivers_under_a_clear_mask_and_keeps_them_under_a_shuffle(talkers, mode, want):  # noqa: F811
    parent = run(talkers, caps=READS, private_pairs=True)
    (child,) = branch(
        parent, talkers, BranchRequest(at=8, rounds=4, intervention=AblationIntervention(channel_id="private_A_B", mode=mode))
    )
    posts = of(child, "posted")
    assert posts and all(e.deliver_t > 8 for e in posts) and receivers(child) == {("A", want)}


def test_an_episode_line_written_before_0_21_0_still_validates_with_null_receivers():
    line = {"seq": 3, "run_id": "run_X", "type": "posted", "t": 2, "wall_time": "2026-10-21T00:00:00.000Z", "tier": "A",
            "agent_id": "A", "channel_id": "private_A_B", "text": "10.5?", "deliver_t": 3}  # fmt: skip
    assert SIM_EVENT.validate_python(line).receivers is None


class RoundTalker(Scripted):
    """Posts another text each round, so a shuffled inbox shows a text other than the post's."""

    kind = "round_talker"

    def __init__(self, ctx, channel_id: str) -> None:
        super().__init__(ctx)
        self.channel_id = channel_id

    def talk(self, obs, tools):
        return [Post(channel_id=self.channel_id, text=f"round {obs.t}: hold 10.5")]

    def act(self, obs, tools):
        return self.price(JOINT)


@pytest.fixture
def round_talkers():
    scripted.SCRIPTED["round_talker"] = RoundTalker
    yield {"A": ("round_talker", {"channel_id": "private_A_B"}), "B": ("constant", {"price": JOINT})}
    del scripted.SCRIPTED["round_talker"]


def shown(sink, post, text=True) -> set[str]:
    """The agents whose observation in round deliver_t lists the post: the record of what each agent was shown."""
    want = (post.channel_id, post.agent_id, post.text)[: 3 if text else 2]
    return {
        o.agent_id
        for o in of(sink, "observed")
        if o.t == post.deliver_t and want in {(m.channel_id, m.sender, m.text)[: len(want)] for m in o.observation.new_messages}
    }


@pytest.mark.parametrize(
    ("caps", "kw"), [(READS, {}), (SENDER_ONLY, {}), (READS, {"c3_isolation": True})], ids=["reads", "sender_only", "c3"]
)
def test_receivers_are_the_agents_whose_observation_lists_the_post(round_talkers, caps, kw):
    sink = run(round_talkers, caps=caps, private_pairs=True, **kw)[0]
    posts = of(sink, "posted")
    assert posts and all(set(p.receivers) == shown(sink, p) for p in posts)


def test_a_post_in_the_last_round_has_no_receivers(round_talkers):
    sink = run(round_talkers, caps=READS, private_pairs=True)[0]
    *earlier, last = of(sink, "posted")
    assert last.t == of(sink, "run_ended")[0].t and last.receivers == [] and earlier
    assert all(p.receivers == ["B"] for p in earlier)


@pytest.mark.parametrize("mode", ["clear", "shuffle"])
def test_in_an_ablation_child_receivers_match_the_observations_and_a_shuffle_shows_another_text(round_talkers, mode):
    parent = run(round_talkers, caps=READS, private_pairs=True)
    request = BranchRequest(at=8, rounds=6, intervention=AblationIntervention(channel_id="private_A_B", mode=mode))
    (child,) = branch(parent, round_talkers, request)
    posts = of(child, "posted")
    assert posts and all(set(p.receivers) == shown(child, p, text=False) for p in posts)
    if mode == "shuffle":  # B gets a message from A, with another post's text: the observed event holds what B saw
        assert any(p.receivers == ["B"] and not shown(child, p) for p in posts)


@pytest.mark.parametrize(("readers", "want"), [("ABC", ["A", "C"]), ("AB", ["A"]), ("BC", ["C"])])
def test_with_three_sellers_receivers_keep_the_channel_order_without_the_sender_or_a_seller_without_a_grant(round_talkers, readers, want):
    policies = {"A": ("constant", {"price": JOINT}), "B": ("round_talker", {"channel_id": "public"}), "C": ("constant", {"price": JOINT})}
    caps = {s: capability(channels=["public"]) if s in readers else capability() for s in "ABC"}
    sink = run(policies, caps=caps, scenario=frozen_scenario(n=3), public=True)[0]
    *earlier, _ = of(sink, "posted")
    assert earlier and all(p.receivers == want and shown(sink, p) == set(want) for p in earlier)


def test_an_episode_stored_before_0_21_0_re_hashes_to_its_stored_episode_hash():
    *prior, ended = [SIM_EVENT.validate_json(line) for line in STORED.read_text(encoding="utf-8").splitlines()[1:]]
    assert [e for e in prior if e.type == "posted"] and all(e.receivers is None for e in prior if e.type == "posted")
    assert ended.type == "run_ended" and Recorder(ended.run_id, [], prior=prior).episode_hash == ended.episode_hash
