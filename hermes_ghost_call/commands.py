"""``/ghost`` slash command. Typed by the human, so it is the only place Intervene can be allowed.

Subcommands: start, status, speak, intervene allow|deny, hangup, transcript, log, check.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from . import cli, safety, tools
from . import hints as hintsmod
from .session import ENDED, LIVE

log = logging.getLogger(__name__)

USAGE = ("Usage: /ghost start [room|+number] <goal> | status [id] | speak [hint] | "
         "intervene allow|deny [id] | hangup [id] | transcript [id] | log [n] | check")
ARGS_HINT = ("start [room|+number] <goal> | status [id] | speak [hint] | intervene allow|deny "
             "[id] | hangup [id] | transcript [id] | log [n] | check")


def ghost_command(raw_args: str = "", **kwargs: Any) -> str:
    try:
        parts = (raw_args or "").split()
        if not parts:
            return USAGE
        sub, rest = parts[0].lower(), parts[1:]
        handler = _SUB.get(sub)
        if handler is None:
            return USAGE
        return handler(rest, kwargs)
    except Exception:  # noqa: BLE001
        log.exception("hermes-ghost-call command failed")
        return "hermes-ghost-call: that command failed internally."


def _who(kwargs: dict) -> str:
    return str(kwargs.get("user_id") or kwargs.get("session_key") or kwargs.get("platform")
               or "local")


def _start(rest: list[str], kw: dict) -> str:
    # "/ghost start room <goal>"         -> speakerphone mode
    # "/ghost start +15551234567 <goal>" -> Twilio mode, that number is the other party
    mode, callee_number = "", ""
    while rest:
        head = rest[0].strip()
        if head.lower() in ("room", "speaker", "speakerphone", "--room"):
            mode, rest = "room", rest[1:]
        elif re.fullmatch(r"\+\d{6,15}", head):
            mode, callee_number, rest = "twilio", head, rest[1:]
        else:
            break
    goal = " ".join(rest).strip()
    label = ""
    if " with " in f" {goal} ":  # "/ghost start book a table with Bella Napoli" -> label
        head, _, tail = goal.rpartition(" with ")
        if head and tail and len(tail) <= 60:
            label = tail.strip()
    out = tools.get_service().start(goal=goal, callee_label=label, mode=mode,
                                    callee_number=callee_number,
                                    origin_session=str(kw.get("session_id") or ""))
    if not out.get("ok"):
        return f"\U0001F47B {out.get('error')}"
    lines = [f"\U0001F47B Ghost {out['session_id']} is ready ({out['mode']} mode, "
             f"hints: {out['hint_mode']}). Ghost never joins the call."]
    links = out.get("links") or {}
    if "them" in links:
        lines.append(f"You (open with headphones): {links['you']}")
        lines.append(f"Them (send to the other party): {links['them']}")
    elif "you" in links:
        lines.append(f"Listening page (open next to the call on speaker): {links['you']}")
    lines.append(out.get("instructions", ""))
    for key in ("note", "warning"):
        if out.get(key):
            lines.append(out[key])
    return "\n".join(x for x in lines if x)


def _status(rest: list[str], kw: dict) -> str:
    out = tools.get_service().status(rest[0] if rest else None)
    if not out.get("ok"):
        return f"\U0001F47B {out.get('error')}"
    iv = "on" if out["intervene_allowed"] else "off"
    tail = f" · ended ({out['end_reason']})" if out["state"] == ENDED else ""
    return (f"\U0001F47B {out['session_id']} · {out['state']} · {out['mode']} · "
            f"{out['duration']} · {out['hint_count']} hints · intervene {iv}{tail}")


def _speak(rest: list[str], kw: dict) -> str:
    arg = rest[0] if rest else None
    out = tools.HANDLERS["ghost_speak"]({"hint_id": arg} if arg else {})
    data = json.loads(out)
    if not data.get("ok"):
        return f"\U0001F47B {data.get('error')}"
    return "\U0001F47B whispering to your earpiece"


def _intervene(rest: list[str], kw: dict) -> str:
    if not rest or rest[0].lower() not in ("allow", "deny"):
        return "Usage: /ghost intervene allow|deny [id]"
    svc = tools.get_service()
    session = svc.find(rest[1] if len(rest) > 1 else None)
    if session is None:
        return "\U0001F47B no ghost session."
    allow = rest[0].lower() == "allow"
    if allow:
        if (session.get("provider") or {}).get("remote") or session.get("mode") == "phone":
            return "\U0001F47B Intervene is disabled on the shared call service."
        if safety.is_unattended(svc.ctx, kw):
            return "\U0001F47B Intervene cannot be allowed in an unattended run."
        if session["state"] not in (LIVE, "created"):
            return f"\U0001F47B {session['id']} has ended."
    svc.store.set_intervene(session["id"], allow, by=_who(kw))
    if allow:
        return (f"\U0001F47B Intervene ON for {session['id']} (this session only). Hermes may now "
                "speak to the other party when you ask; its first words disclose it is an AI "
                f"assistant. /ghost intervene deny {session['id']} turns it off.")
    return f"\U0001F47B Intervene OFF for {session['id']}."


def _hangup(rest: list[str], kw: dict) -> str:
    out = tools.get_service().hangup(rest[0] if rest else None)
    if not out.get("ok"):
        return f"\U0001F47B {out.get('error')}"
    return f"\U0001F47B {out['session_id']} ended. The summary follows."


def _transcript(rest: list[str], kw: dict) -> str:
    svc = tools.get_service()
    session = svc.find(rest[0] if rest else None)
    if session is None:
        return "\U0001F47B no ghost session."
    if not (session.get("config") or {}).get("store_transcripts", True):
        return "\U0001F47B transcripts are off (store_transcripts: false)."
    turns = svc.store.turns(session["id"])
    if not turns:
        return f"\U0001F47B {session['id']}: nothing transcribed yet."
    lines = [f"{hintsmod.stamp(t['t'])} {t['speaker']}: {t['text']}" for t in turns[-60:]]
    return f"\U0001F47B transcript {session['id']}\n" + "\n".join(lines)


def _log(rest: list[str], kw: dict) -> str:
    try:
        n = max(1, min(int(rest[0]), 20)) if rest else 5
    except ValueError:
        n = 5
    svc = tools.get_service()
    rows = svc.store.recent_sessions(n)
    if not rows:
        return "\U0001F47B no ghost sessions yet."
    out = []
    for s in rows:
        summ = s.get("summary") or {}
        agreed = summ.get("agreed") or ""
        out.append(f"{s['id']} · {s['state']} · {s['mode']} · {svc.store.hint_count(s['id'])} "
                   f"hints · {s['goal'][:60]}" + (f" → {agreed[:60]}" if agreed else ""))
    return "\U0001F47B recent sessions\n" + "\n".join(out)


def _check(rest: list[str], kw: dict) -> str:
    svc = tools.get_service()
    results = cli.run_checks(svc.environ, svc.config())
    lines = [f"[{'ok' if ok else '!!'}] {name}: {detail}" for name, ok, detail in results]
    return "\U0001F47B ghost check (names only, never values)\n" + "\n".join(lines)


def _summary(rest: list[str], kw: dict) -> str:
    out = tools.get_service().summary(rest[0] if rest else None, force=True)
    if not out.get("ok"):
        return f"\U0001F47B {out.get('error')}"
    return out["text"]


_SUB: dict[str, Callable[[list[str], dict], str]] = {
    "start": _start, "status": _status, "speak": _speak, "intervene": _intervene,
    "hangup": _hangup, "transcript": _transcript, "log": _log, "check": _check,
    "summary": _summary,
}
