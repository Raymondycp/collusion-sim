"""WebSocket /api/ws/runs/{run_id}?from_seq=: stored events, caught_up, then live events (polled from the store).

The live view and the replay viewer use the same stream. Indicator subscriptions get one indicator_update per
settled round (stored rounds first). The socket closes with 1000 after run_ended.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from starlette.websockets import WebSocket, WebSocketDisconnect

import indicators
from adapters.store import RunsDB
from services import read

POLL_S = 0.2
PING_S = 15.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Subs:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.new: list[str] = []

    def handle(self, msg: dict) -> str | None:
        if msg.get("type") == "subscribe":
            for s in msg.get("subscriptions", []):
                if s.get("id") not in indicators.REGISTRY:
                    return f"unknown indicator {s.get('id')}"
                self.items[s["key"]] = {"id": s["id"], "params": s.get("params") or {}}
                self.new.append(s["key"])
        elif msg.get("type") == "unsubscribe":
            for k in msg.get("keys", []):
                self.items.pop(k, None)
        return None


def _updates(events, keys, subs: _Subs, only_t=None) -> list[dict]:
    view = indicators.SeriesView.from_events(events)
    out = []
    for key in keys:
        sub = subs.items.get(key)
        if sub is None:
            continue
        try:
            inst = read.indicator_body(indicators.instance(view, sub["id"], sub["params"]))
        except indicators.ParamsError:
            out.append({"type": "error", "code": "indicator_params_invalid", "message": f"bad params for {key}"})
            continue
        for i, t in enumerate(inst["t"]):
            if only_t is None or t == only_t:
                out.append(
                    {
                        "type": "indicator_update",
                        "key": key,
                        "indicator_id": sub["id"],
                        "t": t,
                        "values": {name: vals[i] for name, vals in inst["outputs"].items()},
                    }
                )
    return out


MALFORMED = object()


async def stream(ws: WebSocket, db: RunsDB, run_id: str, from_seq: int) -> None:
    await ws.accept()
    if db.run_config(run_id) is None and not db.events(run_id):
        await ws.close(code=4404, reason="run not found")
        return
    stored = await asyncio.to_thread(db.events, run_id)
    if from_seq > len(stored):
        await ws.close(code=4409, reason="from_seq is beyond the last stored seq + 1")
        return
    subs = _Subs()
    inbox: asyncio.Queue = asyncio.Queue()

    async def receive():
        try:
            while True:
                await inbox.put(await ws.receive_json())
        except ValueError:
            await inbox.put(MALFORMED)
        except (WebSocketDisconnect, RuntimeError):
            await inbox.put(None)

    reader = asyncio.create_task(receive())
    try:
        for e in stored[from_seq:]:
            await ws.send_json({"type": "event", "event": e.model_dump(mode="json")})
        await ws.send_json({"type": "caught_up", "last_seq": stored[-1].seq if stored else -1})
        events, sent = stored, len(stored)
        last_ping = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(POLL_S)
            fresh = await asyncio.to_thread(db.events, run_id, sent)
            for e in fresh:
                await ws.send_json({"type": "event", "event": e.model_dump(mode="json")})
                events = events + [e]
                if e.type == "reference":  # the last event of a round that an indicator reads (after settled)
                    for m in _updates(events, list(subs.items), subs, only_t=e.t):
                        await ws.send_json(m)
            sent += len(fresh)
            while not inbox.empty():
                msg = inbox.get_nowait()
                if msg is None:
                    return
                if msg is MALFORMED:
                    await ws.close(code=1007, reason="client message is not valid JSON")
                    return
                error = subs.handle(msg)
                if error:
                    await ws.send_json({"type": "error", "code": "unknown_indicator", "message": error})
            if subs.new:
                for m in _updates(events, subs.new, subs):
                    await ws.send_json(m)
                subs.new = []
            if events and events[-1].type == "run_ended":
                await ws.close(code=1000)
                return
            now = asyncio.get_running_loop().time()
            if now - last_ping >= PING_S:
                await ws.send_json({"type": "ping", "at": _now()})
                last_ping = now
    except WebSocketDisconnect:
        return
    finally:
        reader.cancel()
