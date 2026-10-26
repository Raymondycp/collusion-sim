"""Fake replies in each provider protocol's wire format: one tool call, with usage that has cached and reasoning tokens.

Usage in every format: 100 input tokens (40 of them cached) and 20 output tokens (5 of them reasoning).
Every request is a stream (DECISIONS-21), so `streamed` turns a whole reply into that protocol's server-sent events.
"""

from __future__ import annotations

import json

import httpx2

ARGS = {"price": 10.5, "plan": "Hold."}


def openai_chat(model: str) -> dict:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "set_price", "arguments": json.dumps(ARGS)}}],
    }
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": msg}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }


def openai_responses(model: str) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": "completed",
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "set_price",
                "arguments": json.dumps(ARGS),
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 120,
        },
    }


def anthropic_messages(model: str) -> dict:
    # Anthropic reports input_tokens without the cache reads; Pydantic AI adds them back (100 in total).
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "set_price", "input": ARGS}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 60, "output_tokens": 20, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0},
    }


def google(model: str) -> dict:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"functionCall": {"name": "set_price", "args": ARGS}}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 40,
            "candidatesTokenCount": 15,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 120,
        },
        "modelVersion": model,
    }


WIRE = {"openai_chat": openai_chat, "openai_responses": openai_responses, "anthropic_messages": anthropic_messages, "google": google}


class FakeWire:
    """Answers every request of one protocol with a tool call, or with `status` when set. Keeps each request."""

    def __init__(self, protocol: str, status: int | None = None) -> None:
        self.protocol, self.status = protocol, status
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx2.Response(
                self.status, json={"error": {"message": "fake failure", "type": "rate_limit_error", "code": self.status}}
            )
        return streamed(self.protocol, WIRE[self.protocol]("fake-model"))


def _events(pairs: list[tuple[str | None, dict | str]]) -> bytes:
    out = []
    for name, data in pairs:
        head = f"event: {name}\n" if name else ""
        out.append(f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n")
    return "".join(out).encode()


def _openai_chat_events(full: dict) -> list[tuple[str | None, dict | str]]:
    """One chunk per choice with the whole message as its delta, then the finish reason, then the usage."""
    base = {k: full.get(k) for k in ("id", "created", "model")} | {"object": "chat.completion.chunk"}
    out: list[tuple[str | None, dict | str]] = []
    for c in full.get("choices") or []:
        delta = dict(c.get("message") or {})
        if delta.get("tool_calls"):
            delta["tool_calls"] = [{"index": i, **t} for i, t in enumerate(delta["tool_calls"])]
        out.append((None, base | {"choices": [{"index": c.get("index", 0), "delta": delta, "finish_reason": None}]}))
        out.append((None, base | {"choices": [{"index": c.get("index", 0), "delta": {}, "finish_reason": c.get("finish_reason")}]}))
    if full.get("usage"):
        out.append((None, base | {"choices": [], "usage": full["usage"]}))
    return [*out, (None, "[DONE]")]


def _openai_responses_events(full: dict) -> list[tuple[str | None, dict | str]]:
    start = full | {"status": "in_progress", "output": [], "usage": None}
    out: list[tuple[str | None, dict | str]] = [("response.created", {"type": "response.created", "sequence_number": 0, "response": start})]
    for i, item in enumerate(full["output"]):
        for kind in ("added", "done"):
            name = f"response.output_item.{kind}"
            out.append((name, {"type": name, "sequence_number": len(out), "output_index": i, "item": item}))
    out.append(("response.completed", {"type": "response.completed", "sequence_number": len(out), "response": full}))
    return out


def _anthropic_events(full: dict) -> list[tuple[str | None, dict | str]]:
    usage = full["usage"]
    start = full | {"content": [], "stop_reason": None, "usage": usage | {"output_tokens": 0}}
    out: list[tuple[str | None, dict | str]] = [("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(full["content"]):
        if block["type"] == "tool_use":
            first, delta = block | {"input": {}}, {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            first, delta = block | {"text": ""}, {"type": "text_delta", "text": block["text"]}
        out += [
            ("content_block_start", {"type": "content_block_start", "index": i, "content_block": first}),
            ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": i}),
        ]
    stop = {"stop_reason": full["stop_reason"], "stop_sequence": None}
    out.append(("message_delta", {"type": "message_delta", "delta": stop, "usage": {"output_tokens": usage["output_tokens"]}}))
    return [*out, ("message_stop", {"type": "message_stop"})]


def _google_events(full: dict) -> list[tuple[str | None, dict | str]]:
    return [(None, full)]


EVENTS = {
    "openai_chat": _openai_chat_events,
    "openai_responses": _openai_responses_events,
    "anthropic_messages": _anthropic_events,
    "google": _google_events,
}


def streamed(protocol: str, full: dict) -> httpx2.Response:
    """A whole reply of `protocol` as the server-sent events of its stream."""
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_events(EVENTS[protocol](full)))
