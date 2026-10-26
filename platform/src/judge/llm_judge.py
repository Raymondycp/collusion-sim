"""The LLM judge request and its strict parse (DECISIONS-9 §4; plan J3). Sent through the existing Endpoint, so any built
protocol works. The transcript goes in unchanged (CR-21 F1): the request holds the rendered text, nothing about the run."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ValidationError

from adapters.llm.endpoint import ProviderProfile, Request
from judge.items import ItemSet
from judge.verify import LlmOutput, check
from judge.views import Line, Rendered

MAX_OUTPUT_TOKENS = 800
ATTEMPTS = 2
"""One retry on an invalid output, with the same paraphrase; the retry is paid from the reserve."""


def retry(status: str) -> bool:
    """Only an invalid output is asked again. Refused, error and check failures are final."""
    return status == "invalid"


def request(item_set: ItemSet, paraphrase: int, rendered: Rendered) -> Request:
    """paraphrase: 1-based, as in the plan. Temperature 0, no tools, text output."""
    messages = item_set.llm_messages(paraphrase - 1, rendered.text)
    return Request(messages, [], True, {"temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS})


def cache_key(req: Request, profile: ProviderProfile, paraphrase_hash: str, input_hash: str, attempt: int) -> tuple[str, str]:
    canonical = json.dumps(
        {
            **req.canonical(),
            "model": profile.model,
            "host": profile.host,
            "paraphrase_hash": paraphrase_hash,
            "input_hash": input_hash,
            "attempt": attempt,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return canonical, "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


FENCE = re.compile(r"\A\s*```(?:json)?[ \t]*\n(.*)\n```\s*\Z", re.DOTALL)


def parse(text: str) -> tuple[LlmOutput | None, str | None]:
    """The output, or None with a short reason. Strict: one JSON object and nothing else, or that object in one markdown
    code fence (Claude models fence JSON output)."""
    if m := FENCE.match(text):
        text = m.group(1)
    try:
        return LlmOutput.model_validate_json(text), None
    except ValidationError as e:
        err = e.errors()[0]
        where = "/".join(str(x) for x in err["loc"])
        return None, f"output {where}: {err['msg']}" if where else f"output: {err['msg']}"


def result(text: str | None, lines: list[Line], item_set: ItemSet, view: str) -> dict:
    """The status, output and error reason of one reply: refused (no text), invalid (no strict parse), else the quote
    check's status. Commitments count only in view talk_prices."""
    if not text:
        return {"status": "refused", "error_reason": "no text in the reply"}
    out, why = parse(text)
    if out is None:
        return {"status": "invalid", "error_reason": why}
    if view != "talk_prices":  # the shared output format asks for commitments; outside talk_prices they never count
        out = out.model_copy(update={"commitments": []})
    checked = check(out, lines, item_set.quote_min_words, commitments_allowed=view == "talk_prices")
    output = out.model_dump() | {
        "quotes": [q.model_dump() for q in checked.quotes],
        "commitments": [c.model_dump() for c in checked.commitments],
        "dropped": checked.dropped,
        "quote_lines": list(checked.quote_lines),
        "agreement": checked.agreement,
        "agreement_forced": checked.agreement_forced,
    }
    return {"status": checked.status, "output": output, "error_reason": checked.reason}
