"""The one HTTP client of the CLI. Every failure becomes a CliError that carries a contract-shaped Problem."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx2

DEFAULT_SERVER = "http://127.0.0.1:8000/api"


class CliError(Exception):
    """exit 1: a Problem from the API or a CLI check; exit 3: no usable answer from the server."""

    def __init__(self, code: str, message: str, status: int | None = None, errors: list | None = None, exit_code: int = 1, body=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.body = body or {"code": code, "status": status, "message": message, **({"errors": errors} if errors else {})}


class DryRun(Exception):
    """--dry-run: the request that a write command would send. Nothing was sent."""

    def __init__(self, method: str, path: str, body: Any, params: dict | None):
        super().__init__(f"{method} {path}")
        if isinstance(body, dict) and "api_key" in body:
            body = {**body, "api_key": "(not shown)"}
        self.payload = {"dry_run": True, "method": method, "path": "/" + path, "params": params or {}, "body": body}


def unreachable(message: str) -> CliError:
    return CliError("server_unreachable", message, exit_code=3)


def seg(value: Any) -> str:
    """One URL path segment. '/', '?' and '#' are encoded and '.' or '..' is refused, so an id cannot change the route (CR-3/CR-4 m1)."""
    text = str(value)
    if text in (".", ".."):
        raise CliError("cli_usage", f"{text!r} is not a valid id: a dot segment would change the route.", exit_code=2)
    return quote(text, safe="")


def hub_keys(body: dict) -> dict:
    """CLI-Hub keys (CLI-DESIGN §4.3): `error` copies `message`; `type` copies `code` unless the Problem has its own `type` URI."""
    return {**body, "error": body.get("message", ""), **({} if "type" in body else {"type": body.get("code")})}


class Api:
    def __init__(self, http: httpx2.Client, dry_run: bool = False):
        self.http = http
        self.dry_run = dry_run

    def _send(self, method: str, path: str, params: dict | None, json: Any) -> httpx2.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.dry_run and method != "GET":
            raise DryRun(method, path, json, params)
        try:
            resp = self.http.request(method, path, params=params, json=json)
        except httpx2.TransportError as e:
            raise unreachable(f"No answer from {self.http.base_url}: {type(e).__name__}.") from None
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and "code" in body:
                raise CliError(body["code"], body.get("message", ""), body=body)
            raise unreachable(f"{method} {path} gave HTTP {resp.status_code} with no problem body.")
        return resp

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        resp = self._send(method, path, params, json)
        try:
            return resp.json() if resp.content else None
        except ValueError:
            raise unreachable(f"{method} {path} gave HTTP {resp.status_code} with a body that is not JSON.") from None

    def raw(self, path: str, **params) -> bytes:
        return self._send("GET", path, params, None).content

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params) -> Any:
        return self.request("POST", path, params=params, json=body)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, json=body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, json=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def connect(server: str, timeout: float) -> httpx2.Client:
    return httpx2.Client(base_url=server, timeout=timeout)
