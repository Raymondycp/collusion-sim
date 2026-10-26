"""Provider profiles: an endpoint, its wire protocol, default model and optional prices. The key is in the KeyStore, never here.

Two sources: the profile store (Providers page, keys in the keyring) and `.env` (SIM_PROFILE_<ID>_*, read-only, read once at
start). A `.env` profile is built from its variables one by one; its key variable is only named, never read here, so no
validation error can echo a key (CR-13 P2). Startup errors name a variable, never a value.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from adapters.keys import KeyStore, env_value, key_env, mask_secrets, read_env_file
from adapters.llm.endpoint import PROTOCOLS, Endpoint, EndpointError, ProtocolId, ProviderProfile, Request, SessionHeader, protocol_built
from services.configs import ApiError


class StartupError(RuntimeError):
    """The server refuses to start. The message names variables and profile ids, never a value."""


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    cached_input_usd_per_mtok: float | None = Field(None, ge=0, description="Null prices cached input as input")


class Supports(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_calls: bool | None = None
    json_schema: bool | None = None
    checked_at: str | None = None


class ProfileFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    protocol: ProtocolId = Field("openai_chat", description="Wire format of the endpoint")
    default_model: str | None = None
    pricing: Pricing | None = Field(None, description="Optional. Without a price, calls record cost_usd null and max_usd cannot stop them")
    model_pricing: dict[str, Pricing] = Field(default_factory=dict)
    max_concurrency: int = Field(default=2, ge=1, description="Calls in flight at once, for all runs of this server")
    max_calls_per_5h: int | None = Field(
        None, ge=1, description="Paid calls in any 5 hours, counted on this machine only (other machines sharing the key are not seen)"
    )
    supports_seed: bool = Field(default=False, description="The endpoint honours the OpenAI seed parameter")
    session_header: SessionHeader = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if problem := url_problem(v):
            raise ValueError(f"base_url {problem}")
        return v.rstrip("/")


def url_problem(v: str) -> str | None:
    """Why a base_url is refused, or None. No credentials or query: the URL is shown, logged and copied into
    conditions/models.yaml, a committed file (CR-47 m4); a key goes in the key store only."""
    url = httpx2.URL(v)
    if url.scheme not in ("http", "https") or not url.host:
        return "must be an http(s) URL"
    if url.userinfo or url.query:
        return "must not hold a user name, a password or a query string"
    return None


class ProfileRecord(ProfileFields):
    id: str
    source: Literal["store", "env"] = "store"
    supports: Supports = Field(default_factory=Supports)
    created_at: str


ENV_PREFIX = "SIM_PROFILE_"
ENV_SUFFIXES = sorted(
    ("_BASE_URL", "_PROTOCOL", "_KEY", "_MAX_CALLS_PER_5H", "_DEFAULT_MODEL", "_MAX_CONCURRENCY", "_NAME", "_SESSION_HEADER"),
    key=len,
    reverse=True,
)


def _env_names(env_file: Path) -> list[str]:
    return sorted({n for n in [*os.environ, *read_env_file(env_file)] if n.startswith(ENV_PREFIX)})


def env_profiles(env_file: Path, keys: KeyStore) -> list[ProfileRecord]:
    """The SIM_PROFILE_<ID>_* profiles (DECISIONS-9 §16). Registers each key variable with `keys`. Raises StartupError."""
    groups: dict[str, dict[str, str]] = {}
    for name in _env_names(env_file):
        rest = name[len(ENV_PREFIX) :]
        suffix = next((s for s in ENV_SUFFIXES if rest.endswith(s) and len(rest) > len(s)), None)
        if suffix is None:
            raise StartupError(f"{name}: not a profile variable; the suffix must be one of {', '.join(ENV_SUFFIXES)}")
        groups.setdefault(rest[: -len(suffix)], {})[suffix] = name
    out = []
    for raw, names in sorted(groups.items()):
        if "_BASE_URL" not in names:
            raise StartupError(f"{ENV_PREFIX}{raw}_*: these variables need {ENV_PREFIX}{raw}_BASE_URL")
        pid = raw.lower()
        if pid.startswith("prof_"):
            raise StartupError(f"{ENV_PREFIX}{raw}_BASE_URL: a .env profile id must not start with prof_ (store profiles use it)")

        def value(suffix: str) -> str | None:
            return env_value(names[suffix], env_file) if suffix in names else None

        def number(suffix: str) -> int | None:
            v = value(suffix)
            if v is None:
                return None
            if not v.isdigit() or int(v) < 1:
                raise StartupError(f"{names[suffix]}: not a whole number of at least 1")
            return int(v)

        protocol = value("_PROTOCOL") or "openai_chat"
        if protocol not in PROTOCOLS:
            raise StartupError(f"{names['_PROTOCOL']}: not one of {', '.join(PROTOCOLS)}")
        try:
            rec = ProfileRecord(
                id=pid,
                source="env",
                name=value("_NAME") or pid,
                base_url=value("_BASE_URL") or "",
                protocol=protocol,
                default_model=value("_DEFAULT_MODEL"),
                max_concurrency=number("_MAX_CONCURRENCY") or 2,
                max_calls_per_5h=number("_MAX_CALLS_PER_5H"),
                session_header=value("_SESSION_HEADER"),
                created_at="env",
            )
        except ValidationError as e:
            fields = sorted({str(err["loc"][0]) for err in e.errors()})
            hints = {"base_url": "an http(s) URL", "session_header": "a header name, not a key header and not User-Agent"}
            why = "; ".join(f"{f} must be {hints[f]}" for f in fields if f in hints)
            raise StartupError(f"{ENV_PREFIX}{raw}_*: invalid {', '.join(fields)}" + (f" ({why})" if why else "")) from None
        if "_KEY" not in names:  # CR-19 m4: its own variable is its only key source, never the keyring or SIM_KEY_<ID>
            raise StartupError(f"{ENV_PREFIX}{raw}_KEY: missing; a .env profile needs its key variable (it may be empty)")
        keys.use_env_key(pid, names["_KEY"])
        out.append(rec)
    return out


DEFAULT_ROLES = ("SELLER", "JUDGE", "JUDGE2", "HOST_LLM")
DEFAULT_VARS = [
    f"SIM_DEFAULT_{role}{agent}_{what}"
    for role in DEFAULT_ROLES
    for agent in ["", *(f"_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")]
    for what in ("PROFILE", "MODEL")
]
"""The only variables GET /defaults reads (CR-13 note): never a scan over the environment."""


class DefaultPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=200)


class RoleDefaults(DefaultPick):
    agents: dict[Annotated[str, Field(pattern="^[A-Z]$")], DefaultPick] = {}


class DefaultsBody(BaseModel):
    """PUT /defaults: the same shape as GET. Values from the web UI; they replace the .env values role by role."""

    model_config = ConfigDict(extra="forbid")
    seller: RoleDefaults | None = None
    judge: RoleDefaults | None = None
    judge2: RoleDefaults | None = None
    host_llm: RoleDefaults | None = None


def save_defaults(path: Path, body: DefaultsBody, profiles: "ProfileService") -> None:
    """Every named profile must exist (a default names a profile, never a key)."""
    picks = [(f"{role}/profile_id", r.profile_id) for role, r in body if r is not None]
    picks += [(f"{role}/agents/{a}/profile_id", p.profile_id) for role, r in body if r is not None for a, p in r.agents.items()]
    errors = []
    for where, pid in picks:
        if pid is None:
            continue
        try:
            profiles.record(pid)
        except ApiError:
            errors.append({"path": where, "message": "unknown provider profile"})
    if errors:
        raise ApiError(422, "validation_error", "A default names a provider profile that does not exist.", errors)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body.model_dump_json(exclude_none=True), encoding="utf-8")
    os.replace(tmp, path)


def defaults(env_file: Path, stored: Path | None = None) -> dict:
    """GET /defaults: profile and model per role, and per agent id. A role saved from the web UI (PUT /defaults, in the
    data root) replaces that role's .env values; .env gives only the first values (DECISIONS-11 §1). Pre-fill only."""
    out = env_defaults(env_file)
    if stored is not None and stored.exists():
        saved = DefaultsBody.model_validate_json(stored.read_text(encoding="utf-8"))
        for role, r in saved:
            if r is not None:
                out[role] = r.model_dump() | {"agents": {a: p.model_dump() for a, p in r.agents.items()}}
    return out


def env_defaults(env_file: Path) -> dict:
    """The allow-listed SIM_DEFAULT_* variables (DECISIONS-9 §16)."""
    out = {}
    for role in DEFAULT_ROLES:
        row = {"profile_id": env_value(f"SIM_DEFAULT_{role}_PROFILE", env_file), "model": env_value(f"SIM_DEFAULT_{role}_MODEL", env_file)}
        agents = {}
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p, m = env_value(f"SIM_DEFAULT_{role}_{c}_PROFILE", env_file), env_value(f"SIM_DEFAULT_{role}_{c}_MODEL", env_file)
            if p or m:
                agents[c] = {"profile_id": p, "model": m}
        out[role.lower()] = row | {"agents": agents}
    return out


def paid_calls_allowed(env_file: Path | None = None) -> bool:
    """The lead's switch. Off unless SIM_ALLOW_PAID=1. The process environment wins; with `env_file`, a line in the
    server's `.env` sets it too, the same way the profile keys fall back to `.env` (handoff simplify-cut A2)."""
    value = os.environ.get("SIM_ALLOW_PAID")
    if value is None and env_file is not None:
        value = read_env_file(env_file).get("SIM_ALLOW_PAID")
    return value == "1"


def _errors(e: ValidationError) -> list[dict]:
    """Paths and messages only: a pydantic error can echo its input, and the input may be a key."""
    return [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()]


def _key(body: dict, required: bool) -> str | None:
    key = body.pop("api_key", None)
    if key is None and required:
        raise ApiError(422, "validation_error", "api_key is required.", [{"path": "api_key", "message": "required"}])
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ApiError(422, "validation_error", "api_key must be a non-empty string.", [{"path": "api_key", "message": "empty"}])
    return key.strip() if key else None


class ProfileService:
    def __init__(self, db_path: Path, keys: KeyStore) -> None:
        """Reads the .env profiles once. Raises StartupError on a bad variable or a profile id with two key sources."""
        self.keys = keys
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, json TEXT NOT NULL)")
        self.env = {r.id: r for r in env_profiles(keys.env_file, keys)}
        self._check_sources()

    def _check_sources(self) -> None:
        """DECISIONS-9 §16 and CR-13 P1: one key source per profile id, compared in key_env form."""
        env_ids = {key_env(i): i for i in self.env}
        for rec in self._rows():
            if key_env(rec.id) in env_ids:
                raise StartupError(
                    f"Profile id {rec.id} is in the profile store and in .env ({ENV_PREFIX}{env_ids[key_env(rec.id)].upper()}_*)"
                )
            sources = self.keys.sources(rec.id)
            if len(sources) > 1:
                raise StartupError(f"Profile {rec.id} has a key in {' and in '.join(sources)}; remove one")

    def close(self) -> None:
        self._conn.close()

    def _rows(self) -> list[ProfileRecord]:
        with self._lock:
            return [ProfileRecord.model_validate_json(r[0]) for r in self._conn.execute("SELECT json FROM profiles ORDER BY id")]

    def record(self, profile_id: str) -> ProfileRecord:
        if profile_id in self.env:
            return self.env[profile_id]
        with self._lock:
            row = self._conn.execute("SELECT json FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"Provider profile {profile_id} not found.")
        return ProfileRecord.model_validate_json(row[0])

    def _save(self, rec: ProfileRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (rec.id, rec.model_dump_json()))

    def view(self, rec: ProfileRecord) -> dict:
        status = self.keys.status(rec.id)
        return rec.model_dump(mode="json") | {
            "read_only": rec.source == "env",
            "key_set": status.key_set,
            "key_hint": status.key_hint,
            "key_store": status.key_store,
            "key_env": f"{ENV_PREFIX}{rec.id.upper()}_KEY" if rec.source == "env" else key_env(rec.id),
        }

    def list(self) -> list[dict]:
        return [self.view(r) for r in [*self._rows(), *self.env.values()]]

    def _writable(self, profile_id: str) -> None:
        if profile_id in self.env:
            raise ApiError(409, "profile_read_only", f"Provider profile {profile_id} comes from .env; edit .env and restart the server.")

    def _store(self) -> None:
        """Store profiles need a key store. Off by default: `.env` profiles are the sprint path (handoff simplify-cut B4)."""
        if not self.keys.store_enabled:
            raise ApiError(
                409,
                "keyring_disabled",
                "This server has no key store. Add the provider to .env (SIM_PROFILE_<ID>_*) and restart it.",
            )

    def get(self, profile_id: str) -> dict:
        return self.view(self.record(profile_id))

    def create(self, profile_id: str, body: dict) -> dict:
        self._store()
        body = dict(body)
        key = _key(body, required=True)
        try:
            fields = ProfileFields.model_validate(body)
        except ValidationError as e:
            raise ApiError(422, "validation_error", "The profile does not match the schema.", _errors(e)) from None
        rec = ProfileRecord(
            **fields.model_dump(), id=profile_id, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        self.keys.put(profile_id, key)
        self._save(rec)
        return self.view(rec)

    def update(self, profile_id: str, body: dict) -> dict:
        self._writable(profile_id)
        rec = self.record(profile_id)
        body = dict(body)
        key = _key(body, required=False)
        try:
            fields = ProfileFields.model_validate(rec.model_dump(include=set(ProfileFields.model_fields)) | body)
        except ValidationError as e:
            raise ApiError(422, "validation_error", "The profile does not match the schema.", _errors(e)) from None
        supports = Supports() if fields.base_url != rec.base_url else rec.supports
        changed = ProfileRecord.model_validate(rec.model_dump() | fields.model_dump() | {"supports": supports.model_dump()})
        if key is not None:
            self._store()
            self.keys.put(profile_id, key)
        self._save(changed)
        return self.view(changed)

    def delete(self, profile_id: str, in_use: bool) -> None:
        self._writable(profile_id)
        self.record(profile_id)
        if in_use:
            raise ApiError(409, "profile_in_use", f"Provider profile {profile_id} is used by a queued or running run.")
        self.keys.delete(profile_id)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    def price_of(self, profile_id: str, model: str) -> tuple[float, float] | None:
        try:
            rec = self.record(profile_id)
        except ApiError:
            return None
        p = rec.model_pricing.get(model) or rec.pricing
        return (p.input_usd_per_mtok, p.output_usd_per_mtok) if p else None

    def bind(self, profile_id: str, model: str, family: str | None) -> ProviderProfile:
        """The adapter's view of one endpoint and model, with its price if it has one (DECISIONS-8 §5: prices are optional).
        Raises 422 profile_key_missing or protocol_not_built."""
        rec = self.record(profile_id)
        if not self.keys.status(profile_id).key_set:
            raise ApiError(
                422,
                "profile_key_missing",
                f"Provider profile {profile_id} has no key. Enter it on the Providers page or set {self.view(rec)['key_env']}.",
            )
        if not protocol_built(rec.protocol):
            raise ApiError(
                422, "protocol_not_built", f"Provider profile {profile_id} uses protocol {rec.protocol}, which is designed, not built."
            )
        price = rec.model_pricing.get(model) or rec.pricing
        return ProviderProfile(
            id=rec.id,
            base_url=rec.base_url,
            model=model,
            family=family or "unknown",
            protocol=rec.protocol,
            input_usd_per_mtok=price.input_usd_per_mtok if price else None,
            output_usd_per_mtok=price.output_usd_per_mtok if price else None,
            cached_input_usd_per_mtok=price.cached_input_usd_per_mtok if price else None,
            supports_seed=rec.supports_seed,
            max_concurrency=rec.max_concurrency,
            max_calls_per_5h=rec.max_calls_per_5h,
            session_header=rec.session_header,
        )

    def models(self, profile_id: str, transport: httpx2.BaseTransport | None = None) -> dict:
        """GET the protocol's model list with the key. Not an LLM call and not billed. Only model ids come back."""
        rec = self.record(profile_id)
        key = self.keys.get(profile_id)
        if key is None:
            raise ApiError(422, "profile_key_missing", f"Provider profile {profile_id} has no key.")
        try:
            with httpx2.Client(transport=transport, timeout=10) as client:
                wire = PROTOCOLS[rec.protocol]
                resp = client.get(rec.base_url + MODELS_PATH.get(rec.protocol, "/models"), headers={wire.header: wire.scheme + key})
        except httpx2.HTTPError:
            return {"models": [], "available": False}
        if resp.status_code != 200:
            return {"models": [], "available": False}
        try:
            body = json.loads(resp.text)
            rows = [(m.get("id") or m.get("name") or "") for m in body.get("data", body.get("models", [])) if isinstance(m, dict)]
        except (ValueError, AttributeError):
            return {"models": [], "available": False}
        return {"models": sorted(str(r).removeprefix("models/") for r in rows if r), "available": True}


MODELS_PATH = {"anthropic_messages": "/v1/models", "google": "/v1beta/models"}
"""Model-list path under base_url. The OpenAI base URL already ends in /v1; the Anthropic and Google SDKs add their version."""


TEST_TOOL = {
    "name": "ping",
    "description": "Call this tool once with ok set to true.",
    "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
}


def probe_profile(
    profiles: ProfileService,
    profile_id: str,
    model: str | None,
    transport: httpx2.AsyncBaseTransport | None = None,
    thinking: bool | str | None = None,
) -> dict:
    """One small tool-call request. Costs one call, so the route refuses it unless paid calls are allowed. No output
    limit and the 300 s deadline of a seller call: a reasoning model may think long before the tool call (CR-89 addendum
    3). `thinking`: the unified setting, unset by default. A failure keeps the provider's code, HTTP status and short
    message (keys.mask_secrets with the stored key), never the request body."""
    rec = profiles.record(profile_id)
    model = model or rec.default_model
    if not model:
        raise ApiError(
            422, "validation_error", "Give a model or set default_model on the profile.", [{"path": "model", "message": "required"}]
        )
    key = profiles.keys.get(profile_id)
    if key is None:
        raise ApiError(422, "profile_key_missing", f"Provider profile {profile_id} has no key.")
    if not protocol_built(rec.protocol):
        raise ApiError(
            422, "protocol_not_built", f"Provider profile {profile_id} uses protocol {rec.protocol}, which is designed, not built."
        )
    profile = ProviderProfile(
        id=rec.id, base_url=rec.base_url, model=model, family="unknown", protocol=rec.protocol, session_header=rec.session_header
    )
    endpoint = Endpoint(profile, key, transport=transport, timeout_s=300, session=f"profile-test:{rec.id}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        reply, latency = endpoint.request(
            Request(
                messages=[{"role": "user", "content": "Call the ping tool."}],
                tools=[TEST_TOOL],
                allow_text=False,
                settings={"temperature": 0} | ({} if thinking is None else {"thinking": thinking}),
            )
        )
    except EndpointError as e:
        return {
            "ok": False,
            "model": model,
            "latency_ms": None,
            "supports": rec.supports.model_dump(),
            "error": {
                "kind": "other" if e.code == "provider_error" else e.code,
                "message": f"The endpoint call failed ({e.code}).",
                "http_status": e.http_status,
                "provider_code": e.provider_code,
                "provider_message": mask_secrets(e.message, (key,)) if e.message else None,
            },
        }
    supports = Supports(tool_calls=any(c.name == "ping" for c in reply.tool_calls), json_schema=rec.supports.json_schema, checked_at=now)
    if rec.source == "store":
        profiles._save(rec.model_copy(update={"supports": supports}))
    else:
        profiles.env[rec.id] = rec.model_copy(update={"supports": supports})
    return {"ok": True, "model": model, "latency_ms": latency, "supports": supports.model_dump(), "error": None}
