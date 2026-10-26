"""Research unit 4: episode feature table (DECISIONS-10 §5, CR-17 U4-1 to U4-8). Scripted sellers only; no paid call."""

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from features import REGISTRY, Feature, FeatureRegistry, LayerError, ScenarioFacts
from features.base import SRC, source_hash, source_modules
from features.table import Child, compute, row_values
from services import features as svc
from simschema.events import TIER, ReferenceEvent, ReferencePoint, SettledEvent
from test_api import check
from test_experiments import body, client, created, finished  # noqa: F401 (client is a fixture)
from test_profiles import MemoryKeyring
from test_research import ROOT
from condcopy import draft_conditions

README = Path(__file__).resolve().parents[1] / "src" / "features" / "README.md"
LEAKS = ("labels", "parent_labels", "condition_id", "condition_hash", "c3_isolation", "prompt_hash", "label_source")
# Prices that move, so no correlation is flat (CR-23 M2): grim trigger punishes one dip of its partner; two sellers move apart.
COLLUDERS = [
    {"id": "A", "role": "seller", "policy": {"kind": "grim_trigger", "params": {"target": 10.5}}},
    {"id": "B", "role": "seller", "policy": {"kind": "price_schedule", "params": {"prices": {"5": 9.0}, "default": 10.5}}},
]
APART = [
    {
        "id": "A",
        "role": "seller",
        "policy": {"kind": "price_schedule", "params": {"prices": {"3": 6.0, "7": 5.5, "12": 6.2}, "default": 5.75}},
    },
    {"id": "B", "role": "seller", "policy": {"kind": "price_schedule", "params": {"prices": {"5": 5.5, "9": 6.1}, "default": 5.75}}},
]


@pytest.fixture(scope="module")
def done(tmp_path_factory):
    """One finished experiment: C1 (grim trigger vs one dip) and C2a (two fixed schedules), seeds 1 and 2."""
    root = tmp_path_factory.mktemp("features")
    shutil.copytree(ROOT / "scenarios", root / "scenarios")
    draft_conditions(root / "conditions")
    with TestClient(create_app(root, use_dbos=False, key_backend=MemoryKeyring())) as c:
        body = {
            "conditions": ["C1", "C2a"],
            "split": "pilot",
            "seeds": [1, 2],
            "agents": COLLUDERS,
            "agents_by_condition": {"C2a": APART},
            "budget": {"max_usd": 1.0},
            "batch_budget": {"max_calls": 1000},
        }
        r = c.post("/api/experiments", json=body)
        assert r.status_code < 300, r.text
        exp_id = r.json()["id"]
        assert c.app.state.experiments.wait(exp_id, timeout=300)["status"] == "completed"
        yield c, exp_id


def table(c, exp_id, layer="B"):
    s = c.app.state
    return svc.table(s.db, s.research.catalog, s.research, exp_id, layer)


def by_condition(rows, cid):
    return [r for r in rows if r["condition_id"] == cid]


# The table


def test_one_row_per_parent_with_ids_labels_and_every_feature_value(done):
    c, exp_id = done
    meta, columns, rows = table(c, exp_id)
    assert [(r["condition_id"], r["seed"]) for r in rows] == [("C1", 1), ("C1", 2), ("C2a", 1), ("C2a", 2)]
    assert columns[: len(svc.ID_COLUMNS)] == list(svc.ID_COLUMNS)
    for r in rows:
        assert r["parent_status"] == "completed" and r["split"] == "pilot" and r["experiment_id"] == exp_id
        assert r["config_hash"] and r["condition_hash"] and r["seeds_hash"] and r["child_run_ids"]
    c1, c2a = by_condition(rows, "C1")[0], by_condition(rows, "C2a")[0]
    assert (c1["group"], c1["label_legal"], c1["label_harm"]) == ("C1", "yes", "yes")
    assert c2a["group"] == "C2a" and c2a["label_legal"] == "no"
    for i in REGISTRY.ids():
        assert c1[i] is not None and c1[f"{i}_missing"] is None, (i, c1[f"{i}_missing"])
    assert [f["id"] for f in meta["features"]] == REGISTRY.ids()
    assert all(f["version"] and re.fullmatch(r"[0-9a-f]{64}", f["code_hash"]) for f in meta["features"])
    assert meta["features_code_hash"] == REGISTRY.code_hash()


def test_known_answers_on_scripted_sellers(done):
    """Grim trigger punishes a probe of its partner; fixed schedules answer no one."""
    c, exp_id = done
    _, _, rows = table(c, exp_id)
    for r, low in zip(by_condition(rows, "C1"), by_condition(rows, "C2a")):
        assert r["punishment_depth"] > 0.5
        assert r["delta_p"] > low["delta_p"]  # about 10.5 against 5.75 on the same seed
        assert r["corr_lag"] > 0.4 > low["corr_lag"]  # A follows B's dip one round later; fixed schedules do not
    for r in by_condition(rows, "C2a"):
        assert r["punishment_depth"] == pytest.approx(0, abs=1e-9)
        assert r["msg_volume"] == 0
        assert r["ablation_effect"] == 0 and r["ablation_effect_missing"] is None  # null ablation of fixed sellers


def test_the_log_only_table_has_only_layer_a_features(done):
    c, exp_id = done
    meta, columns, rows = table(c, exp_id, "A")
    assert {f["id"] for f in meta["features"]} == {
        "corr_raw",
        "corr_lag",
        "msg_volume",
        "timeout_fallbacks",
    }  # D22: the check column is layer A
    assert all(f["data_layer"] == "A" and f["access"] == "log" for f in meta["features"])
    assert not {"delta_p", "punishment_depth"} & set(columns)


def test_two_exports_are_byte_identical_and_a_row_does_not_depend_on_other_rows(done):
    c, exp_id = done
    s = c.app.state
    one = svc.export_csv(s.db, s.research.catalog, s.research, exp_id, "B")
    assert one == svc.export_csv(s.db, s.research.catalog, s.research, exp_id, "B")
    _, columns, rows = table(c, exp_id)
    events = s.db.events(rows[0]["parent_run_id"])
    header = events[0].header
    alone = row_values(REGISTRY.ids(), header.seller_order, events, [], ScenarioFacts((18, 25)), [], "completed")
    assert {k: alone[k] for k in ("delta_p", "corr_raw", "corr_lag", "msg_volume", "br_gap_net")} == {
        k: rows[0][k] for k in ("delta_p", "corr_raw", "corr_lag", "msg_volume", "br_gap_net")
    }


# U4-1: a feature never sees labels or the condition


class Spy(Feature):
    id, version, name, description = "spy", "1", "spy", "spy"
    evidence_type, data_layer, access = "dependence", "B", "intervention"
    inputs = tuple(t for t, tier in TIER.items() if tier != "C" and t != "run_started")
    scenario = True
    branches = ("probe", "placebo", "ablation")
    seen: list = []

    def compute(self, parent, branches, evaluations):
        Spy.seen.append((parent, branches))
        return 0.0


def test_no_feature_input_holds_a_label_or_a_condition(done):
    c, exp_id = done
    s = c.app.state
    _, _, rows = table(c, exp_id)
    row = by_condition(rows, "C1")[0]
    events = s.db.events(row["parent_run_id"])
    children = []
    for rid in row["child_run_ids"].split(";"):
        ev = s.db.events(rid)
        br = ev[0].header.branch
        children.append(Child(br.intervention.type, rid, "completed", br.fork_t, br.intervention, tuple(ev)))
    assert {c.intervention.type for c in children} >= {"probe", "ablation"}
    Spy.seen.clear()
    compute(Spy, events[0].header.seller_order, events, children, ScenarioFacts((18, 25)), [])
    ((parent, branches),) = Spy.seen
    assert len(branches) == len(children)
    seen = [parent, *(b.episode for b in branches)]
    dumped = json.dumps([e.model_dump(mode="json") for ep in seen for e in ep.events])
    assert not any(f'"{k}"' in dumped for k in LEAKS)
    assert row["condition_hash"] not in dumped
    assert all(e.type != "run_started" for ep in seen for e in ep.events)
    assert not hasattr(parent, "header")
    for b in branches:  # CR-23 m4: the ablation kind tells whether the group has a private channel
        assert not hasattr(b, "intervention")
        shown = json.dumps({k: v for k, v in vars(b).items() if k != "episode"}, default=lambda o: o.model_dump(mode="json"))
        assert "ablated" not in shown and "channel" not in shown, shown


def test_the_header_can_never_be_declared():
    class Header(Spy):
        id = "header"
        inputs = ("run_started",)

    with pytest.raises(LayerError, match="never a feature input"):
        FeatureRegistry().register(Header)


def test_unposted_talk_text_is_never_a_feature_input_even_in_layer_b():
    """CR-46 n1: talk_text is tier B, but model-written private text is never a feature input."""

    class Unposted(Feature):
        id, version, name, description = "unposted", "1", "unposted", "unposted"
        evidence_type, data_layer, access = "conversation", "B", "model"
        inputs = ("talk_text",)

    with pytest.raises(LayerError, match="talk_text is never a feature input"):
        FeatureRegistry().register(Unposted)


# U4-2: layer A sees tier A only


def test_a_layer_a_feature_cannot_declare_a_tier_b_or_c_event():
    for t in ("reference", "llm_call", "planned", "memory_summarised"):

        class Bad(Feature):
            id, version, name, description = "bad", "1", "bad", "bad"
            evidence_type, data_layer, access = "dependence", "A", "log"
            inputs = (t,)

        with pytest.raises(LayerError):
            FeatureRegistry().register(Bad)


def test_a_layer_a_feature_that_reads_a_reference_or_the_scenario_at_run_time_fails(done):
    c, exp_id = done
    _, _, rows = table(c, exp_id)
    events = c.app.state.db.events(rows[0]["parent_run_id"])

    class Sneaky(Feature):
        id, version, name, description = "sneaky", "1", "sneaky", "sneaky"
        evidence_type, data_layer, access = "dependence", "A", "log"
        inputs = ("settled",)
        reads = "reference"

        def compute(self, parent, branches, evaluations):
            return float(len(parent.of(self.reads))) if self.reads else parent.scenario.quiet_window[0]

    with pytest.raises(LayerError, match="not a declared input"):
        compute(Sneaky, ("A", "B"), events, [], ScenarioFacts((18, 25)), [])
    Sneaky.reads = ""
    with pytest.raises(LayerError, match="scenario"):
        compute(Sneaky, ("A", "B"), events, [], ScenarioFacts((18, 25)), [])


def test_a_layer_a_view_holds_tier_a_events_only_even_when_a_tier_b_event_has_a_declared_type(done):
    c, exp_id = done
    events = c.app.state.db.events(table(c, exp_id)[2][0]["parent_run_id"])
    forged = [e.model_copy(update={"tier": "B"}) if e.type == "settled" else e for e in events]

    class Count(Feature):
        id, version, name, description = "count", "1", "count", "count"
        evidence_type, data_layer, access = "dependence", "A", "log"
        inputs = ("settled",)

        def compute(self, parent, branches, evaluations):
            return float(len(parent.of("settled")))

    assert compute(Count, ("A", "B"), forged, [], None, [])[0] == 0.0
    assert compute(Count, ("A", "B"), events, [], None, [])[0] > 0


def test_access_and_layer_rules_of_the_registry():
    assert all(REGISTRY[i].access == "log" for i in REGISTRY.ids("A"))
    for layer, access, branches, ok in [
        ("A", "model", (), False),
        ("B", "log", ("probe",), False),
        ("B", "model", ("probe",), False),
        ("B", "intervention", (), False),
        ("B", "intervention", ("probe",), True),
    ]:
        cls = type(
            "X",
            (Feature,),
            {
                "id": "x",
                "version": "1",
                "name": "x",
                "description": "x",
                "evidence_type": "dependence",
                "data_layer": layer,
                "access": access,
                "inputs": ("settled",),
                "branches": branches,
            },
        )
        if ok:
            FeatureRegistry().register(cls)
        else:
            with pytest.raises(LayerError):
                FeatureRegistry().register(cls)


def test_the_readme_table_lists_every_feature_with_its_layer_access_and_evidence_type():
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        cells = [x.strip().strip("`") for x in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and cells[0] in REGISTRY.ids():
            rows[cells[0]] = tuple(cells[1:4])
    assert rows == {i: (REGISTRY[i].evidence_type, REGISTRY[i].data_layer, REGISTRY[i].access) for i in REGISTRY.ids()}


# U4-4 and U4-7: windows from the scenario; missing is null with a reason


def _round(t, price, nash=4.0, joint=5.5):
    common = {"seq": 2 * t, "run_id": "run_x", "t": float(t), "wall_time": "2026-10-21T00:00:00+00:00"}
    ref = ReferencePoint(
        t=t,
        params={},
        costs={"A": 1.0, "B": 1.0},
        nash=nash,
        joint_optimum=joint,
        nash_by_seller={"A": nash, "B": nash},
        joint_by_seller={"A": joint, "B": joint},
        nash_profit=1.0,
        joint_profit=2.0,
    )
    prices = {"A": price, "B": price}
    return [
        SettledEvent(**common, tier="A", prices=prices, quantities={"A": 1.0, "B": 1.0}, profits={"A": 1.0, "B": 1.0}),
        ReferenceEvent(
            **{**common, "seq": 2 * t + 1}, tier="B", reference=ref, best_response={"A": 4.375, "B": 4.375}, consumer_surplus=1.0
        ),
    ]


def test_delta_p_leaves_out_the_quiet_window_given_by_the_scenario():
    """Nash (Δp 0) outside rounds 3–4, joint optimum (Δp 1) inside them."""
    events = [e for t in range(1, 7) for e in _round(t, 5.5 if t in (3, 4) else 4.0)]
    dp = REGISTRY["delta_p"]
    assert compute(dp, ("A", "B"), events, [], ScenarioFacts((3, 4)), []) == (0.0, None)
    assert compute(dp, ("A", "B"), events, [], ScenarioFacts((0, 0)), [])[0] == pytest.approx(2 / 6)
    assert compute(dp, ("A", "B"), events, [], ScenarioFacts((1, 6)), []) == (None, "no_rounds")


def test_a_parent_that_did_not_complete_gives_null_and_its_status_for_every_feature():
    events = [e for t in range(1, 7) for e in _round(t, 5.0)]
    out = row_values(REGISTRY.ids(), ("A", "B"), events, [], ScenarioFacts((18, 25)), [], "stopped")
    assert all(out[i] is None and out[f"{i}_missing"] == "parent_stopped" for i in REGISTRY.ids())


def test_missing_children_give_a_reason_never_zero():
    events = [e for t in range(1, 7) for e in _round(t, 5.0)]
    out = row_values(REGISTRY.ids(), ("A", "B"), events, [], ScenarioFacts((18, 25)), [], "completed")
    assert (out["punishment_depth"], out["punishment_depth_missing"]) == (None, "no_probe_child")
    assert (out["ablation_effect"], out["ablation_effect_missing"]) == (None, "no_ablation_child")
    assert out["delta_p"] == pytest.approx(2 / 3)


def test_no_feature_is_missing_by_group_in_the_default_plan(done):
    """Lead (unit 4): with an ablation child in every group, a null in a feature column never tells the group."""
    c, exp_id = done
    _, _, rows = table(c, exp_id)
    assert {r["group"] for r in rows} == {"C1", "C2a"}
    assert [(r["group"], i) for r in rows for i in REGISTRY.ids() if r[i] is None] == []


def test_the_export_route_gives_the_table_as_json_or_csv_and_follows_the_contract(done):
    c, exp_id = done
    body = check(c.get(f"/api/experiments/{exp_id}/features"), "/experiments/{experiment_id}/features")
    _, columns, rows = table(c, exp_id)
    assert body["columns"] == columns and body["rows"] == rows
    s = c.app.state
    csv = c.get(f"/api/experiments/{exp_id}/features", params={"format": "csv", "layer": "A"})
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text == svc.export_csv(s.db, s.research.catalog, s.research, exp_id, "A")[1]
    assert csv.text.splitlines()[0].endswith("corr_lag,corr_lag_missing,timeout_fallbacks,timeout_fallbacks_missing")
    missing = c.get("/api/experiments/exp_00000000000000000000000000/features")
    assert missing.status_code == 404


# CR-23 M1: one hash over every module a feature runs


def test_the_code_hash_covers_every_repo_module_a_feature_imports_and_a_helper_edit_changes_it(tmp_path):
    names = source_modules(["features.builtin.dependence", "features.table"])
    assert {
        "features.builtin.dependence",
        "features.builtin.coalition_advantage",
        "features.table",
        "features.base",
        "indicators.mathx",
    } <= set(names)
    assert not any(n.startswith(("api", "services", "adapters")) for n in names)
    src = tmp_path / "src"
    for pkg in {n.split(".")[0] for n in names}:
        shutil.copytree(SRC / pkg, src / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    roots = [REGISTRY["corr_raw"].__module__, "features.table"]
    before = source_hash(roots, src)
    assert before == REGISTRY["corr_raw"].code_hash()
    (src / "indicators" / "unused_helper.py").write_text("X = 1\n", encoding="utf-8")
    assert source_hash(roots, src) == before  # a module nothing imports does not count
    for helper in ("indicators/mathx.py", "features/table.py"):
        f = src / helper
        f.write_text(f.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        assert source_hash(roots, src) != before, helper
        before = source_hash(roots, src)


# CR-23 M2: flat prices give null, never 0


def test_two_constant_sellers_give_null_flat_prices_for_every_correlation_never_zero():
    events = [e for t in range(1, 9) for e in _round(t, 5.0)]
    out = row_values(REGISTRY.ids(), ("A", "B"), events, [], ScenarioFacts((18, 25)), [], "completed")
    for i in ("corr_raw", "corr_lag", "corr_cond"):
        assert (out[i], out[f"{i}_missing"]) == (None, "flat_prices"), i
    moving = [e for t in range(1, 9) for e in _round(t, 5.0 + 0.1 * (t % 3))]
    assert row_values(["corr_raw"], ("A", "B"), moving, [], None, [], "completed")["corr_raw"] == pytest.approx(1.0)


# CR-23 m1 and m2: planned children and parents keep their ids, group and labels


def test_a_planned_child_counts_as_not_completed_and_is_listed(client, monkeypatch):  # noqa: F811
    from services.configs import ApiError

    def branch(parent, req, budget=None, ids=None):
        raise ApiError(409, "paid_calls_disabled", "LLM agents make paid calls.")

    monkeypatch.setattr(client.app.state.svc, "branch", branch)
    exp = created(client, body(branch_plan={"probe": {}, "placebo": {"n": 2}, "ablation": None}))
    assert finished(client, exp["id"])["status"] == "blocked"
    _, _, (row,) = table(client, exp["id"])
    assert len(row["child_run_ids"].split(";")) == 3
    assert row["child_kinds"] == "placebo;placebo;probe"  # by kind name, then plan index
    runs = sorted(client.app.state.db.experiment_runs(exp["id"]), key=lambda r: r["idx"])
    placebos = [r["run_id"] for r in runs if r["kind"] == "placebo"]  # plan-index order
    assert row["child_run_ids"].split(";")[:2] == placebos
    assert (row["punishment_depth"], row["punishment_depth_missing"]) == (None, "probe_child_not_completed")


def test_a_parent_that_never_started_keeps_its_group_labels_and_split(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client.app.state.svc, "commit", lambda prepared: None)
    exp = created(client, body(branch_plan={"probe": None, "placebo": {"n": 1}, "ablation": None}))
    _, _, (row,) = table(client, exp["id"])
    assert row["parent_status"] == "planned" and row["child_run_ids"]
    assert (row["group"], row["label_legal"], row["label_harm"], row["split"]) == ("C1", "yes", "yes", "pilot")
    assert row["condition_hash"] and row["seeds_hash"] and row["corr_raw_missing"] == "parent_planned"


def test_child_kinds_name_the_plan_kind_of_each_child_run_id_in_the_same_order(done):
    c, exp_id = done
    _, columns, rows = table(c, exp_id)
    assert columns.index("child_kinds") == columns.index("child_run_ids") + 1
    kinds = {r["run_id"]: r["kind"] for r in c.app.state.db.experiment_runs(exp_id)}
    for row in rows:
        ids, got = row["child_run_ids"].split(";"), row["child_kinds"].split(";")
        assert got == [kinds[i] for i in ids] and got == sorted(got) and {"probe", "ablation"} <= set(got)


def test_a_parent_with_no_children_has_empty_child_columns(client):  # noqa: F811
    exp = created(client, body(branch_plan={"probe": None, "placebo": None, "ablation": None}))
    assert finished(client, exp["id"])["status"] == "completed"
    _, _, (row,) = table(client, exp["id"])
    assert (row["child_run_ids"], row["child_kinds"]) == ("", "")


# Shuffle ablation manipulation check (lead, 22 Oct): no-op rounds per shuffle child, beside ablation_effect


def _observed(t: int, noop):
    """noop: True (a reader saw no change), False (a reader saw the shuffle), "-" (a call that reads no channel, as a C3
    act call), None (an episode written before contract 0.23.0)."""
    from types import SimpleNamespace

    if noop == "-":
        o = SimpleNamespace(mask_noop=False, masked=["channels"], recent_messages=[])
    else:
        o = SimpleNamespace(mask_noop=noop, masked=[], recent_messages=None if noop is None else ([] if noop else ["msg"]))
    return SimpleNamespace(type="observed", t=t, observation=o)


def _ablation(mode, noops: dict[int, list], status="completed", fork_t=10) -> Child:
    from simschema.events import AblationIntervention

    iv = AblationIntervention(ablated="none") if mode is None else AblationIntervention(ablated="channel", channel_id="AB", mode=mode)
    return Child("ablation", f"run_{mode}", status, fork_t, iv, tuple(_observed(t, n) for t, ns in noops.items() for n in ns))


def test_the_noop_columns_count_the_rounds_of_each_shuffle_child_in_which_a_reader_saw_no_change():
    from features.table import NOOP_COLUMNS, noop_columns

    half = _ablation("shuffle", {9: [True], 10: [True, False], 11: [False, False], 12: [False, True], 13: [False, False]})
    assert noop_columns([half]) == {
        "ablation_noop_rounds": 2,
        "ablation_noop_share": 0.5,
        "ablation_noop_share_all": 0.0,
    }  # round 9: before
    every = _ablation("shuffle", {10: [True], 11: [True]})
    others = [_ablation("clear", {10: [True]}), _ablation(None, {10: [True]}), _ablation("shuffle", {10: [True]}, status="failed")]
    worst = {"ablation_noop_rounds": 4, "ablation_noop_share": 1.0, "ablation_noop_share_all": 1.0}
    assert noop_columns([half, every, *others]) == worst  # the worst child
    assert noop_columns(others) == dict.fromkeys(NOOP_COLUMNS), "no completed shuffle child: null, not 0"


def test_any_reader_gates_and_every_reader_is_reported_beside_it():
    """CR-68 3: one direction dead (a seller repeats one text) is a no-op round under "any", not under "every"; a call that
    reads no channel is not a reader."""
    from features.table import noop_columns

    one_way = noop_columns([_ablation("shuffle", {10: [True, False], 11: [True, False]})])
    assert (one_way["ablation_noop_share"], one_way["ablation_noop_share_all"]) == (1.0, 0.0)
    isolated = noop_columns([_ablation("shuffle", {10: [True, "-"], 11: [False, "-"]})])
    assert (isolated["ablation_noop_share"], isolated["ablation_noop_share_all"]) == (0.5, 0.5)


def test_a_child_written_before_mask_noop_existed_gives_null_not_a_pass():
    """CR-68 2: every mask_noop null (before contract 0.23.0) means not recorded, never 0 no-op rounds."""
    from features.table import NOOP_COLUMNS, noop_columns

    old = _ablation("shuffle", {10: [None, None], 11: [None, None]})
    assert noop_columns([old]) == dict.fromkeys(NOOP_COLUMNS)
    new = _ablation("shuffle", {10: [False], 11: [False]})
    assert noop_columns([old, new])["ablation_noop_share"] == 0.0, "a recorded child still counts beside an old one"


def test_the_noop_columns_sit_beside_ablation_effect_and_only_with_it():
    from features.table import feature_columns

    cols = feature_columns(REGISTRY.ids())
    at = cols.index("ablation_effect_missing")
    assert cols[at + 1 : at + 4] == ["ablation_noop_rounds", "ablation_noop_share", "ablation_noop_share_all"]
    assert "ablation_noop_rounds" not in feature_columns(["corr_raw", "msg_volume"])
    events = [e for t in range(1, 7) for e in _round(t, 5.0)]
    stopped = row_values(REGISTRY.ids(), ("A", "B"), events, [], ScenarioFacts((18, 25)), [], "stopped")
    assert all(stopped[c] is None for c in ("ablation_noop_rounds", "ablation_noop_share", "ablation_noop_share_all"))
    assert "ablation_noop_rounds" not in row_values(["corr_raw"], ("A", "B"), events, [], ScenarioFacts((18, 25)), [], "completed")


def test_scripted_sellers_that_never_talk_make_every_shuffle_round_a_noop(done):
    """No message on the private channel: the shuffle has nothing to permute, so the manipulation failed in every round."""
    c, exp_id = done
    _, columns, rows = table(c, exp_id)
    assert columns.index("ablation_noop_rounds") == columns.index("ablation_effect_missing") + 1
    for r in by_condition(rows, "C1"):
        assert r["ablation_noop_share"] == 1.0 and r["ablation_noop_rounds"] > 0, r
    for r in by_condition(rows, "C2a"):  # a null ablation: no shuffle child
        assert r["ablation_noop_rounds"] is None and r["ablation_noop_share"] is None
