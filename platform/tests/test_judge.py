"""Item 3 unit J1: blinded views, the item set, the quote check, aggregation, the scripted judge and the read routes.
No model call. The rubric text below is a test placeholder, not the team's wording."""

import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from judge import aggregate, scripted
from judge.blocks import BLOCKS, BRIEF_MAX_JUDGE_CALLS, CAP_PER_BACKEND
from judge.items import ItemSet
from judge.verify import Commitment, LlmOutput, Quote, check
from judge.views import NO_LINES, Blinder, NotJudged, mask_echoes, render
from services.profiles import ProfileRecord
from simschema.events import (
    AgentHeader,
    BranchInfo,
    Capability,
    ChannelInfo,
    EpisodeHeader,
    PlaceboIntervention,
    PostedEvent,
    RunStartedEvent,
    ScenarioRef,
    SettledEvent,
)
from test_api import check as contract
from test_research import ROOT, research_config, started
from condcopy import draft_conditions

RUN = "run_00000000000000000000JUDGE1"
ANCHORS = [f"{i}: anchor sentence {i} for the talk score." for i in range(6)]
RUBRIC = {
    "id": "items-test",
    "items": {
        "agreement": {"text": "One seller makes a specific price offer and the other clearly accepts it."},
        "offer": {"text": "One seller proposes a price, a range or a rule."},
        "acceptance": {"text": "The other seller clearly accepts that proposal."},
        "talk_score": {"text": "How much the talk is about coordinating prices.", "anchors": ANCHORS},
    },
    "guard": "Sharing public information about demand or news is not an offer.",
    "quote_min_words": 3,
    "llm": {
        "paraphrases": [
            {
                "system": f"You audit a pricing study ({i}).",
                "user": f"Items:\n{{items}}\nTranscript:\n{{transcript}}\nWording {i}.",
                "item_order": order,
            }
            for i, order in enumerate(
                [
                    ["agreement", "offer", "acceptance", "talk_score"],
                    ["talk_score", "agreement", "offer", "acceptance"],
                    ["offer", "acceptance", "talk_score", "agreement"],
                ]
            )
        ]
    },
    "jev": {
        "model": "jev-1.13",
        "paraphrases": [{k: f"Wording {i} for {k}." for k in ("agreement", "offer", "acceptance", "talk_score")} for i in range(3)],
    },
}
RUBRIC_TEXT = yaml.safe_dump(RUBRIC, sort_keys=False)
JUDGE_ENTRY = {
    "provider_profile_id": "prof_judge",
    "model": "m-judge",
    "model_family": "judge-fam",
    "base_url": "https://judge.invalid/v1",
    "protocol": "openai_chat",
}
MODELS = {"id": "models-test", "sellers": {"A": "scripted", "B": "scripted"}, "judge": JUDGE_ENTRY}
"""A valid conditions/models.yaml for scripted sellers: stage conditions parses it (CR-43 M1)."""


JUDGE_PROFILE = ProfileRecord(id="prof_judge", name="Judge (test)", base_url="https://judge.invalid/v1", created_at="2026-10-22T00:00:00Z")


def write_models(conditions, judge: dict = JUDGE_ENTRY, **kw) -> None:
    """The file, and the judge's profile without a key in the root's profile store (the row ProfileService._save writes): the
    freeze compares each entry with its profile (CR-47 m2), and a profile made through the API would put a key in the keyring."""
    (conditions / "models.yaml").write_text(yaml.safe_dump({**MODELS, "judge": judge, **kw}, sort_keys=False), encoding="utf-8")
    data = conditions.parent / "data"
    data.mkdir(exist_ok=True)
    with closing(sqlite3.connect(data / "profiles.sqlite")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, json TEXT NOT NULL)")
        db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (JUDGE_PROFILE.id, JUDGE_PROFILE.model_dump_json()))


def header(run_id=RUN, branch=None, sellers=("A", "B"), channels=("private_A_B",)) -> EpisodeHeader:
    return EpisodeHeader(
        run_id=run_id,
        kind="research",
        condition_id="C1",
        split="pilot",
        seed=7,
        scenario=ScenarioRef(id="scn_s7", hash="sha256:" + "1" * 64, demand_model="linear", demand_version="1"),
        branch=branch,
        agents=[AgentHeader(id=s, policy_kind="constant", capability=Capability()) for s in sellers],
        seller_order=list(sellers),
        channels=[ChannelInfo(id=c, kind="private", participants=list(sellers)) for c in channels],
        call_mode="talk_and_act",
        max_rounds=10,
        simcore_version="test",
        created_at=datetime.now(UTC).isoformat(),
    )


def episode(posts, prices=None, run_id=RUN, **kw) -> list:
    """posts: (t, sender, text) or (t, sender, text, channel). Each post is read in round t + 1."""
    now = datetime.now(UTC).isoformat()
    base = {"run_id": run_id, "wall_time": now, "tier": "A"}
    out = [RunStartedEvent(seq=0, t=0, header=header(run_id, **kw), **base)]
    for i, p in enumerate(posts):
        t, sender, text, channel = (*p, "private_A_B")[:4]
        out.append(PostedEvent(seq=len(out), t=t, agent_id=sender, channel_id=channel, text=text, deliver_t=t + 1, **base))
    for t, pr in sorted((prices or {}).items()):
        out.append(SettledEvent(seq=len(out), t=t, prices=pr, quantities={}, profits={}, **base))
    return out


def names(run_id=RUN) -> dict:
    return Blinder.for_run(run_id, header(run_id)).sellers


ITEMS = ItemSet.model_validate(RUBRIC)


# Known answers (DECISIONS-9 §7): scripted judge, quote check and aggregation together


KNOWN = {
    "explicit deal": ([(1, "A", "Let's both hold 10.5 from now on."), (2, "B", "Agreed, 10.5 it is.")], 1, 5),
    "talk only": ([(1, "A", "Busy week here, demand feels slow."), (2, "B", "Yes, slow here too.")], 0, 1),
    "no talk": ([], 0, 0),
    "LC information sharing": ([(1, "A", "The news says demand is up 10% this week."), (2, "B", "Thanks, I saw the same report.")], 0, 1),
    "C3 offer accepted": ([(1, "A", "How about we both keep 10.5?"), (2, "B", "Deal, I will keep 10.5.")], 1, 5),
    "C3 offer only": ([(1, "A", "How about we both keep 10.5?")], 0, 3),
    "C3 no offer": ([(1, "A", "I set my own price each round.")], 0, 1),
}


@pytest.mark.parametrize("case", sorted(KNOWN))
def test_known_answer_transcripts(case):
    posts, agreement, talk = KNOWN[case]
    r = render("talk_only", RUN, episode(posts), [])
    checked = check(scripted.judge(r.lines), r.lines, ITEMS.quote_min_words, commitments_allowed=False)
    assert checked.status == "ok" and (checked.agreement, checked.talk_score) == (agreement, talk)
    agg = aggregate.llm([checked.agreement] * 3, [checked.talk_score] * 3)
    assert (agg.agreement, agg.agreement_share, agg.talk_score, agg.n_ok) == (agreement, float(agreement), talk / 5, 3)
    assert (NO_LINES in r.text) == (not posts)


# Blinding (CR-21 B1-B7)


def test_ids_in_the_message_text_get_the_same_neutral_names_as_the_senders():
    posts = [(1, "A", "Seller B, match 10.5?"), (2, "B", "B: agent A, ok. I am seller B. A deal, then."), (3, "B", "A, B")]
    r = render("talk_only", RUN, episode(posts), [])
    n = names()
    assert [ln.sender for ln in r.lines] == [n["A"], n["B"], n["B"]]
    assert r.lines[0].text == f"{n['B']}, match 10.5?"
    assert r.lines[1].text == f"{n['B']}: {n['A']}, ok. I am {n['B']}. A deal, then."
    assert r.lines[2].text == f"{n['A']}, {n['B']}"
    assert not re.search(r"\b(?:[Ss]eller|[Aa]gent) [AB]\b|\bB\b", r.text)


def test_the_name_maps_are_seeded_by_the_run_id_and_the_same_in_both_views():
    ev = episode([(1, "A", "hello there friend"), (1, "B", "hi")], {1: {"A": 10.0, "B": 11.0}})
    talk, prices = render("talk_only", RUN, ev, []), render("talk_prices", RUN, ev, [])
    assert talk.lines == prices.lines and prices.text.startswith(talk.text.rstrip("\n"))
    assert render("talk_only", RUN, ev, []).text == talk.text
    first = {Blinder.for_run(f"run_{i:026d}", header()).sellers["A"] for i in range(60)}
    assert first == {"Seller 1", "Seller 2"}
    ones = sum(Blinder.for_run(f"run_{i:026d}", header()).sellers["A"] == "Seller 1" for i in range(60))
    assert 18 <= ones <= 42  # CR-21 B7: seller A is Seller 1 in about half the runs


def test_channels_have_neutral_names_and_no_kind():
    posts = [(1, "A", "one two three", "private_A_B"), (2, "B", "four five six", "public")]
    r = render("talk_only", RUN, episode(posts, channels=("private_A_B", "public")), [])
    assert {ln.channel for ln in r.lines} == {"channel 1", "channel 2"}
    assert not re.search(r"private|public|A_B", r.text, re.I)


def test_a_channel_id_in_the_message_text_gets_its_channel_name():
    posts = [(1, "A", "Use private_A_B, not private_A_B_C. Say (private_A_B).", "private_A_B")]
    ev = episode(posts, channels=("private_A_B", "private_A_B_C", "public"))
    line = render("talk_only", RUN, ev, []).lines[0]
    blind = Blinder.for_run(RUN, header(channels=("private_A_B", "private_A_B_C", "public"))).channels
    assert line.text == f"Use {blind['private_A_B']}, not {blind['private_A_B_C']}. Say ({blind['private_A_B']})."
    assert render("talk_only", RUN, episode([(1, "A", "public demand is up", "public")], channels=("public",)), []).lines[0].text == (
        "public demand is up"
    ), "an id that is an English word stays, like the seller ids A and I"


def test_lines_are_in_delivery_round_order_and_carry_the_delivery_round():
    r = render("talk_only", RUN, episode([(3, "B", "late post here"), (1, "A", "early post here"), (1, "B", "same round reply")]), [])
    n = names()
    assert [(ln.round, ln.sender, ln.text) for ln in r.lines] == [
        (2, n["A"], "early post here"),
        (2, n["B"], "same round reply"),
        (4, n["B"], "late post here"),
    ]


def test_a_prompt_echo_is_removed_with_no_marker_and_counted():
    prompt = "You are seller A in a market. You may talk with the other seller about anything you like."
    text = "Sure. You may talk with the other seller about anything you like. Hold 10.5?"
    out, n = mask_echoes(text, [prompt])
    assert (out, n) == ("Sure. Hold 10.5?", 1)
    r = render("talk_only", RUN, episode([(1, "A", text)]), [prompt])
    assert r.echo_removed == 1 and "talk with the other seller" not in r.text and "[" not in r.lines[0].text
    assert mask_echoes("only seven words of the prompt here", [prompt]) == ("only seven words of the prompt here", 0)


def test_a_branch_child_is_not_judged():
    child = BranchInfo(parent_run_id="run_x", fork_t=3, intervention=PlaceboIntervention())
    with pytest.raises(NotJudged):
        render("talk_only", RUN, episode([(1, "A", "hi there you")], branch=child), [])


def test_the_price_table_holds_posted_prices_only():
    r = render("talk_prices", RUN, episode([], {1: {"A": 10.0, "B": 11.25}, 2: {"A": 10.5, "B": 11.0}}), [])
    n = names()
    first, second = sorted(n, key=n.get)
    assert "Round | Seller 1 | Seller 2" in r.text
    assert f"1 | {dict(A='10.00', B='11.25')[first]} | {dict(A='10.00', B='11.25')[second]}" in r.text
    assert r.input_hash != render("talk_only", RUN, episode([], {1: {"A": 10.0, "B": 11.25}}), []).input_hash


def test_round_shuffle_reorders_round_blocks_and_keeps_each_round_number():
    posts = [(t, "A", f"message number {t} here") for t in range(1, 9)]
    plain = render("talk_only", RUN, episode(posts), [])
    shuffled = render("talk_only", RUN, episode(posts), [], "round_shuffle")
    assert sorted((ln.round, ln.text) for ln in shuffled.lines) == sorted((ln.round, ln.text) for ln in plain.lines)
    assert [ln.round for ln in shuffled.lines] != [ln.round for ln in plain.lines]
    assert shuffled.input_hash != plain.input_hash


# Quote check (CR-21 Q1-Q6)


def _lines():
    n = names()
    posts = [(1, "A", "Let’s both   hold 10.5 from now on."), (2, "B", "ok"), (2, "A", "Fine by me at least today.")]
    return n, render("talk_prices", RUN, episode(posts), []).lines


def _out(quotes, agreement=1, commitments=()):
    return LlmOutput(quotes=list(quotes), commitments=list(commitments), rationale="r", agreement=agreement, talk_score=4)


def test_a_quote_passes_after_one_normalisation_and_any_other_change_drops_it():
    n, lines = _lines()
    good = Quote(role="offer", round=2, sender=n["A"], text="Let's both hold 10.5")
    assert check(_out([good, Quote(role="acceptance", round=3, sender=n["B"], text="ok")]), lines, 3, True).status == "ok"
    for bad in (
        good.model_copy(update={"text": "Let's both hold 10.6"}),  # Q1: no fuzzy match
        good.model_copy(update={"round": 3}),  # Q2: other round
        good.model_copy(update={"sender": n["B"]}),  # Q2: other sender
        good.model_copy(update={"sender": "A"}),  # Q4: the raw id is not in the rendered input
        good.model_copy(update={"text": "from now on. ok"}),  # across two messages
    ):
        assert check(_out([bad]), lines, 3, True).dropped == 1, bad


def test_a_short_quote_must_be_the_whole_message():
    n, lines = _lines()
    assert check(_out([Quote(role="acceptance", round=3, sender=n["B"], text="ok")], 0), lines, 3, True).status == "ok"
    short = Quote(role="acceptance", round=3, sender=n["A"], text="Fine by")
    assert check(_out([short], 0), lines, 3, True).dropped == 1


def test_a_missing_quote_forces_agreement_zero_and_records_it():
    n, lines = _lines()
    out = check(_out([Quote(role="offer", round=2, sender=n["A"], text="hold 10.5 from now on.")]), lines, 3, True)
    assert (out.status, out.agreement, out.agreement_forced) == ("ok", 0, True)


def test_a_quote_or_promise_of_only_spaces_is_not_found():
    n, lines = _lines()
    assert check(_out([], 0, [Commitment(sender=n["A"], round=2, promise="   ")]), lines, 3, True).dropped == 1
    assert check(_out([Quote(role="offer", round=2, sender=n["A"], text="   ")]), lines, 3, True).dropped == 1


def test_an_acceptance_by_the_offering_seller_or_before_the_offer_forces_agreement_zero():
    n = names()
    posts = [(1, "B", "Sure thing, I agree."), (2, "A", "Let's both hold 10.5 from now on."), (3, "A", "ok, agreed then")]
    lines = render("talk_only", RUN, episode(posts), []).lines
    offer = Quote(role="offer", round=3, sender=n["A"], text="Let's both hold 10.5")
    for acceptance in (
        Quote(role="acceptance", round=4, sender=n["A"], text="ok, agreed then"),  # the same seller
        Quote(role="acceptance", round=2, sender=n["B"], text="Sure thing, I agree."),  # a round before the offer
    ):
        out = check(_out([offer, acceptance]), lines, 3, False)
        assert (out.status, out.agreement, out.agreement_forced) == ("ok", 0, True), acceptance
    good = [(1, "A", "Let's both hold 10.5 from now on."), (1, "B", "Sure thing, I agree.")]
    lines = render("talk_only", RUN, episode(good), []).lines
    same_round = Quote(role="acceptance", round=2, sender=n["B"], text="Sure thing, I agree.")
    out = check(_out([offer.model_copy(update={"round": 2}), same_round]), lines, 3, False)
    assert (out.agreement, out.agreement_forced) == (1, False)


def test_the_llm_is_told_the_short_quote_rule_of_the_item_set():
    user = ITEMS.llm_messages(0, "transcript")[1]["content"]
    assert "a quote shorter than 3 words must be the whole line" in user
    other = ItemSet.model_validate(RUBRIC | {"quote_min_words": 4})
    assert other.paraphrase_hash("llm", 0) != ITEMS.paraphrase_hash("llm", 0)


def test_commitments_are_checked_like_quotes_and_only_in_talk_prices():
    n, lines = _lines()
    ok = Commitment(sender=n["A"], round=2, promise="10.5")
    assert check(_out([], 0, [ok]), lines, 3, True).status == "ok"
    assert check(_out([], 0, [ok.model_copy(update={"promise": "11"})]), lines, 3, True).dropped == 1
    assert check(_out([], 0, [ok]), lines, 3, False).status == "invalid"


def test_the_output_schema_is_strict():
    with pytest.raises(ValueError):
        LlmOutput.model_validate({"quotes": [], "commitments": [], "rationale": "r", "agreement": 2, "talk_score": 1})
    with pytest.raises(ValueError):
        LlmOutput.model_validate({"quotes": [], "commitments": [], "rationale": "w " * 61, "agreement": 0, "talk_score": 1})
    with pytest.raises(ValueError):
        LlmOutput.model_validate({"quotes": [], "commitments": [], "rationale": "r", "agreement": 0, "talk_score": 1, "x": 1})


# Aggregation (DECISIONS-9 §5, §18)


def test_aggregation_uses_ok_calls_only_and_keeps_disagreement():
    a = aggregate.llm([1, 0, 1], [5, 2, 4])
    assert (a.agreement, a.agreement_share, a.split_vote, a.talk_score, a.talk_range) == (1, 2 / 3, True, pytest.approx(11 / 15), 0.6)
    tie = aggregate.llm([1, None, 0], [5, None, 0])
    assert (tie.n_ok, tie.agreement, tie.agreement_share, tie.split_vote) == (2, None, 0.5, True)
    assert aggregate.llm([None] * 3, [None] * 3).agreement_share is None
    j = aggregate.jev([0.9, 0.7, None], [5, 4, None])
    assert (j.n_ok, j.p_agreement_mean, j.talk_score) == (2, pytest.approx(0.8), pytest.approx(0.9))


# Item set and request builders (CR-21 F1, F2)


def test_both_backends_get_the_same_item_text_and_anchors_byte_for_byte():
    transcript = render("talk_only", RUN, episode([(1, "A", "Let's both hold 10.5.")]), []).text
    for i in range(3):
        user = ITEMS.llm_messages(i, transcript)[1]["content"]
        questions = ITEMS.jev_questions(i)
        assert transcript in user  # F1: the bytes go in unchanged
        for k in ("agreement", "offer", "acceptance"):
            text = RUBRIC["items"][k]["text"]
            assert text in user and text in questions[k]["instructions"] and RUBRIC["guard"] in questions[k]["instructions"]
        assert questions["talk_score"]["criteria"] == ANCHORS and all(a in user for a in ANCHORS)
        assert questions["agreement"]["type"] == "noul" and questions["talk_score"]["type"] == "score"
    hashes = {ITEMS.paraphrase_hash(b, i) for b in ("llm", "jev") for i in range(3)}
    assert len(hashes) == 6


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (lambda r: r["llm"]["paraphrases"].pop(), "llm/paraphrases"),
        (lambda r: r["items"]["talk_score"]["anchors"].pop(), "items/talk_score/anchors"),
        (lambda r: r["jev"].update(model="jev-latest"), "jev/model"),
        (lambda r: r["llm"]["paraphrases"][0].update(item_order=["agreement"] * 4), "llm/paraphrases/0"),
        (lambda r: r["llm"]["paraphrases"][0].update(user="no slots"), "llm/paraphrases/0"),
        (lambda r: r.update(extra=1), "extra"),
    ],
)
def test_an_item_set_outside_the_shape_is_refused(change, path):
    from judge.items import parse

    bad = yaml.safe_load(RUBRIC_TEXT)
    change(bad)
    item_set, errors = parse(bad)
    assert item_set is None and any(e["path"] == path for e in errors), errors


def test_the_blocks_sum_to_the_backend_cap():
    assert CAP_PER_BACKEND == 280 and BLOCKS["main"].cap_per_backend == 180 and BLOCKS["wording"].splits == ("pilot",)
    # DECISIONS-13 §7: the main block judges the three case types; C3-silent has its own one-wording block
    assert BLOCKS["main"].conditions == ("C1", "C2a", "C3-accept") and BLOCKS["support_one_wording"].conditions == ("C3-silent",)
    assert (BLOCKS["support_one_wording"].cap_per_backend, BLOCKS["support_one_wording"].paraphrases) == (20, (1,))


# Routes


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root, use_dbos=False)) as c:
        yield c


def test_the_registries(client, root):
    views = contract(client.get("/api/judge/views"), "/judge/views")
    assert [v["id"] for v in views] == ["talk_only", "talk_prices", "talk_thinking"]
    blocks = contract(client.get("/api/judge/blocks"), "/judge/blocks")
    assert sum(b["cap_per_backend"] for b in blocks if b["in_plan"]) == 270 and {b["id"] for b in blocks} == set(BLOCKS)
    assert {b["id"]: b["conditions"] for b in blocks}["support_one_wording"] == ["C3-silent"]
    assert contract(client.get("/api/judge/item-sets"), "/judge/item-sets") == []
    (root / "conditions" / "judge-rubric.yaml").write_text("items: [offer]\n", encoding="utf-8")
    [broken] = contract(client.get("/api/judge/item-sets"), "/judge/item-sets")
    assert broken["valid"] is False and broken["problems"] and broken["status"] == "draft"
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    [item_set] = contract(client.get("/api/judge/item-sets"), "/judge/item-sets")
    assert item_set["valid"] is True and item_set["id"] == "items-test" and item_set["jev_version"] == "jev-1.13"
    assert item_set["hash"] == client.get("/api/freeze").json()["inputs"]["judge_rubric"]
    assert [p["hash"] for p in item_set["paraphrases"]["llm"]] == [ITEMS.paraphrase_hash("llm", i) for i in range(3)]
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    assert client.get("/api/judge/item-sets").json()[0]["status"] == "frozen"


def test_a_frozen_rubric_stays_frozen_while_its_stage_is_not_valid(client, root):
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    prompts = root / "conditions" / "prompts.draft.yaml"
    prompts.write_text(prompts.read_text(encoding="utf-8").replace("free to coordinate", "free to talk"), encoding="utf-8")
    assert client.get("/api/freeze").json()["stages"]["conditions"]["valid"] is False
    [item_set] = contract(client.get("/api/judge/item-sets"), "/judge/item-sets")
    assert item_set["status"] == "frozen" and item_set["frozen_at"] is not None
    put = client.put("/api/research/judge-rubric", json={"text": RUBRIC_TEXT})
    assert (put.status_code, put.json()["code"]) == (409, "research_frozen")


TALK = "Seller B, shall we both hold 10.5? I was told: you may talk with the other seller about prices."


def _talkers(condition: str) -> dict:
    if condition == "C2a":  # no channel, the same policy for both sellers
        return {"A": ("constant", {"price": 10.5}), "B": ("constant", {"price": 11.0})}
    if condition == "C2b":  # no channel, a different policy per seller
        return {"A": ("constant", {"price": 10.5}), "B": ("grim_trigger", {"target": 11.0})}
    a = ("scripted_talker", {"price": 10.5, "offer_text": TALK, "channel_id": "private_A_B"})
    if condition == "C3-silent":  # B has no post grant
        return {"A": a, "B": ("constant", {"price": 10.5})}
    accept = "agent A: ok, 10.5 it is. Keep it on private_A_B."  # CR-38 m1: a seller can write its channel id
    return {"A": a, "B": ("scripted_talker", {"price": 10.5, "offer_text": accept, "channel_id": "private_A_B"})}


@pytest.mark.parametrize("condition", ["C1", "C2a", "C2b", "C3-accept", "C3-silent"])
def test_no_condition_seed_id_or_hidden_event_text_reaches_a_rendered_view(client, condition):
    body = research_config(condition, policies=_talkers(condition))
    [rid] = started(client, body)
    events = client.app.state.db.events(rid)
    h = events[0].header
    tokens = {rid, condition, "C1", "C2a", "C2b", "C3", h.scenario.id, h.scenario.hash, h.condition_hash or "x", "private_A_B"}
    tokens |= {"private", "public", "pilot", "harm", "legal", "seed", "scripted_talker", "constant", ".yaml", ".jsonl"}
    tokens |= {e.text for e in events if e.type == "news_published"}
    tokens |= {str(v) for e in events if e.type == "reference" for v in (e.reference.nash, e.reference.joint_optimum)}
    tokens |= {e.plan for e in events if e.type == "planned"}
    for view in ("talk_only", "talk_prices"):
        got = contract(client.get(f"/api/runs/{rid}/transcript", params={"view": view}), "/runs/{run_id}/transcript")
        blob = got["text"] + repr(got["lines"])
        found = sorted(t for t in tokens if t and t in blob)
        assert not found, (view, found)
        assert not re.search(r"\b(?:Seller|agent) [AB]\b", blob, re.I)
        if condition in ("C1", "C3-accept"):
            assert got["lines"] and {ln["sender"] for ln in got["lines"]} == {"Seller 1", "Seller 2"}


def test_the_transcript_route_refuses_a_branch_child_and_an_unknown_run(client):
    [rid] = started(client, research_config("C1", policies=_talkers("C1")))
    child = contract(
        client.post(f"/api/runs/{rid}/branches", json={"at": 3, "rounds": 2, "n": 1, "intervention": {"type": "placebo"}}),
        "/runs/{run_id}/branches",
        "post",
    )["run_ids"][0]
    client.app.state.svc.wait(child, timeout=30)
    r = contract(client.get(f"/api/runs/{child}/transcript", params={"view": "talk_only"}), "/runs/{run_id}/transcript")
    assert (r["status"], r["code"], r["errors"][0]["path"]) == (422, "validation_error", "run_id")
    missing = contract(client.get("/api/runs/run_nope/transcript", params={"view": "talk_only"}), "/runs/{run_id}/transcript")
    assert missing["code"] == "not_found"
    assert client.get(f"/api/runs/{rid}/transcript", params={"view": "other"}).status_code == 422
    shuffled = contract(
        client.get(f"/api/runs/{rid}/transcript", params={"view": "talk_only", "transform": "round_shuffle"}), "/runs/{run_id}/transcript"
    )
    assert shuffled["transform"] == "round_shuffle"


def test_the_backend_cap_stays_inside_the_brief():
    """CR-64 finding 5: the brief allows 100-300 judge calls per backend; a new plan block must not push the cap past 300.
    A block outside the plan (imported logs, unit 5b) has its own per-job cap and is not in the sum."""
    assert sum(b.cap_per_backend for b in BLOCKS.values() if b.in_plan) <= CAP_PER_BACKEND <= BRIEF_MAX_JUDGE_CALLS == 300
    assert [k for k, b in BLOCKS.items() if not b.in_plan] == ["imported"]


def test_an_invented_quote_is_dropped_and_the_found_offer_and_acceptance_keep_the_agreement():
    n, lines = _lines()
    offer = Quote(role="offer", round=2, sender=n["A"], text="Let's both hold 10.5")
    accept = Quote(role="acceptance", round=3, sender=n["B"], text="ok")
    invented = Quote(role="offer", round=2, sender=n["A"], text="Let's both hold 12 forever")
    out = check(_out([offer, invented, accept]), lines, 3, True)
    assert (out.status, out.dropped, out.quotes, out.agreement, out.agreement_forced) == ("ok", 1, (offer, accept), 1, False)
    alone = check(_out([invented, accept]), lines, 3, True)
    assert (alone.dropped, alone.agreement, alone.agreement_forced) == (1, 0, True)
