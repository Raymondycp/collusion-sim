import json
import re
from pathlib import Path

import yaml

DRAFT = Path(__file__).parents[1] / "conditions" / "prompts.draft.yaml"
FORBIDDEN = re.compile(
    r"\b(C1|C2a|C2b|C3|C4|LC|ADV|group|condition|experiment|study|collu\w*|cartel|detect\w*|label\w*|tacit|baseline)\b", re.I
)
GROUPS = {"C1", "C1-public", "C1-public-private", "C2a", "C2b", "C3", "C4", "LC", "ADV"}


def texts(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in ("manipulated",):
                yield from texts(v, f"{path}/{k}")


def test_every_group_has_two_distinct_wordings_for_each_prompt():
    d = yaml.safe_load(DRAFT.read_text(encoding="utf-8"))
    assert set(d["groups"]) == GROUPS
    variants = [d["base"]]
    for g in d["groups"].values():
        for role in ("pricing", "talk"):
            v = g.get(role)
            if isinstance(v, dict):
                variants += [v] if "v1" in v else list(v.values())
    for v in variants:
        assert set(v) == {"v1", "v2"} and v["v1"].strip() != v["v2"].strip()


def test_no_prompt_names_a_group_a_label_or_the_study():
    d = yaml.safe_load(DRAFT.read_text(encoding="utf-8"))
    for path, text in texts({"base": d["base"], "groups": d["groups"]}):
        if text in ("base", "same_as_pricing"):
            continue
        assert not FORBIDDEN.search(text), (path, FORBIDDEN.search(text).group())


def test_each_group_keeps_the_base_goal_and_adds_only_its_item():
    d = yaml.safe_load(DRAFT.read_text(encoding="utf-8"))
    for name, g in d["groups"].items():
        pricing = g["pricing"]
        if pricing == "base":
            continue
        for k in ("v1", "v2"):
            assert pricing[k].startswith(d["base"][k].split(".")[0]), (name, k)


CASES = Path(__file__).parents[1] / "presets" / "cases"
# SCENARIO-LIBRARY §4 item 3: a case prompt names no case, firm, statute, year or outcome.
CASE_WORDS = re.compile(
    r"\b(Topkins|ATPCO|Twombly|Bell|Atlantic|Amazon|Maersk|DOJ|Commission|Sherman|antitrust|Article|statute|law|court|"
    r"case|guilty|plea|settle\w*|fine\w*|price-fixing|conspir\w*|agreement|\d{4})\b",
    re.I,
)


def test_case_presets_keep_the_neutral_base_and_name_no_group_label_or_case():
    base = " ".join(yaml.safe_load(DRAFT.read_text(encoding="utf-8"))["base"]["v2"].split())
    files = sorted(CASES.glob("*.json"))
    assert {f.stem for f in files} == {"base", "amazon_posters", "airline_fares", "shipping_gri", "parallel_only"}
    for f in files:
        for agent in json.loads(f.read_text(encoding="utf-8"))["agents"]:
            llm = agent["policy"]["llm"]
            for text in filter(None, (llm["system_prompt"], llm["talk_prompt"])):
                assert text.startswith(base), f.stem
                assert not FORBIDDEN.search(text), (f.stem, FORBIDDEN.search(text).group())
                assert not CASE_WORDS.search(text), (f.stem, CASE_WORDS.search(text).group())
                extra = [s for s in re.split(r"(?<=\.)\s+", text[len(base) :].strip()) if s]
                assert len(extra) <= 2 and (f.stem != "base" or not extra), (f.stem, extra)


def test_the_case_smoke_base_paraphrases_are_the_condition_file_texts():
    import importlib.util

    path = Path(__file__).parents[1] / "docs" / "sim" / "review" / "pilot-1" / "case_smoke.py"
    spec = importlib.util.spec_from_file_location("case_smoke", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    base = yaml.safe_load(DRAFT.read_text(encoding="utf-8"))["base"]
    assert (smoke.V1, smoke.V2) == (" ".join(base["v1"].split()), " ".join(base["v2"].split()))
