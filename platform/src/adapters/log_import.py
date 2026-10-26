"""Log import (QUEUE "Log import"; DECISIONS-14 §3.3): conversation logs from outside the simulator to an episode.v5 and
its trail.jsonl, for post-hoc analysis (F6; the brief's "evidence from logged trajectories").

Inputs:
- A Pi-agent session export (`~/.pi/agent/sessions/.../*.jsonl`), one file per seller. Each assistant message is one
  call: its thinking, answer text and tool calls. The tools `post` (channel_id, text), `set_price` (price) and `hold`
  are the seller's moves, as in the simulator. The round is the "Round N" of the last user text before the call; without
  one, it is 1 + the seller's earlier set_price or hold calls.
- Rows (JSON list, or CSV with a header): `speaker`, `round`, and optional `text`, `channel` (default public), `action`
  (a price, or "hold"), `quantity`, `profit`, `thinking`, `model`.

The episode holds what a log shows, as tier-A events only: `posted`, `acted` and `settled` (the prices of the sellers
that acted; quantities and profits only when the log gives them). `run_started` and `run_ended` frame it, as in every
episode. No observation, reference or llm_call event: the log has no market model and no request record. The header
says so: kind exploratory, split exploratory, policy_kind `imported`, scenario id `imported` with the sources' hash.
The trail holds one trail.v1 line per call (Pi) or per row with text or thinking, with `seq` null (there is no llm_call
event) and `source` naming the input format; key-like strings in its text are redacted before it is stored (CR-76
minor 2). The run id comes from the sources' hash, so the same files give the same run id and the same bytes.

Each seller gets the grants a run needs to hold the moves: set_price, hold, and post on the channels it posted on
(CR-76 major 1). A private channel id names its two sellers in sorted order.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from adapters.trail import EVENT_FIELDS, TRAIL_VERSION
from features import REGISTRY
from features.table import compute
from simcore import __version__
from simcore.recorder import MemorySink, Recorder
from simschema.episode import episode_lines, read_episode
from simschema.events import (
    ActedEvent,
    AgentHeader,
    Capability,
    ChannelInfo,
    EpisodeHeader,
    PostedEvent,
    RunEndedEvent,
    RunStartedEvent,
    ScenarioRef,
    SettledEvent,
    ToolGrant,
)

AGENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
ROUND = re.compile(r"\bRound (\d+)\b")
B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
FINISH = {"stop": "stop", "toolUse": "tool_call", "length": "length", "error": "error", "aborted": "error"}
MAX_ROUNDS = 1000
KEYLIKE = re.compile(r"sk-[A-Za-z0-9_-]{8,}|(?i:(api[_-]?key[\"']?\s*[:=]\s*[\"']?))[^\"'\s,}]+")
REDACTED = "[redacted]"


class LogImportError(ValueError):
    pass


@dataclass
class Turn:
    """One seller's moves and trail in one round, from one call (Pi) or one row."""

    agent: str
    t: int
    posts: list[tuple[str, str]] = field(default_factory=list)
    price: float | None = None
    hold: bool = False
    quantity: float | None = None
    profit: float | None = None
    trail: dict | None = None


@dataclass(frozen=True)
class Imported:
    run_id: str
    header: EpisodeHeader
    lines: list[str]
    trail: list[dict]

    def episode_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def trail_text(self) -> str:
        """The trail.jsonl bytes, with key-like strings redacted: a log can hold a key that a tool echoed (CR-76 minor 2)."""
        return "".join(json.dumps(_redacted(line), sort_keys=True, ensure_ascii=False) + "\n" for line in self.trail)


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return KEYLIKE.sub(lambda m: (m.group(1) or "") + REDACTED, value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


def _agent(name: Any) -> str:
    agent = str(name).strip()
    if not AGENT_ID.match(agent):
        raise LogImportError(f"speaker {agent!r} is not an agent id (a letter, then up to 15 letters, digits or _)")
    return agent


def _amount(value: Any, what: str) -> float:
    """A price or quantity: a finite number, 0 or more."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise LogImportError(f"{what} {value!r} is not a number") from None
    if not math.isfinite(x) or x < 0:
        raise LogImportError(f"{what} {value!r} is not a finite number of 0 or more")
    return x


def _round(value: Any, what: str) -> int:
    try:
        t = int(value)
    except (TypeError, ValueError):
        raise LogImportError(f"{what}: round {value!r} is not a whole number") from None
    if not 1 <= t <= MAX_ROUNDS:
        raise LogImportError(f"{what}: round {t} is not in 1..{MAX_ROUNDS}")
    return t


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _trail_line(source: str, agent: str, t: int, model: str | None, messages: list, finish: str | None, usage: dict) -> dict:
    line = dict.fromkeys(EVENT_FIELDS) | {"t": float(t), "agent_id": agent, "purpose": "act", "attempt": 1, "sample_index": 0}
    line |= {"model": model, "cache_hit": False, "charged": False} | usage
    return line | {
        "trail": TRAIL_VERSION,
        "source": source,
        "paid_by_run": None,
        "finish_reason": finish,
        "allow_text": None,
        "settings": None,
        "tools": None,
        "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
        "missing": None,
    }


# Pi-agent session export


def _pi_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def from_pi(agent: str, text: str) -> tuple[list[Turn], str]:
    """The turns of one seller's Pi session, and the session's start time."""
    agent = _agent(agent)
    entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not entries or entries[0].get("type") != "session":
        raise LogImportError(f"{agent}: not a Pi session export (line 1 must be type session)")
    started = entries[0].get("timestamp") or "1970-01-01T00:00:00Z"
    turns, request, last_user, acted, model = [], [], "", 0, None
    for entry in entries[1:]:
        if entry.get("type") == "model_change":
            model = entry.get("modelId") or model
        if entry.get("type") != "message":
            continue
        m, at = entry["message"], _at(entry.get("timestamp") or started)
        if m["role"] == "user":
            last_user = _pi_text(m.get("content"))
            request.append(UserPromptPart(content=last_user, timestamp=at))
        elif m["role"] == "toolResult":
            request.append(
                ToolReturnPart(
                    tool_name=m.get("toolName", ""), content=_pi_text(m.get("content")), tool_call_id=m.get("toolCallId", ""), timestamp=at
                )
            )
        elif m["role"] == "assistant":
            found = ROUND.search(last_user)
            turn = Turn(agent, _round(found.group(1) if found else acted + 1, agent))
            parts: list = []
            for p in m.get("content") or []:
                if p.get("type") == "thinking" and p.get("thinking"):
                    parts.append(ThinkingPart(content=p["thinking"]))
                elif p.get("type") == "text" and p.get("text"):
                    parts.append(TextPart(content=p["text"]))
                elif p.get("type") == "toolCall":
                    args = p.get("arguments") or {}
                    parts.append(ToolCallPart(tool_name=p["name"], args=args, tool_call_id=p.get("id", "")))
                    if p["name"] == "post" and args.get("text"):
                        turn.posts.append((str(args.get("channel_id") or "public"), str(args["text"])))
                    elif p["name"] == "set_price" and args.get("price") is not None:
                        turn.price = _amount(args["price"], f"{agent}: set_price price")
                    elif p["name"] == "hold":
                        turn.hold = True
            u = m.get("usage") or {}
            usage = {
                "input_tokens": u.get("input"),
                "output_tokens": u.get("output"),
                "cached_input_tokens": u.get("cacheRead"),
                "reasoning_tokens": u.get("reasoning"),
                "cost_usd": (u.get("cost") or {}).get("total"),
            }
            model = m.get("model") or model
            reply = ModelResponse(
                parts=parts,
                usage=RequestUsage(input_tokens=u.get("input") or 0, output_tokens=u.get("output") or 0),
                model_name=model,
                timestamp=at,
                finish_reason=FINISH.get(m.get("stopReason")),
            )
            sent = [ModelRequest(parts=request, timestamp=at)] if request else []
            finish = FINISH.get(m.get("stopReason"))
            turn.trail = _trail_line("pi", agent, turn.t, model, [*sent, reply], finish, usage) | {"error": m.get("errorMessage")}
            turns.append(turn)
            request = []
            acted += turn.price is not None or turn.hold
    return turns, started


# Rows: JSON or CSV


def read_rows(name: str, data: bytes) -> list[dict]:
    """A .csv file by its name, else JSON."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LogImportError(f"{name}: not UTF-8 text") from None
    if name.lower().endswith(".csv"):
        return list(csv.DictReader(io.StringIO(text)))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LogImportError(f"{name}: not JSON ({e.msg}, line {e.lineno})") from None
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise LogImportError("a JSON log is a list of rows, or an object with a rows list")
    return rows


def _number(row: dict, key: str, i: int) -> float | None:
    """An optional number of a row. A price (action) and a quantity are finite and 0 or more; a profit is finite."""
    value = row.get(key)
    if value in (None, ""):
        return None
    if key != "profit":
        return _amount(value, f"row {i}: {key}")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise LogImportError(f"row {i}: {key} {value!r} is not a number") from None
    if not math.isfinite(x):
        raise LogImportError(f"row {i}: {key} {value!r} is not a finite number")
    return x


def from_rows(rows: list[dict], created_at: str) -> list[Turn]:
    turns = []
    for i, row in enumerate(rows):
        if not row.get("speaker") or row.get("round") in (None, ""):
            raise LogImportError(f"row {i}: speaker and round are required")
        t = _round(row["round"], f"row {i}")
        turn = Turn(_agent(row["speaker"]), t, quantity=_number(row, "quantity", i), profit=_number(row, "profit", i))
        if text := (row.get("text") or "").strip():
            turn.posts.append((str(row.get("channel") or "public"), text))
        action = row.get("action")
        if isinstance(action, str) and action.strip().lower() == "hold":
            turn.hold = True
        elif action not in (None, ""):
            turn.price = _number(row, "action", i)
        thinking = (row.get("thinking") or "").strip()
        if thinking or text:
            parts = ([ThinkingPart(content=thinking)] if thinking else []) + ([TextPart(content=text)] if text else [])
            reply = ModelResponse(parts=parts, model_name=row.get("model") or None, timestamp=_at(created_at))
            turn.trail = _trail_line("rows", turn.agent, t, row.get("model") or None, [reply], None, {})
        turns.append(turn)
    return turns


# Turns to an episode


def _channel(cid: str, agents: list[str]) -> ChannelInfo:
    """The channel of a log's channel id; private_B_A is private_A_B, the id a run gives that pair."""
    if cid == "public":
        return ChannelInfo(id=cid, kind="public", participants=agents)
    pair = cid.removeprefix("private_").split("_")
    if cid.startswith("private_") and len(pair) == 2 and len(set(pair)) == 2 and set(pair) <= set(agents):
        a, b = sorted(pair)
        return ChannelInfo(id=f"private_{a}_{b}", kind="private", participants=[a, b])
    raise LogImportError(f"channel {cid!r}: use public or private_<A>_<B> with two speakers of the log")


def _grants(agent: str, turns: list[Turn], channel: dict[str, str]) -> Capability:
    posted = sorted({channel[c] for x in turns if x.agent == agent for c, _ in x.posts})
    post = [ToolGrant(tool_id="post", params={"channels": posted})] if posted else []
    return Capability(grants=[ToolGrant(tool_id="set_price"), ToolGrant(tool_id="hold"), *post])


def build(turns: list[Turn], sources: list[bytes], created_at: str, models: dict[str, str | None] | None = None) -> Imported:
    """The episode and trail of the turns. `sources`: the input files' bytes, for the run id and the scenario hash. A
    value the episode schema refuses is a LogImportError, as any other bad input."""
    try:
        return _build(turns, sources, created_at, models or {})
    except ValidationError as e:
        first = e.errors()[0]
        raise LogImportError(f"{'.'.join(map(str, first['loc'])) or 'log'}: {first['msg']}") from None


def _build(turns: list[Turn], sources: list[bytes], created_at: str, models: dict[str, str | None]) -> Imported:
    if not turns:
        raise LogImportError("the log has no turn")
    digest = hashlib.sha256(b"\0".join(sources)).digest()
    n = int.from_bytes(digest[:17], "big") >> 6
    run_id = "run_" + "".join(B32[(n >> (5 * i)) & 31] for i in reversed(range(26)))
    agents = sorted({t.agent for t in turns})
    if len(agents) < 2:
        raise LogImportError("a log needs at least two speakers")
    rounds = max(t.t for t in turns)
    named = {c: _channel(c, agents) for c in {c for t in turns for c, _ in t.posts}}
    channel = {c: info.id for c, info in named.items()}
    channels = sorted({info.id: info for info in named.values()}.values(), key=lambda c: c.id)
    header = EpisodeHeader(
        run_id=run_id,
        kind="exploratory",
        split="exploratory",
        seed=0,
        scenario=ScenarioRef(id="imported", hash="sha256:" + digest.hex(), demand_model="unknown", demand_version="0"),
        agents=[AgentHeader(id=a, policy_kind="imported", capability=_grants(a, turns, channel), model=models.get(a)) for a in agents],
        seller_order=agents,
        channels=channels,
        call_mode="talk_and_act" if channels else "one_call",
        max_rounds=rounds,
        reproducible=False,
        simcore_version=__version__,
        created_at=created_at,
    )
    sink = MemorySink()
    rec = Recorder(run_id, [sink], now=lambda: created_at)
    rec.emit(RunStartedEvent, 0, header=header)
    by_channel = {c.id: c for c in channels}
    last: dict[str, float] = {}
    by_round: dict[int, list[Turn]] = {}
    for x in turns:
        by_round.setdefault(x.t, []).append(x)
    for t in range(1, rounds + 1):
        now = by_round.get(t, [])
        for a in agents:
            for cid, text in ((channel[c], text) for x in now if x.agent == a for c, text in x.posts):
                receivers = [] if t == rounds else [r for r in by_channel[cid].participants if r != a]
                rec.emit(PostedEvent, t, agent_id=a, channel_id=cid, text=text, deliver_t=t + 1, receivers=receivers)
        prices, quantities, profits = {}, {}, {}
        for a in agents:
            mine = [x for x in now if x.agent == a]
            move = next((x for x in reversed(mine) if x.price is not None or x.hold), None)
            if move is not None and (move.price is not None or a in last):
                price = move.price if move.price is not None else last[a]
                rec.emit(ActedEvent, t, agent_id=a, price=price, hold=move.price is None)
                prices[a] = last[a] = price
            for x in mine:
                if x.quantity is not None:
                    quantities[a] = x.quantity
                if x.profit is not None:
                    profits[a] = x.profit
        if prices:
            rec.emit(SettledEvent, t, prices=prices, quantities=quantities, profits=profits)
    rec.emit(RunEndedEvent, rounds, status="completed", end_reason="horizon", episode_hash=rec.episode_hash)
    lines = episode_lines(sink.events)
    read_episode(lines)
    trail = [x.trail | {"run_id": run_id} for x in sorted(turns, key=lambda x: (x.t, agents.index(x.agent))) if x.trail]
    return Imported(run_id, header, lines, trail)


def import_pi(sessions: dict[str, bytes]) -> Imported:
    """One Pi session export per seller, by agent id."""
    turns, starts, sources, models = [], [], [], {}
    for agent, data in sorted(sessions.items()):
        try:
            text = data.decode("utf-8")
            mine, started = from_pi(agent, text)
        except UnicodeDecodeError:
            raise LogImportError(f"{agent}: not UTF-8 text") from None
        except json.JSONDecodeError as e:
            raise LogImportError(f"{agent}: a line is not JSON ({e.msg})") from None
        turns += mine
        starts.append(started)
        sources.append(data)
        models[agent] = next((x.trail["model"] for x in reversed(mine) if x.trail and x.trail["model"]), None)
    return build(turns, sources, min(starts), models)


def import_rows(name: str, data: bytes, created_at: str) -> Imported:
    """One JSON or CSV rows file (CSV by the .csv name)."""
    rows = read_rows(name, data)
    if not all(isinstance(r, dict) for r in rows):
        raise LogImportError(f"{name}: each row is an object")
    models = {_agent(r["speaker"]): r.get("model") or None for r in rows if r.get("speaker") and r.get("model")}
    return build(from_rows(rows, created_at), [data], created_at, models)


# Analysis


def feature_row(imported: Imported) -> dict:
    """Every registered feature on the imported episode: log features are computed on the tier-A events (the layer-A
    export); a feature with access model or intervention is missing with reason access_<class>, since a log has no payoff
    model and no branch."""
    header, events = read_episode(imported.lines)
    out: dict = {"run_id": imported.run_id}
    for i in REGISTRY.ids():
        cls = REGISTRY[i]
        value, reason = (None, f"access_{cls.access}") if cls.access != "log" else compute(cls, header.seller_order, events, [], None, [])
        out[i], out[f"{i}_missing"] = value, reason
    return out
