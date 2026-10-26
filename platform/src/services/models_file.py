"""conditions/models.yaml (CR-43 M1, lead 22 Oct): the models of the research runs, a freeze input of stage conditions.

Dev and test starts must use these models: each seller of each condition, with its sampling settings, and the LLM judge.
Each entry also holds its profile's base_url and protocol (filled from the profile at PUT), so a profile edited later to
point elsewhere fails the freeze and the start check, not only a changed model name."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from judge.items import placeholders
from services.configs import AgentConfig, ApiError
from services.profiles import url_problem

SCRIPTED = "scripted"
"""A seller entry that is this string: the seller has a scripted (non-LLM) policy of any kind and any parameters."""
IDENTITY = ("provider_profile_id", "model", "model_family")
SAMPLING = ("temperature", "top_p", "max_output_tokens", "timeout_s", "thinking", "thinking_on_timeout")
"""Seller request settings that change the episodes, so test must equal dev (CR-47 M1). The judge's are in llm_judge.py."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelEntry(_Strict):
    provider_profile_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_family: str = Field(min_length=1, description="The judge's family must differ from every LLM seller's")
    base_url: str | None = Field(None, description="Filled from the profile at PUT; the freeze needs it")
    protocol: str | None = Field(None, description="Filled from the profile at PUT; the freeze needs it")


class SellerEntry(ModelEntry):
    """The sampling settings are required, with no default: top_p, max_output_tokens and thinking may be null, but the
    file must say so (DECISIONS-18 entries 2 and 3: no output limit, 300 s, thinking unset for the pilot model). Only
    thinking_on_timeout has a default (null, CR-91 minor 1)."""

    temperature: float = Field(ge=0, le=2)
    top_p: float | None = Field(gt=0, le=1)
    max_output_tokens: int | None = Field(ge=1, description="Null: no output limit (DECISIONS-18 entry 2)")
    timeout_s: float = Field(gt=0, le=600, description="Deadline of the whole call (DECISIONS-18 entry 2: 300 s; DECISIONS-21)")
    thinking: bool | Literal["minimal", "low", "medium", "high", "xhigh"] | None = Field(
        description="Pydantic AI's unified thinking setting, or null (DECISIONS-18 entry 3)"
    )
    thinking_on_timeout: bool | Literal["minimal", "low", "medium", "high", "xhigh"] | None = Field(
        None,
        description="The thinking setting of the attempt after a timeout (DECISIONS-22 entry 5.3). Default null keeps `thinking`,"
        " so a file written before the field stays valid (CR-91 minor 1)",
    )


Seller = SellerEntry | Literal["scripted"]


class ModelsFile(_Strict):
    id: str = Field(min_length=1, max_length=64)
    sellers: dict[str, Seller] = Field(min_length=1, description="Per seller id, for every condition")
    by_condition: dict[str, dict[str, Seller]] = Field(default_factory=dict, description="Per condition, overrides per seller id")
    judge: ModelEntry

    def seller(self, condition_id: str, seller_id: str) -> Seller | None:
        return self.by_condition.get(condition_id, {}).get(seller_id, self.sellers.get(seller_id))

    def entries(self) -> list[tuple[str, ModelEntry]]:
        """(path, entry) of every model entry, the judge last."""
        out = [(f"sellers/{s}", e) for s, e in self.sellers.items() if e != SCRIPTED]
        out += [(f"by_condition/{c}/{s}", e) for c, m in self.by_condition.items() for s, e in m.items() if e != SCRIPTED]
        return [*out, ("judge", self.judge)]


def parse(data: object) -> tuple[ModelsFile | None, list[dict]]:
    """The file, or None with field-path errors. Text `TEAM TO WRITE` is not valid (as in the judge rubric)."""
    found = placeholders(data)
    try:
        file = ModelsFile.model_validate(data)
    except ValidationError as e:
        return None, [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()] + found
    return (None, found) if found else (file, [])


def fill(data: object, record: Callable) -> object:
    """Set base_url and protocol of every entry with a provider_profile_id from that profile. 422 for an unknown profile or a
    base_url with credentials or a query, which would reach a committed file (CR-47 m4)."""

    def refuse(path: str, message: str) -> ApiError:
        return ApiError(422, "validation_error", "conditions/models.yaml cannot take this provider profile.",
                        [{"path": f"{path}provider_profile_id", "message": message}])  # fmt: skip

    def walk(node: object, path: str) -> object:
        if isinstance(node, dict) and isinstance(node.get("provider_profile_id"), str):
            try:
                rec = record(node["provider_profile_id"])
            except ApiError as e:
                raise refuse(path, e.message) from None
            if problem := url_problem(rec.base_url):
                raise refuse(path, f"the profile's base_url {problem}")
            return node | {"base_url": rec.base_url, "protocol": rec.protocol}
        if isinstance(node, dict):
            return {k: walk(v, f"{path}{k}/") for k, v in node.items()}
        return node

    return walk(data, "")


def _drift(entry: ModelEntry, record: Callable, path: str) -> list[dict]:
    """The entry's base_url and protocol against its profile now."""
    try:
        rec = record(entry.provider_profile_id)
    except ApiError as e:
        return [{"path": f"{path}/provider_profile_id", "message": e.message}]
    out = []
    for k in ("base_url", "protocol"):
        now, saved = getattr(rec, k), getattr(entry, k)
        if now != saved:
            out.append({"path": f"{path}/provider_profile_id", "message": f"the profile's {k} is {now!r}, models.yaml has {saved!r}"})
    return out


def freeze_problems(file: ModelsFile, conditions: list, record: Callable) -> list[dict]:
    """What makes a valid file unfit to freeze: an entry without base_url or protocol or whose profile changed since the PUT
    (CR-47 m2), an id that matches no condition or seller (m1), a condition seller with no entry, a C2a or C2b model rule
    broken, or a judge family equal to an LLM seller's family (judge from another family)."""
    out = []
    for p, e in file.entries():
        missing = [k for k in ("base_url", "protocol") if getattr(e, k) is None]
        msg = "missing; save the file through PUT /research/models to fill it from the profile"
        out += [{"path": f"{p}/{k}", "message": msg} for k in missing] if missing else _drift(e, record, p)
    by_id = {c.file.id: c.file for c in conditions}
    used = {s for f in by_id.values() for s in f.sellers}
    out += [{"path": f"sellers/{s}", "message": f"no condition has a seller {s}"} for s in file.sellers if s not in used]
    for c, overrides in file.by_condition.items():
        if c not in by_id:
            out.append({"path": f"by_condition/{c}", "message": f"no condition {c}"})
            continue
        out += [
            {"path": f"by_condition/{c}/{s}", "message": f"condition {c} has no seller {s}"} for s in overrides if s not in by_id[c].sellers
        ]
    families = set()
    for f in by_id.values():
        got = {s: file.seller(f.id, s) for s in f.sellers}
        out += [{"path": f"sellers/{s}", "message": f"no entry for seller {s} of condition {f.id}"} for s, e in got.items() if e is None]
        if None in got.values():
            continue
        keys = [tuple(getattr(e, k) for k in IDENTITY) if e != SCRIPTED else SCRIPTED for e in got.values()]
        fams = [e.model_family for e in got.values() if e != SCRIPTED]  # scripted kinds are checked at start
        if f.same_model_for_all_sellers is True and len(set(keys)) != 1:
            out.append(
                {"path": f"by_condition/{f.id}", "message": f"condition {f.id} needs the same profile, model and family for its sellers"}
            )
        if f.same_model_for_all_sellers is False and len(set(fams)) != len(fams):
            out.append({"path": f"by_condition/{f.id}", "message": f"condition {f.id} needs a different model_family each"})
        families |= set(fams)
    if file.judge.model_family in families:
        out.append({"path": "judge/model_family", "message": f"{file.judge.model_family} is also an LLM seller family"})
    return out


def _differences(entry: ModelEntry, got: dict, record: Callable, path: str) -> list[dict]:
    out = [
        {"path": f"{path}/{k}", "message": f"models.yaml has {getattr(entry, k)!r}, the request {v!r}"}
        for k, v in got.items()
        if v != getattr(entry, k)
    ]
    return out or _drift(entry, record, path)


def seller_differences(file: ModelsFile, condition_id: str, agents: list[AgentConfig], record: Callable) -> list[dict]:
    """Field-path errors for each seller of a run config that does not use the file's model and sampling for this condition."""
    out = []
    for i, a in enumerate(agents):
        if a.role != "seller":
            continue
        want, llm = file.seller(condition_id, a.id), a.policy.llm
        if want is None:
            out.append({"path": f"agents/{i}", "message": f"models.yaml has no entry for seller {a.id}"})
        elif want == SCRIPTED or llm is None:
            if (want == SCRIPTED) != (llm is None):
                need = "a scripted policy" if want == SCRIPTED else "an LLM policy"
                out.append({"path": f"agents/{i}/policy/kind", "message": f"models.yaml gives seller {a.id} {need}"})
        else:
            out += _differences(want, {k: getattr(llm, k) for k in IDENTITY + SAMPLING}, record, f"agents/{i}/policy/llm")
    return out


def judge_differences(file: ModelsFile, llm, record: Callable) -> list[dict]:
    """Field-path errors when an LLM judge backend does not use the file's judge model."""
    return _differences(file.judge, {k: getattr(llm, k) for k in IDENTITY}, record, "backends/llm")
