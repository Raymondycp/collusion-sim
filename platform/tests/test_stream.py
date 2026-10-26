"""DECISIONS-21: every model request is a stream. A local HTTP server sends the chunks with real gaps, because the stall
limit is the client's read timeout and a mock transport has none. No paid call."""

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest

import adapters.llm.endpoint as endpoint
import fakewire
from adapters.llm import Endpoint, EndpointError, ProviderProfile, Request

KEY = "sk-test-stream-0000"
THOUGHT = "Rival held 10.5; hold it."


def chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> bytes:
    body = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    body["choices"] = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n".encode()


def pieces() -> list[bytes]:
    """A reply in many small chunks: thinking, then a tool call whose arguments arrive in parts, then usage."""
    args = json.dumps({"price": 10.5, "plan": "Hold."})
    words = THOUGHT.split(" ")
    out = [chunk({"role": "assistant", "reasoning_content": w + (" " if i < len(words) - 1 else "")}) for i, w in enumerate(words)]
    out.append(
        chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "set_price", "arguments": ""}}]})
    )
    out += [chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[i : i + 5]}}]}) for i in range(0, len(args), 5)]
    out.append(chunk({}, finish="tool_calls"))
    out.append(chunk(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}))
    return [*out, b"data: [DONE]\n\n"]


def serve(parts, gaps):
    """A server that sends parts[i] after gaps[i] seconds and keeps each request body. `bodies` gets "closed" when a write
    finds the client gone."""
    bodies: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for part, gap in zip(parts, gaps, strict=False):
                    time.sleep(gap)
                    self.wfile.write(part)
                    self.wfile.flush()
            except OSError:  # the client gave up
                bodies.append("closed")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = ProviderProfile(id="p", protocol="openai_chat", base_url=f"http://127.0.0.1:{srv.server_port}/v1", model="m", family="f")
    return srv, prof, bodies


def ask(prof: ProviderProfile, timeout_s: float = 300):
    return Endpoint(prof, KEY, timeout_s=timeout_s).request(Request([{"role": "user", "content": "u"}], [], True))


@pytest.fixture
def stall(monkeypatch):
    monkeypatch.setattr(endpoint, "STALL_S", 0.6)


def test_a_slow_stream_longer_than_the_stall_limit_is_one_reply_assembled_from_its_chunks(stall):
    parts = pieces()
    srv, prof, bodies = serve(parts, [0.4] + [0.1] * (len(parts) - 1))  # every gap under 0.6 s, the whole call over it
    try:
        reply, latency = ask(prof)
    finally:
        srv.shutdown()
    assert latency > 600 and bodies[0]["stream"] is True
    assert [(c.name, json.loads(c.arguments)) for c in reply.tool_calls] == [("set_price", {"price": 10.5, "plan": "Hold."})]
    assert (reply.thinking, reply.thinking_parts, reply.finish_reason) == (THOUGHT, 1, "tool_call")
    assert (reply.input_tokens, reply.output_tokens) == (100, 20)


def test_a_stream_that_stops_sending_is_a_stalled_provider_error(stall):
    parts = pieces()
    srv, prof, _ = serve(parts[:3], [0.0, 0.1, 2.0])
    start = time.perf_counter()
    try:
        with pytest.raises(EndpointError) as e:
            ask(prof)
    finally:
        srv.shutdown()
    assert (e.value.code, e.value.provider_code) == ("provider_error", "stream_stalled")
    assert time.perf_counter() - start < 1.8, "it fails at the stall limit, not when the server ends"


def test_a_stream_that_keeps_sending_past_the_call_timeout_is_a_timeout(stall):
    parts = pieces()
    srv, prof, _ = serve(parts, [0.3] * len(parts))
    try:
        with pytest.raises(EndpointError) as e:
            ask(prof, timeout_s=1.0)
    finally:
        srv.shutdown()
    assert (e.value.code, e.value.provider_code) == ("timeout", "call_timeout")


def test_a_stream_of_more_than_60_s_with_tokens_flowing_is_not_cut():
    """The pilot case at the real limits: OpenCode Go cut non-streaming calls at about 60 s (DECISIONS-21)."""
    parts = pieces()
    gaps = [5.0] * 13 + [0.0] * (len(parts) - 13)
    srv, prof, _ = serve(parts, gaps)
    try:
        reply, latency = ask(prof)
    finally:
        srv.shutdown()
    assert latency >= 65_000 and reply.tool_calls and reply.thinking == THOUGHT


def pings():
    while True:
        yield b": ping\n\n"


def test_pings_do_not_extend_the_call_deadline_and_the_connection_is_closed(stall):
    """CR-85 3: comment lines reset the stall limit but parse to no event; the wall-clock deadline still ends the call."""
    srv, prof, bodies = serve(pings(), iter(lambda: 0.2, None))
    start = time.perf_counter()
    try:
        with pytest.raises(EndpointError) as e:
            ask(prof, timeout_s=1.0)
        took = time.perf_counter() - start
        time.sleep(0.6)  # the next ping finds the connection closed
    finally:
        srv.shutdown()
    assert (e.value.code, e.value.provider_code) == ("timeout", "call_timeout")
    assert 1.0 <= took < 1.6 and "closed" in bodies


def test_the_production_limits_are_60_s_stall_and_the_timeout_of_the_call():
    assert endpoint.STALL_S == 60.0
    ep = Endpoint(
        ProviderProfile(id="p", protocol="openai_chat", base_url="http://127.0.0.1:9/v1", model="m", family="f"), KEY, timeout_s=300
    )
    assert ep.timeout_s == 300


# CR-85 1: a stream that closes before the provider's finish is never a reply


def mock_stream(protocol: str, events: list):
    return httpx2.MockTransport(
        lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=fakewire._events(events))
    )


CUT = {  # events after which each protocol's stream is closed: no finish chunk, no completed or stop event
    "openai_chat": lambda ev: ev[:-3],
    "openai_responses": lambda ev: ev[:-1],
    "anthropic_messages": lambda ev: ev[:-2],
    "google": lambda ev: [(None, {"candidates": [{"content": {"role": "model", "parts": [{"text": "Let us hold"}]}}]})],
}


@pytest.mark.parametrize("protocol", list(fakewire.WIRE))
def test_a_stream_closed_before_the_finish_is_a_truncated_provider_error_in_every_protocol(protocol):
    from test_item2_llm import TOOL, profile

    events = fakewire.EVENTS[protocol](fakewire.WIRE[protocol]("fake-model"))
    req = Request([{"role": "user", "content": "u"}], [TOOL], True)
    whole, _ = Endpoint(profile(protocol), KEY, transport=mock_stream(protocol, events)).request(req)
    assert whole.tool_calls and whole.output_tokens == 20
    with pytest.raises(EndpointError) as e:
        Endpoint(profile(protocol), KEY, transport=mock_stream(protocol, CUT[protocol](events))).request(req)
    assert (e.value.code, e.value.provider_code) == ("provider_error", "stream_truncated")


def test_a_reply_cut_mid_arguments_is_retried_and_never_stored(tmp_path):
    """cr's case: the standard reply closed cleanly after 12 chunks (arguments cut, no finish, no usage, no [DONE])."""
    from simkit import of
    from test_llm_policy import llm_run

    cut = b"".join(pieces()[:12])
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=cut))
    sink, _, cache, _ = llm_run(None, tmp_path, rounds=1, transport=transport)
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error, c.provider_code, c.output_tokens) for c in calls] == [
        (n, "provider_error", "stream_truncated", None) for n in (1, 2, 3)
    ]
    assert all(a.fallback for a in of(sink, "acted", agent_id="A"))
    with sqlite3.connect(tmp_path / "calls.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone() == (0,)


def test_a_malformed_chunk_is_a_provider_error_named_by_its_class():
    bad = pieces()[:3] + [b"data: {not json\n\n"]
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(bad))
    )
    with pytest.raises(EndpointError) as e:
        Endpoint(
            ProviderProfile(id="p", protocol="openai_chat", base_url="http://fake.local/v1", model="m", family="f"),
            KEY,
            transport=transport,
        ).request(Request([{"role": "user", "content": "u"}], [], True))
    assert (e.value.code, e.value.provider_code) == ("provider_error", "JSONDecodeError")


def test_a_stream_without_usage_gives_zero_tokens_that_the_model_check_refuses():
    """CR-85 2: the reply itself is whole (finish chunk and [DONE]), but the provider sent no usage chunk."""
    no_usage = [p for p in pieces() if b'"usage"' not in p]
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(no_usage))
    )
    reply, _ = Endpoint(
        ProviderProfile(id="p", protocol="openai_chat", base_url="http://fake.local/v1", model="m", family="f"), KEY, transport=transport
    ).request(Request([{"role": "user", "content": "u"}], [], True))
    assert reply.tool_calls and (reply.input_tokens, reply.output_tokens) == (0, 0)
