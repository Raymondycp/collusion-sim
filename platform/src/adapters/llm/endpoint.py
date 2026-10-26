"""A provider endpoint through the Pydantic AI direct API. The profile's protocol picks the wire format (PROTOCOLS).

Every SDK client has its retries off (our policy owns retries). The key goes on the wire only through an httpx2 Auth
that sets the protocol's header per request; the SDKs get a placeholder. Errors become a code only: no provider text,
request body or header is kept, so a key cannot leak through an error.

Every request is a stream (DECISIONS-21): a provider closes a non-streaming call at about 60 s, while tokens arriving keep a
stream open. The reply is the assembled final response; a partial stream is never kept. A stream with no data for STALL_S
is a provider error (`stream_stalled`); a stream that ends without the provider's finish reason is a provider error
(`stream_truncated`); a call longer than the timeout is a `timeout` (`call_timeout`), on a wall clock that pings or comment
lines cannot extend (CR-85 3).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal

import anthropic
import httpx2
import openai
from anthropic import AsyncAnthropic
from google.genai import errors as google_errors
from openai import AsyncOpenAI
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_ai import ToolDefinition
from pydantic_ai.direct import model_request_stream
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from adapters.keys import mask_secrets

ProtocolId = Literal["openai_chat", "openai_responses", "anthropic_messages", "google", "typesafe_systemone"]

USER_AGENT = f"collusion-sim/{importlib.metadata.version('collusion-sim')}"
STALL_S = 60.0
"""Seconds with no data on a stream before the call fails (DECISIONS-21 §3)."""
KEY_HEADERS = {"authorization", "x-api-key", "x-goog-api-key", "user-agent"}


def _session_header(name: str | None) -> str | None:
    if name is not None and (not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", name) or name.lower() in KEY_HEADERS):
        raise ValueError("a header name (letters, digits, '-'), not a key header and not User-Agent")
    return name


SessionHeader = Annotated[
    str | None,
    AfterValidator(_session_header),
    Field(description="Header that carries a stable conversation id (<run_id>:<agent_id>), for example x-opencode-session"),
]


class ProviderProfile(BaseModel):
    """Everything about an endpoint except its key. The key lives in the KeyStore under the profile id."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    base_url: str
    model: str
    family: str
    protocol: ProtocolId = "openai_chat"
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cached_input_usd_per_mtok: float | None = None
    supports_seed: bool = False
    max_concurrency: int = 2
    max_calls_per_5h: int | None = None
    session_header: SessionHeader = None

    @property
    def host(self) -> str:
        return httpx2.URL(self.base_url).host

    @property
    def base_path(self) -> str:
        """The path of base_url, without query or fragment (a key in a query string never reaches the record)."""
        return httpx2.URL(self.base_url).path

    @property
    def priced(self) -> bool:
        return self.input_usd_per_mtok is not None and self.output_usd_per_mtok is not None

    def price(self) -> dict:
        """The price as the run freezes it. source "unknown": no input or output price, so calls cost null."""
        return {
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "cached_input_usd_per_mtok": self.cached_input_usd_per_mtok,
            "source": "user" if self.priced else "unknown",
        }

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float | None:
        """USD of one call, or None without a price. input_tokens includes the cached ones; with no cached price they
        cost the input price. Reasoning tokens are inside output_tokens."""
        if not self.priced:
            return None
        cached = self.cached_input_usd_per_mtok if self.cached_input_usd_per_mtok is not None else self.input_usd_per_mtok
        fresh = input_tokens - cached_input_tokens
        return (self.input_usd_per_mtok * fresh + cached * cached_input_tokens + self.output_usd_per_mtok * output_tokens) / 1e6


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Reply:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None  # Pydantic AI's FinishReason; None in cache rows written before DECISIONS-14 §2.2
    # DECISIONS-14 §2.1: what the provider returned as thinking or reasoning parts, never asked for. `thinking_parts` counts
    # them (a part may carry no text, for example a signature only); `thinking` joins their text, None when there is none.
    # Both None in cache rows written before.
    thinking: str | None = None
    thinking_parts: int | None = None

    @property
    def tokens(self) -> int:
        """What max_tokens counts: input (cached included) + output (reasoning included)."""
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "tool_calls": [c.__dict__ for c in self.tool_calls],
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "finish_reason": self.finish_reason,
                "thinking": self.thinking,
                "thinking_parts": self.thinking_parts,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, blob: str) -> "Reply":
        d = json.loads(blob)
        calls = tuple(ToolCall(**c) for c in d["tool_calls"])
        return cls(
            d["text"],
            calls,
            d["input_tokens"],
            d["output_tokens"],
            d.get("cached_input_tokens"),
            d.get("reasoning_tokens"),
            d.get("finish_reason"),
            d.get("thinking"),
            d.get("thinking_parts"),
        )


SETTING_WIRE = {
    "temperature": ("temperature",),
    "top_p": ("top_p", "topP"),
    "max_tokens": ("max_tokens", "max_completion_tokens", "max_output_tokens", "maxOutputTokens"),
    "seed": ("seed",),
}
"""Per request setting, its body field names in each protocol (Google sends them in generationConfig)."""
REASONING_FIELDS = ("reasoning_effort", "reasoning", "thinking", "output_config")
"""Top-level request fields that carry the thinking setting (openai_chat, openai_responses, anthropic_messages); google
sends it in generationConfig.thinkingConfig."""


def _reasoning(body: dict) -> dict:
    """The reasoning fields of a request body (REASONING_FIELDS, or Google's thinkingConfig)."""
    fields = {k: body[k] for k in REASONING_FIELDS if k in body}
    if isinstance(body.get("generationConfig"), dict) and "thinkingConfig" in body["generationConfig"]:
        fields["thinkingConfig"] = body["generationConfig"]["thinkingConfig"]
    return fields


HTTP_CODES = {401: "auth", 403: "auth", 404: "not_found", 410: "not_found", 429: "rate_limit"}
FATAL_HTTP = frozenset({401, 403, 404, 410, 422})
"""HTTP statuses that fail on every retry (bad key, unknown or removed model, a request the API cannot read), so they stop
the run after the first call (DECISIONS-23 entry 2). Any other status, a 400 refusal among them, takes the error path: resent,
then a recorded fallback, with its status, code and masked message."""
FATAL = frozenset({"auth", "not_found"})
MESSAGE_CHARS = 200
"""Length of the masked provider message kept with a failed call (DECISIONS-24 §3)."""
# A provider's own error code is kept only in this shape (for example rate_limit_exceeded), so free text never gets in.
PROVIDER_CODE = re.compile(r"[a-z0-9_.]{1,48}")


class EndpointError(Exception):
    """A provider failure, as a code. `fatal` codes (bad key, unknown or removed model, bad path) and the FATAL_HTTP statuses
    fail on every retry, so they stop the run.
    `http_status`: the HTTP status, when there was one. `provider_code`: the provider's error code or type from the error body
    when it has the PROVIDER_CODE shape, else the name of the exception class for a failure with no HTTP status.
    `message`: the provider's error message, masked (keys.mask_secrets) and cut to MESSAGE_CHARS; never in str(error)."""

    def __init__(self, code: str, http_status: int | None = None, provider_code: str | None = None, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = code in FATAL or http_status in FATAL_HTTP
        self.http_status, self.provider_code, self.message = http_status, provider_code, message


def _body_message(body: object, auth: _KeyHeader) -> str | None:
    """The provider's error message ({"message": ...} or {"error": {"message": ...}}), on one line, masked by
    keys.mask_secrets with the endpoint's own key: key-shaped strings, the key, its hint and a masked echo of it become
    [redacted]; model ids stay (DECISIONS-24 §3; CR-90 addendum 1)."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    text = body.get("message") if isinstance(body, dict) else body if isinstance(body, str) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return auth.mask(" ".join(text.split()), MESSAGE_CHARS)


def _body_code(body: object) -> str | None:
    """The provider's error code or type from an error body ({"code": ...} or {"error": {"code": ...}})."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    if not isinstance(body, dict):
        return None
    for name in ("code", "type"):
        value = body.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and PROVIDER_CODE.fullmatch(str(value)):
            return str(value)
    return None


@dataclass
class Request:
    """Our provider-neutral request. Its canonical JSON is the cache key input."""

    messages: list[dict]
    tools: list[dict]
    allow_text: bool
    settings: dict = field(default_factory=dict)

    def canonical(self) -> dict:
        return {"messages": self.messages, "tools": self.tools, "allow_text": self.allow_text, "settings": self.settings}


def _to_pydantic_ai(messages: list[dict]) -> list:
    out: list = []
    for m in messages:
        if m["role"] == "system":
            out.append(ModelRequest(parts=[SystemPromptPart(content=m["content"])]))
        elif m["role"] == "user":
            if out and isinstance(out[-1], ModelRequest) and all(isinstance(p, SystemPromptPart) for p in out[-1].parts):
                out[-1] = ModelRequest(parts=[*out[-1].parts, UserPromptPart(content=m["content"])])
            else:
                out.append(ModelRequest(parts=[UserPromptPart(content=m["content"])]))
        elif m["role"] == "assistant":
            parts = [TextPart(content=m["content"])] if m.get("content") else []
            parts += [ToolCallPart(tool_name=c["name"], args=c["arguments"], tool_call_id=c["id"]) for c in m["tool_calls"]]
            out.append(ModelResponse(parts=parts))
        elif m["role"] == "tool":
            part = ToolReturnPart(tool_name=m["name"], content=m["content"], tool_call_id=m["tool_call_id"])
            if out and isinstance(out[-1], ModelRequest) and all(isinstance(p, ToolReturnPart) for p in out[-1].parts):
                out[-1] = ModelRequest(parts=[*out[-1].parts, part])
            else:
                out.append(ModelRequest(parts=[part]))
    return out


_slots: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def _slot(profile: ProviderProfile) -> threading.BoundedSemaphore:
    """One semaphore per profile, shared by all runs in this process: at most `max_concurrency` calls in flight."""
    with _slots_lock:
        return _slots.setdefault((profile.id, profile.max_concurrency), threading.BoundedSemaphore(profile.max_concurrency))


class _KeyHeader(httpx2.Auth):
    """Sets the protocol's key header per request. The key lives only here; repr and vars() do not show it.

    Also sets the headers that are never secret (`plain`: User-Agent, the session header), last, so an SDK default
    cannot replace them."""

    __slots__ = ("__key", "__header", "__scheme", "plain")

    def __init__(self, key: str, header: str, scheme: str, plain: dict[str, str]) -> None:
        self.__key, self.__header, self.__scheme = key, header, scheme
        self.plain = plain

    def __repr__(self) -> str:
        return "_KeyHeader(***)"

    def mask(self, text: str, limit: int) -> str:
        """keys.mask_secrets with this key, so a provider's echo of it never leaves the endpoint (CR-90 addendum 1)."""
        return mask_secrets(text, (self.__key,), limit)

    def auth_flow(self, request: httpx2.Request):
        request.headers[self.__header] = self.__scheme + self.__key
        request.headers.update(self.plain)
        yield request


PLACEHOLDER = "set-per-request"


def _openai(model_cls: type[Model]) -> Callable[[str, str, httpx2.AsyncClient], Model]:
    def build(model: str, base_url: str, http: httpx2.AsyncClient) -> Model:
        client = AsyncOpenAI(api_key=PLACEHOLDER, base_url=base_url, max_retries=0, http_client=http)
        # Pydantic AI drops the unified thinking setting for a model name it does not know (every OpenCode Go model);
        # models.yaml says which models take it (DECISIONS-18 entry 3), so the setting is always passed on.
        return model_cls(model, provider=OpenAIProvider(openai_client=client), profile={"supports_thinking": True})

    return build


def _anthropic(model: str, base_url: str, http: httpx2.AsyncClient) -> Model:
    client = AsyncAnthropic(api_key=PLACEHOLDER, base_url=base_url, max_retries=0, http_client=http)
    return AnthropicModel(model, provider=AnthropicProvider(anthropic_client=client))


def _google(model: str, base_url: str, http: httpx2.AsyncClient) -> Model:
    # google-genai retries only when retry_options is set, so the default is no retry.
    return GoogleModel(model, provider=GoogleProvider(api_key=PLACEHOLDER, http_client=http, base_url=base_url))


@dataclass(frozen=True)
class Protocol:
    header: str
    scheme: str
    build: Callable[[str, str, httpx2.AsyncClient], Model] | None


PROTOCOLS: dict[str, Protocol] = {
    "openai_chat": Protocol("Authorization", "Bearer ", _openai(OpenAIChatModel)),
    "openai_responses": Protocol("Authorization", "Bearer ", _openai(OpenAIResponsesModel)),
    "anthropic_messages": Protocol("x-api-key", "", _anthropic),
    "google": Protocol("x-goog-api-key", "", _google),
    "typesafe_systemone": Protocol("Authorization", "Bearer ", None),
}


def protocol_built(protocol: str) -> bool:
    return PROTOCOLS[protocol].build is not None


_SDK_ERRORS = (openai.APIError, anthropic.APIError, google_errors.APIError, httpx2.HTTPError, UnexpectedModelBehavior)
_TIMEOUTS = (openai.APITimeoutError, anthropic.APITimeoutError, httpx2.TimeoutException)


def _loop() -> asyncio.AbstractEventLoop:
    """This thread's event loop, the one Pydantic AI's sync wrappers use, so a client's pooled connections stay on one loop."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def _stalled(e: BaseException) -> bool:
    """A read timeout anywhere in the cause chain: no data for STALL_S."""
    seen: BaseException | None = e
    while seen is not None:
        if isinstance(seen, httpx2.ReadTimeout):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class Endpoint:
    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str,
        transport: httpx2.BaseTransport | httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 60.0,
        session: str | None = None,
    ) -> None:
        """Raises EndpointError("protocol_not_built") for a registered protocol that has no client yet. `session`: the
        conversation id sent in the profile's session header (not part of a request's cache key)."""
        self.profile = profile
        wire = PROTOCOLS[profile.protocol]
        if wire.build is None:
            raise EndpointError("protocol_not_built")
        plain = {"User-Agent": USER_AGENT}
        if profile.session_header and session:
            plain[profile.session_header] = session
        # The read timeout is the gap between two pieces of data, so on a stream it is the stall limit; the whole call is
        # held to timeout_s in _send.
        timeout = httpx2.Timeout(timeout_s, read=min(STALL_S, timeout_s))
        self._auth = _KeyHeader(api_key, wire.header, wire.scheme, plain)
        http = httpx2.AsyncClient(transport=transport, timeout=timeout, auth=self._auth)
        self.timeout_s = timeout_s
        self._model = wire.build(profile.model, profile.base_url, http)
        self._timing = threading.local()
        self._probes: dict[str, dict] = {}  # request body per settings JSON (_probe)
        self._probe_lock = threading.Lock()  # sent_settings may run on any thread (CR-90 addendum 2 minor 3)

    def timing(self) -> tuple[int | None, int | None]:
        """(slot_wait_ms, first_byte_ms) of this thread's last request, also a failed one (DECISIONS-22 entry 5.8). Slot wait
        is the wait for a concurrency slot of the profile; first byte is the time from the request to the first stream data
        (null when none came)."""
        return getattr(self._timing, "slot_wait_ms", None), getattr(self._timing, "first_byte_ms", None)

    def sent_thinking(self, thinking: bool | str | None) -> str:
        """The provider's reasoning fields of a request with this thinking setting, as canonical JSON. Pydantic AI renders
        the request to a capture transport (no network call, no key), so the result is the same on a replay. Two settings
        with the same result send the same request: for gpt-6-luna, minimal is sent as low (DECISIONS-22, degraded; lead,
        23 Oct)."""
        return json.dumps(_reasoning(self._probe({} if thinking is None else {"thinking": thinking})), sort_keys=True)

    def sent_settings(self, settings: dict) -> dict:
        """The request settings as Pydantic AI sends them (DECISIONS-23 entries 3 and 4): a setting that the body does not
        carry is left out (OpenAI reasoning models take no temperature or top_p while reasoning is on), and `thinking` is
        replaced by `sent_reasoning`, the provider's reasoning fields in the body. The cache key keeps the configured settings."""
        probed = self._probe(settings | ({"seed": 0} if "seed" in settings else {}))  # the seed value does not change the shape
        body = probed | (probed["generationConfig"] if isinstance(probed.get("generationConfig"), dict) else {})
        out = {k: v for k, v in settings.items() if k != "thinking" and any(n in body for n in SETTING_WIRE.get(k, (k,)))}
        if reasoning := _reasoning(probed):
            out["sent_reasoning"] = reasoning
        return out

    def _probe(self, settings: dict) -> dict:
        """The JSON body that Pydantic AI sends for a one-message request with these settings, rendered to a capture
        transport: no network call and no key, so the result is the same on a replay. Kept per settings."""
        key = json.dumps(settings, sort_keys=True)
        with self._probe_lock:
            if key not in self._probes:
                self._probes[key] = self._render(settings)
            return self._probes[key]

    def _render(self, settings: dict) -> dict:
        bodies: list[dict] = []

        def capture(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")

        probe = Endpoint(self.profile.model_copy(update={"id": f"{self.profile.id}.probe"}), "probe-no-key", httpx2.MockTransport(capture))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # Pydantic AI warns when it drops a setting; dropped_sampling reports it
            try:
                probe.request(Request([{"role": "user", "content": "probe"}], [], True, settings))
            except EndpointError:  # the empty stream has no reply; only the captured body is wanted
                pass
        return bodies[0]

    def __repr__(self) -> str:
        return f"Endpoint(profile={self.profile.id!r}, protocol={self.profile.protocol!r})"

    def request(self, req: Request) -> tuple[Reply, int]:
        """Send one request. Returns the reply and the latency in ms. Raises EndpointError(code)."""
        params = ModelRequestParameters(
            function_tools=[
                ToolDefinition(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in req.tools
            ],
            allow_text_output=req.allow_text,
        )
        asked = time.perf_counter()
        self._timing.slot_wait_ms, self._timing.first_byte_ms = None, None
        with _slot(self.profile):
            self._timing.slot_wait_ms = int((time.perf_counter() - asked) * 1000)
            return self._send(req, params)

    async def _stream(self, req: Request, params: ModelRequestParameters) -> ModelResponse:
        """The assembled final response. The deadline cancels the read and closes the connection whatever arrives."""
        async with asyncio.timeout(self.timeout_s):
            async with model_request_stream(
                self._model, _to_pydantic_ai(req.messages), model_settings=dict(req.settings), model_request_parameters=params
            ) as stream:
                events = stream.__aiter__()
                try:
                    async for _ in events:
                        if self._timing.first_byte_ms is None:
                            self._timing.first_byte_ms = int((time.perf_counter() - self._timing.sent) * 1000)
                finally:  # a deadline or an error leaves the generator open: close it on this loop (CR-90)
                    if hasattr(events, "aclose"):
                        await events.aclose()
                return stream.get()

    def _send(self, req: Request, params: ModelRequestParameters) -> tuple[Reply, int]:
        start = self._timing.sent = time.perf_counter()
        try:
            resp = _loop().run_until_complete(self._stream(req, params))
        except TimeoutError:
            raise EndpointError("timeout", None, "call_timeout") from None
        except ModelHTTPError as e:
            code = HTTP_CODES.get(e.status_code, "provider_error")
            raise EndpointError(code, e.status_code, _body_code(e.body), _body_message(e.body, self._auth)) from None
        except ModelAPIError as e:  # pydantic-ai wraps SDK connection errors, timeouts among them, in this type
            if _stalled(e):
                raise EndpointError("provider_error", None, "stream_stalled") from None
            kind = type(e.__cause__ or e).__name__[:48]
            raise EndpointError("timeout" if isinstance(e.__cause__, _TIMEOUTS) else "provider_error", None, kind) from None
        except _SDK_ERRORS as e:
            if _stalled(e):
                raise EndpointError("provider_error", None, "stream_stalled") from None
            status = getattr(e, "status_code", None)
            raise EndpointError("provider_error", status if isinstance(status, int) else None, type(e).__name__[:48]) from None
        if not (resp.provider_details or {}).get("finish_reason"):
            # Only the provider's own finish (finish chunk, response.completed, message_delta, finishReason) ends a reply;
            # Pydantic AI fills finish_reason "stop" for a stream that just closed (CR-85 1).
            raise EndpointError("provider_error", None, "stream_truncated")
        latency = int((time.perf_counter() - start) * 1000)
        text = "".join(p.content for p in resp.parts if isinstance(p, TextPart)) or None
        calls = tuple(
            ToolCall(id=p.tool_call_id, name=p.tool_name, arguments=p.args if isinstance(p.args, str) else json.dumps(p.args or {}))
            for p in resp.parts
            if isinstance(p, ToolCallPart)
        )
        thought = [p for p in resp.parts if isinstance(p, ThinkingPart)]
        thinking = "\n\n".join(p.content for p in thought if p.content) or None
        u = resp.usage
        reply = Reply(
            text,
            calls,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_tokens,
            u.output_reasoning_tokens,
            resp.finish_reason,
            thinking,
            len(thought),
        )
        return reply, latency
