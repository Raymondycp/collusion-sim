import pytest
from pydantic import ValidationError

from simcore.builtin.generators.template import TEMPLATES
from simcore.generators import GENERATORS


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_gives_a_draft_that_passes_the_check(template):
    draft = GENERATORS.get("template").generate({"template": template, "n_sellers": 3})
    report = draft.check()
    assert report.ok, [c.message for c in report.checks if not c.ok]
    assert draft.status == "draft" and draft.hash is None and draft.seller_ids == ["A", "B", "C"]
    assert draft.provenance["generator_id"] == "template" and draft.provenance["inputs"]["template"] == template


def test_private_template_shows_demand_news_only_to_the_first_seller():
    draft = GENERATORS.get("template").generate({"template": "cost_shock_plus_private_demand_news"})
    assert [e.visible for e in draft.events] == [["A", "B"], ["A"]]
    assert draft.params(12).a == 12 and draft.params(8).cost_of("B") == 1.5


def test_random_is_deterministic_for_a_seed_and_differs_across_seeds():
    gen = GENERATORS.get("random")
    a, b, other = gen.generate({}, 7), gen.generate({}, 7), gen.generate({}, 8)
    assert a == b and a.content_hash() == b.content_hash()
    assert a.content_hash() != other.content_hash()


def test_random_drafts_stay_inside_the_bounds_and_pass_the_check():
    gen = GENERATORS.get("random")
    for seed in range(40):
        draft = gen.generate({"n_events": 3}, seed)
        assert draft.check().ok and not draft.provenance.get("check_failed")
        p = draft.market.demand.params
        assert 9 <= p["a"] <= 11 and 0.8 <= p["c"] <= 1.2
        for e in draft.events:
            for s in e.shocks:
                assert (s.path, s.op) in {("/c", "add"), ("/a", "mul")}
                assert (-0.6 <= s.value <= 0.6) if s.op == "add" else (0.85 <= s.value <= 1.15)


def test_random_reports_a_failed_check_instead_of_hiding_it():
    draft = GENERATORS.get("random").generate({"param_bounds": {"/d": (1.6, 1.9)}, "attempts": 3}, 1)
    assert draft.provenance["check_failed"] and not draft.check().ok


def test_bad_inputs_are_rejected():
    with pytest.raises(ValidationError):
        GENERATORS.get("template").generate({"template": "nope"})
    with pytest.raises(ValidationError):
        GENERATORS.get("random").generate({"min_rounds": 50, "max_rounds": 40})


def test_shock_news_sets_only_the_audience_of_every_event_and_nothing_else():
    """CR-12 m3: the draw is the same for public and private; private shows every event to the one informed seller."""
    shocks = [{"path": "/a", "op": "mul", "min": 0.9, "max": 1.1}, {"path": "/c", "op": "add", "min": -0.5, "max": 0.5}]
    gen, paths = GENERATORS.get("random"), set()
    for seed in range(30):
        base = {"shock_bounds": shocks, "placebo_share": 0.3}
        public = gen.generate({**base, "shock_news": "public"}, seed)
        private = gen.generate({**base, "shock_news": "private", "informed_seller": "B"}, seed)
        drawn = gen.generate(base, seed)
        for a, b, c in zip(public.events, private.events, drawn.events, strict=True):
            paths |= {sh.path for sh in a.shocks} or {"news only"}
            assert (a.visible, b.visible) == (["A", "B"], ["B"]), seed
            assert a.model_dump(exclude={"visible"}) == b.model_dump(exclude={"visible"}) == c.model_dump(exclude={"visible"})
        assert public.market == private.market == drawn.market and public.horizon == private.horizon == drawn.horizon
    assert paths == {"/a", "/c", "news only"}


@pytest.mark.parametrize(
    "inputs",
    [{"shock_news": "private"}, {"shock_news": "public", "informed_seller": "A"}, {"shock_news": "private", "informed_seller": "C"}],
)
def test_informed_seller_is_set_exactly_for_private_news_and_names_a_seller(inputs):
    with pytest.raises(ValidationError):
        GENERATORS.get("random").generate(inputs, 1)
