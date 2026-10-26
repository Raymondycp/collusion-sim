"""A fake OpenAI-compatible chat endpoint on an httpx2 MockTransport. `brain(body)` decides each reply, which is sent as a
stream when the request asks for one (every Endpoint request does; DECISIONS-21)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2

from fakewire import streamed

Reply = dict | int
NO_CHOICES = object()  # a brain reply: a 200 chat completion with an empty choices list


def call(name: str, **args) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}],
    }


def text(content: str) -> dict:
    return {"role": "assistant", "content": content}


class FakeLLM:
    def __init__(self, brain: Callable[[dict], Reply]) -> None:
        self.brain = brain
        self.bodies: list[dict] = []
        self.raw: list[bytes] = []
        self.headers: list[dict[str, str]] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            self.models_auth = request.headers.get("authorization")
            return httpx2.Response(200, json={"object": "list", "data": [{"id": "m-small"}, {"id": "m-large"}, {"object": "model"}]})
        self.raw.append(request.content)
        self.headers.append(dict(request.headers))
        self.auth = request.headers.get("authorization")
        body = json.loads(request.content)
        self.bodies.append(body)
        reply = self.brain(body)
        if isinstance(reply, type) and issubclass(reply, httpx2.TransportError):
            raise reply("fake network failure", request=request)
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"error": {"message": "fake failure", "type": "server_error"}})
        if isinstance(reply, httpx2.Response):
            return reply
        if reply is NO_CHOICES:
            full = {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"], "choices": []}
        else:
            full = {
                "id": f"chatcmpl-{len(self.bodies)}",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop", "message": reply}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }
        return streamed("openai_chat", full) if body.get("stream") else httpx2.Response(200, json=full)


def tools_offered(body: dict) -> list[str]:
    return [t["function"]["name"] for t in body.get("tools", [])]


def last_is_tool_result(body: dict) -> bool:
    return body["messages"][-1]["role"] == "tool"


def cooperator(price: float = 10.5) -> Callable[[dict], Reply]:
    """Looks up rival prices once when it can, then sets `price`. Talk calls answer PASS (no post)."""

    def brain(body: dict) -> Reply:
        offered = tools_offered(body)
        if "set_price" not in offered:
            return text("PASS")
        if "get_rival_prices" in offered and not last_is_tool_result(body):
            return call("get_rival_prices", rounds=1)
        return call("set_price", price=price, plan="Hold the high price.")

    return brain
