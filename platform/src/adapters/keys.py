"""API keys, server side only. Read from the process environment, then `.env`.

The key for store profile P is the env var key_env(P) = SIM_KEY_<P in upper case>, or its entry in the OS keyring when
the server runs with SIM_KEYRING=1 (opt-in: the sprint path is `.env`). The key of a .env profile is only its own
variable SIM_PROFILE_<ID>_KEY (registered with use_env_key). `.env` is read, never written. No method returns a key to
a caller outside the LLM adapter; the API sees only status().
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DEFAULT_SERVICE = "collusion-sim"


class Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def os_keyring() -> Backend | None:
    """The OS keyring, only with SIM_KEYRING=1. Off by default: a headless or locked desktop session makes it fail, and
    the team's keys already live in `.env` (handoff simplify-cut B4)."""
    if os.environ.get("SIM_KEYRING", "0") != "1":
        return None
    import keyring

    return keyring


def key_env(profile_id: str) -> str:
    return "SIM_KEY_" + re.sub(r"[^A-Z0-9]", "_", profile_id.upper())


def hint(key: str) -> str:
    """First 3 and last 4 characters. A key shorter than 20 characters shows only the ellipsis (7 of 12 would be too many)."""
    return f"{key[:3]}…{key[-4:]}" if len(key) >= 20 else "…"


TOKEN = re.compile(r"[A-Za-z0-9_\-.~+/=]{16,}")


SECRET_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])sk[-_][A-Za-z0-9_\-*.…]{5,}"  # sk- and sk_ keys, masked echoes ("sk-proj-ABCD****wxyz", "…wxyz")
    r"|[A-Za-z0-9]{2,}\*{4,}[A-Za-z0-9]*"  # other masked echoes
    r"|Bearer\s+\S+"
)
RUN = re.compile(r"[A-Za-z0-9_\-.]+")
"""A run that may be a token; a run is masked when it looks like a key and not like a model id (_keylike)."""
WINDOW = 8
"""CR-89 addendum 2 point 1: a provider may echo part of the stored key; any 8 characters of it mark the run that holds them."""


def _keylike(run: str) -> bool:
    """A bare run of 32 or more letters and digits, a JWT (eyJ...), or a run of 24 or more characters with at least 3
    digits and a part of 16 or more characters between separators (a Google key). Model ids have short parts."""
    if len(run) >= 32 and run.isalnum() or run.startswith("eyJ") and len(run) >= 24:
        return True
    parts = re.split(r"[-_.]", run)
    return len(run) >= 24 and sum(c.isdigit() for c in run) >= 3 and max(len(x) for x in parts) >= 16


def mask_secrets(text: str, secrets: tuple[str, ...] = (), limit: int = 160) -> str:
    """Provider text for an API answer (CR-89 addenda 1 to 3): key-shaped strings, then the given secrets, their hints
    and any run that holds 8 characters of one, become [redacted]; model ids and other names stay. Cut to limit after
    masking, so no secret is cut in half first."""
    # The shapes first (CR-89 addendum 3): a masked echo of the stored key is then removed whole, not cut by a window.
    text = SECRET_SHAPES.sub("[redacted]", text)
    for secret in (s for s in secrets if s):
        text = text.replace(secret, "[redacted]")
        if len(secret) >= 20:
            text = text.replace(hint(secret), "[redacted]")
        windows = {secret[i : i + WINDOW] for i in range(len(secret) - WINDOW + 1)}
        text = RUN.sub(lambda m: "[redacted]" if any(w in m.group() for w in windows) else m.group(), text)
    return RUN.sub(lambda m: "[redacted]" if _keylike(m.group()) else m.group(), text)[:limit]


def redact(text: str, limit: int = 300) -> str:
    """Text for a server log: every run of 16 or more key-like characters becomes [redacted], then cut to limit."""
    return TOKEN.sub("[redacted]", text)[:limit]


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; # comments, blank lines and an optional 'export ' prefix are allowed. Quotes around a value are removed."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[m.group(1)] = value
    return out


@dataclass(frozen=True)
class KeyStatus:
    key_set: bool
    key_store: Literal["keyring", "env"]
    key_hint: str | None


def env_value(name: str, env_file: Path) -> str | None:
    """One variable from the process environment, else from .env. Read by name only."""
    return os.environ.get(name) or read_env_file(env_file).get(name) or None


class KeyStore:
    def __init__(self, env_file: Path, service: str | None = None, backend: Backend | None = None) -> None:
        self.env_file = Path(env_file)
        self.service = service or os.environ.get("SIM_KEYRING_SERVICE", DEFAULT_SERVICE)
        self.backend = backend if backend is not None else os_keyring()
        self._env_keys: dict[str, str] = {}

    @property
    def store_enabled(self) -> bool:
        """True when a key can be written: an explicit backend (tests) or SIM_KEYRING=1."""
        return self.backend is not None

    def use_env_key(self, profile_id: str, variable: str) -> None:
        """A .env profile: its key is this variable only."""
        self._env_keys[profile_id] = variable

    def sources(self, profile_id: str) -> list[str]:
        """Where a key for a store profile is set: "keyring" and/or the variable name. Names only, never a value."""
        out = ["keyring"] if self.backend is not None and self.backend.get_password(self.service, profile_id) else []
        name = key_env(profile_id)
        if os.environ.get(name):
            out.append(f"{name} (environment)")
        elif read_env_file(self.env_file).get(name):
            out.append(f"{name} (.env)")
        return out

    def put(self, profile_id: str, key: str) -> None:
        if self.backend is None:
            raise RuntimeError("No key store: the server runs without a keyring (SIM_KEYRING=0).")
        self.backend.set_password(self.service, profile_id, key)

    def delete(self, profile_id: str) -> None:
        if self.backend is None:
            return
        if self.backend.get_password(self.service, profile_id) is not None:
            self.backend.delete_password(self.service, profile_id)

    def _lookup(self, profile_id: str) -> tuple[str | None, Literal["keyring", "env"]]:
        if profile_id in self._env_keys:
            return env_value(self._env_keys[profile_id], self.env_file), "env"
        key = self.backend.get_password(self.service, profile_id) if self.backend is not None else None
        if key:
            return key, "keyring"
        return env_value(key_env(profile_id), self.env_file), "env"

    def get(self, profile_id: str) -> str | None:
        """For the LLM adapter only. Never log, store or return the result."""
        return self._lookup(profile_id)[0]

    def status(self, profile_id: str) -> KeyStatus:
        key, where = self._lookup(profile_id)
        return KeyStatus(key_set=key is not None, key_store=where if key else "keyring", key_hint=hint(key) if key else None)
