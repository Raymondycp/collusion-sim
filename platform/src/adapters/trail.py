"""trail.jsonl: the full AI trail of a run, one line per LLM call in call order (DECISIONS-14 §2.2).

`finish_run` writes it to runs/<run_id>/trail.jsonl, next to episode.jsonl, from the run's llm_call events and the call
cache (cache/calls.sqlite). An export serves that file; for a run that has none (older runs) it builds the trail again
from the cache. It is a sidecar: not part of episode.jsonl and not in the episode hash.

Each line copies the llm_call event (seq, round, agent, purpose, attempt, charged, model, tokens, error) and adds, from
the cache, what was sent: `allow_text`, `settings` (the llm_call event's settings as sent; the cache row's configured
settings in runs stored before that field), the tools offered, and `messages`: the request messages and the reply
in Pydantic AI's message format (ModelMessagesTypeAdapter; ModelRequest with SystemPromptPart, UserPromptPart,
ToolReturnPart, then a ModelResponse with ThinkingPart, TextPart and ToolCallPart). `paid_by_run` is the run that paid for the cached
reply, so a hit replayed from another run shows it. Every timestamp is the event's wall_time, so a run gives the same bytes.

`missing`: null when request and reply are there; "reply" for a failed call (the request comes from the failed_calls
table, there is no reply); "no_cache_row" when the cache has neither (a cleared cache, or a failed call before the cache
kept failed requests); "unreadable_row" when a row cannot be read or parsed (that line only; the others stay).
A cache file that cannot be opened counts as no cache. `finish_reason` is the provider's, as Pydantic AI maps it (stop,
length, content_filter, tool_call, error); it is null with no reply and for cache rows written before the cache kept it.
Token counts: RequestUsage in `messages` has no "unknown", so a count the provider did not give is 0 there. The line's own
token fields copy the event and stay null when unknown; use those for analysis.
`thinking` is the text of the thinking or reasoning parts that the provider returned (DECISIONS-14 §2.1), also a
ThinkingPart in the ModelResponse; `thinking_parts` counts those parts (a part can have no text). Nothing asks a
model for them. Both are null with no reply and in cache rows written before the cache kept them; `thinking` is
null, never empty, when the parts had no text.

The lines hold the full prompts, so they show the condition. A trail is for the team's analysis, not for blinded sets.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter
from pydantic_ai import ToolDefinition
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.usage import RequestUsage

from adapters.llm.endpoint import _to_pydantic_ai
from simschema.events import EventBase, LlmCallEvent

TRAIL_VERSION = "trail.v1"
TOOLS = TypeAdapter(list[ToolDefinition])
EVENT_FIELDS = (
    "seq",
    "t",
    "agent_id",
    "purpose",
    "attempt",
    "sample_index",
    "provider_profile_id",
    "model",
    "request_hash",
    "cache_hit",
    "charged",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "cost_usd",
    "latency_ms",
    "error",
    "http_status",
    "provider_code",
    "degraded",
    "slot_wait_ms",
    "first_byte_ms",
    "parallel",
    "provider_message",
)


def _stamped(message, at: datetime):
    """The message with its own and its parts' timestamps set to `at` (Pydantic AI sets now() by default)."""
    parts = [dataclasses.replace(p, timestamp=at) if hasattr(p, "timestamp") else p for p in message.parts]
    return dataclasses.replace(message, parts=parts, timestamp=at)


def _messages(request: dict, reply: dict | None, e: LlmCallEvent) -> list:
    at = datetime.fromisoformat(e.wall_time.replace("Z", "+00:00"))
    sent = [_stamped(m, at) for m in _to_pydantic_ai(request["messages"])]
    if reply is None:
        return sent
    parts = [ThinkingPart(content=reply["thinking"])] if reply.get("thinking") else []
    parts += [TextPart(content=reply["text"])] if reply.get("text") else []
    parts += [ToolCallPart(tool_name=c["name"], args=c["arguments"], tool_call_id=c["id"]) for c in reply["tool_calls"]]
    usage = RequestUsage(
        input_tokens=reply.get("input_tokens") or 0,
        output_tokens=reply.get("output_tokens") or 0,
        cache_read_tokens=reply.get("cached_input_tokens") or 0,
        output_reasoning_tokens=reply.get("reasoning_tokens") or 0,
    )
    answer = ModelResponse(parts=parts, usage=usage, model_name=e.model, timestamp=at, finish_reason=reply.get("finish_reason"))
    return [*sent, answer]


def _rows(conn: sqlite3.Connection | None, key: str) -> tuple[dict | None, dict | None, str | None]:
    """(request, reply, paid_by_run) of a key: a cached reply, else a failed request, else nothing."""
    if conn is None:
        return None, None, None
    row = conn.execute("SELECT request_json, reply_json, paid_by_run FROM calls WHERE key = ?", (key,)).fetchone()
    if row:
        return json.loads(row[0]), json.loads(row[1]), row[2]
    has_failed = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'failed_calls'").fetchone()
    row = conn.execute("SELECT request_json FROM failed_calls WHERE key = ?", (key,)).fetchone() if has_failed else None
    return (json.loads(row[0]), None, None) if row else (None, None, None)


def _open(cache_path: Path) -> sqlite3.Connection | None:
    if not Path(cache_path).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(cache_path).as_posix()}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _line(conn: sqlite3.Connection | None, e: LlmCallEvent) -> dict:
    request, reply, paid_by = _rows(conn, e.request_hash)
    line = {"trail": TRAIL_VERSION, "run_id": e.run_id, **{k: getattr(e, k) for k in EVENT_FIELDS}, "paid_by_run": paid_by}
    line["finish_reason"] = reply.get("finish_reason") if reply else None
    line["thinking"] = reply.get("thinking") if reply else None
    line["thinking_parts"] = reply.get("thinking_parts") if reply else None
    if request is None:
        return line | {"allow_text": None, "settings": e.settings, "tools": None, "messages": None, "missing": "no_cache_row"}
    tools = [ToolDefinition(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in request["tools"]]
    return line | {
        "allow_text": request.get("allow_text"),
        "settings": request.get("settings") if e.settings is None else e.settings,
        "tools": TOOLS.dump_python(tools, mode="json"),
        "messages": ModelMessagesTypeAdapter.dump_python(_messages(request, reply, e), mode="json"),
        "missing": None if reply else "reply",
    }


def trail_lines(events: list[EventBase], cache_path: Path) -> list[dict]:
    calls = sorted((e for e in events if isinstance(e, LlmCallEvent)), key=lambda e: e.seq)
    conn = _open(cache_path)
    try:
        out = []
        for e in calls:
            try:
                out.append(_line(conn, e))
            except Exception:  # one bad row hides only its own call (CR-66 addendum 2)
                base = {"trail": TRAIL_VERSION, "run_id": e.run_id, **{k: getattr(e, k) for k in EVENT_FIELDS}}
                empty = dict.fromkeys(
                    ("paid_by_run", "finish_reason", "thinking", "thinking_parts", "allow_text", "settings", "tools", "messages")
                )
                out.append(base | empty | {"settings": e.settings, "missing": "unreadable_row"})
        return out
    finally:
        if conn:
            conn.close()


def run_trail(runs_dir: Path, cache_path: Path, run_id: str, events: list[EventBase]) -> list[dict]:
    """The trail lines of one run: the trail.jsonl that finish_run stored, else built again from the events and the cache."""
    stored = Path(runs_dir) / run_id / "trail.jsonl"
    if stored.is_file():
        return [json.loads(line) for line in stored.read_text(encoding="utf-8").splitlines() if line.strip()]
    return trail_lines(events, cache_path)


def thinking(line: dict) -> list[str] | None:
    """The thinking text parts of the reply in a trail line, in order. None when the line has no reply; [] when the
    provider returned none (DECISIONS-14 §2.5: a hidden thinking is never an empty string)."""
    replies = [m for m in line.get("messages") or [] if m.get("kind") == "response"]
    if not replies:
        return None
    return [p["content"] for m in replies for p in m.get("parts", []) if p.get("part_kind") == "thinking" and p.get("content")]


def trail_text(events: list[EventBase], cache_path: Path) -> str:
    """trail.jsonl: one JSON object per line, keys sorted, so the same run and cache give the same bytes."""
    return "".join(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n" for line in trail_lines(events, cache_path))
