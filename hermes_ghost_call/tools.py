"""Tool handlers and the plugin-side service (store, config, environment, ticker watchers).

No handler ever asks the user anything: there is no ``ask_choice``, no approval card and no
prepare/approve pair. ``ghost_start`` creates the session and returns the join links; the slash
command or the tool call *is* the consent to listen. The only gated tool is ``ghost_intervene``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import config as configmod
from . import hints as hintsmod
from . import mode as modemod
from . import remote as remotemod
from . import safety
from . import secrets as secretsmod
from .session import CREATED, ENDED, LIVE, Store, elapsed

log = logging.getLogger(__name__)

LINK_TTL_S = 600.0
MAX_GOAL_CHARS = 400
MAX_LABEL_CHARS = 80
HANGUP_WAIT_S = 1.2
DAY_S = 86400.0


class GhostService:
    def __init__(self, *, ctx: Any = None, store: Store | None = None,
                 environ: Mapping[str, str] | None = None,
                 clock: Callable[[], float] | None = None, start_watchers: bool = True) -> None:
        self.ctx = ctx
        self._store = store
        self._environ = environ
        self.clock = clock
        self.start_watchers = start_watchers
        self.watchers: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._remote: remotemod.RemoteBridge | None = None

    @property
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = Store(clock=self.clock)
            return self._store

    @property
    def environ(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else secretsmod.Environ()

    @property
    def remote(self) -> remotemod.RemoteBridge | None:
        """The shared bridge client when GHOST_CALL_BRIDGE_URL and a credential are set, else None
        (upstream behavior: a co-located bridge on the shared SQLite file)."""
        env = self.environ
        if not remotemod.configured(env):
            return None
        if self._remote is None:
            self._remote = remotemod.RemoteBridge(env)
        return self._remote

    def secret(self, name: str) -> str:
        return secretsmod.get(name, self._environ)

    def config(self) -> dict[str, Any]:
        getter = getattr(self.ctx, "get_config", None) if self.ctx is not None else None
        return configmod.resolve(getter)

    def now(self) -> float:
        return self.clock() if self.clock else time.time()

    # -- sessions --
    def expire_stale(self) -> None:
        now = self.now()
        for s in self.store.active_sessions():
            if s["state"] == CREATED and now - s["created_at"] > LINK_TTL_S + 30:
                self.store.end_once(s["id"], "link_expired")
            elif s["state"] == LIVE and elapsed(s, now) > \
                    float((s.get("config") or {}).get("max_duration_s", 900)) + 60:
                # The bridge enforces the cap; this covers a bridge that died mid-call.
                self.store.end_once(s["id"], "duration_cap")

    def find(self, sid: str | None) -> dict | None:
        sid = (sid or "").strip()
        if sid:
            return self.store.get_session(sid)
        active = self.store.active_sessions()
        return active[-1] if active else self.store.latest_session()

    def links(self, session: dict, tokens: dict[str, str]) -> dict[str, str]:
        base = self.secret("PUBLIC_BASE_URL").strip().rstrip("/")
        return {role: f"{base}/g/{session['id']}/{role}?t={tok}" for role, tok in tokens.items()}

    def start(self, *, goal: str, callee_label: str = "", mode: str = "",
              callee_number: str = "", origin_session: str = "") -> dict:
        goal = " ".join(str(goal or "").split())[:MAX_GOAL_CHARS]
        if not goal:
            return {"ok": False, "error": "a goal is required, e.g. 'book a table Friday 8pm "
                                          "for 4, budget $80, name Winch'"}
        label = " ".join(str(callee_label or "").split())[:MAX_LABEL_CHARS]
        cfg = self.config()
        self.expire_stale()
        active = self.store.active_sessions()
        if active:
            return {"ok": False, "error": f"ghost session {active[-1]['id']} is still active. "
                                          f"End it with /ghost hangup {active[-1]['id']}."}
        if self.store.sessions_since(self.now() - DAY_S) >= cfg["daily_session_limit"]:
            return {"ok": False, "error": "daily ghost session limit reached "
                                          f"({cfg['daily_session_limit']} per 24 h)."}
        if self.remote is not None:
            return self._start_remote(goal=goal, label=label, mode=mode, cfg=cfg,
                                      callee_number=callee_number,
                                      origin_session=origin_session)
        env = self.environ
        numbers = bool(cfg["user_number"] and callee_number)
        chosen = modemod.resolve(env, mode, has_numbers=numbers)
        if not self.secret("PUBLIC_BASE_URL").strip():
            return {"ok": False, "error": "PUBLIC_BASE_URL is not set; the bridge links need "
                                          "it. Run /ghost check."}
        provider: dict[str, str] = {}
        if chosen.name == modemod.TWILIO:
            for leg, number in (("user_number", cfg["user_number"]),
                                ("callee_number", callee_number)):
                check = safety.check_dialable(number, env)
                if not check.ok:
                    return {"ok": False, "error": f"Twilio mode refused the {leg}: "
                                                  f"{check.reason}."}
                provider[leg] = safety.validate_number(number)[0] or ""
        session = self.store.create_session(goal=goal, callee_label=label, mode=chosen.name,
                                            config=cfg, origin_session=origin_session,
                                            provider=provider)
        out: dict[str, Any] = {"ok": True, "session_id": session["id"], "mode": chosen.name,
                               "hint_mode": cfg["hint_mode"],
                               "max_duration_s": cfg["max_duration_s"]}
        if chosen.note:
            out["note"] = chosen.note
        if chosen.name == modemod.WEB:
            tokens = {role: self.store.mint_token(session["id"], role, "link", LINK_TTL_S)
                      for role in ("you", "them")}
            out["links"] = self.links(session, tokens)
            out["instructions"] = ("Open 'you' yourself with headphones; send 'them' to the "
                                   "other party. Each link works once, for 10 minutes.")
        elif chosen.name == modemod.ROOM:
            tokens = {"you": self.store.mint_token(session["id"], "you", "link", LINK_TTL_S)}
            out["links"] = self.links(session, tokens)
            out["instructions"] = ("Open the link on a laptop or tablet, put your phone call on "
                                   "speaker next to it, and tap Start listening. Ghost hears "
                                   "both sides through that one mic; hints show on the page. "
                                   "The link works once, for 10 minutes.")
        else:
            out["instructions"] = ("Twilio will ring your phone first; pick up and the other "
                                   "party is dialed into the call. Ghost only listens.")
        if not self.secret("OPENAI_API_KEY"):
            out["warning"] = "OPENAI_API_KEY is not set: the call works, but no hints."
        self._watch(session["id"])
        return out

    def _watch(self, sid: str) -> None:
        if self.start_watchers:
            from .watcher import TickerWatcher

            watcher = TickerWatcher(self, sid)
            self.watchers[sid] = watcher
            watcher.start()

    def _start_remote(self, *, goal: str, label: str, mode: str, cfg: dict,
                      callee_number: str, origin_session: str) -> dict:
        """Start on the shared bridge: it serves the pages, places phone calls and runs hints;
        the local store mirrors it, so the ticker, summary and tools work unchanged."""
        assert self.remote is not None
        if mode == modemod.ROOM:
            name = modemod.ROOM
        elif callee_number or mode in (modemod.TWILIO, "phone"):
            name = "phone"
        else:
            name = modemod.WEB
        numbers: dict[str, str] | None = None
        if name == "phone":
            numbers = {}
            for leg, number in (("you", cfg["user_number"]), ("them", callee_number)):
                e164, why = safety.validate_number(number)
                if e164 is None:  # the bridge applies the block and allow lists itself
                    return {"ok": False, "error": f"phone mode refused the {leg} number: "
                                                  f"{why or 'missing'} (set user_number)."}
                numbers[leg] = e164
        session = self.store.create_session(goal=goal, callee_label=label, mode=name,
                                            config=cfg, origin_session=origin_session,
                                            provider={"remote": True})
        try:
            resp = self.remote.create(session, numbers=numbers)
        except remotemod.BridgeError as exc:
            self.store.end_once(session["id"], "bridge_error")
            return {"ok": False, "error": f"call service error: {exc}"}
        raw_links = resp.get("links") if isinstance(resp.get("links"), dict) else {}
        links = {r: u for r, u in raw_links.items() if r in ("you", "them")
                 and isinstance(u, str) and u.startswith(("https://", "http://localhost"))}
        out: dict[str, Any] = {"ok": True, "session_id": session["id"], "mode": name,
                               "hint_mode": cfg["hint_mode"],
                               "max_duration_s": cfg["max_duration_s"], "links": links}
        tell = ("Tell the other person the call is being transcribed, then tap the confirm "
                "button on your page; nothing is transcribed before that.")
        if name == modemod.WEB:
            out["instructions"] = ("Open 'you' yourself with headphones; send 'them' to the "
                                   "other party. Each link works once, for 10 minutes. " + tell)
        elif name == modemod.ROOM:
            out["instructions"] = ("Open the link on a laptop or tablet next to your call on "
                                   "speaker and tap Start listening. " + tell)
        else:
            out["instructions"] = ("Your phone rings first; pick up and the other party is "
                                   "dialed. They hear that the call is being transcribed "
                                   "before anything is. Ghost only listens.")
        self._watch(session["id"])
        return out

    def status(self, sid: str | None) -> dict:
        self.expire_stale()
        s = self.find(sid)
        if s is None:
            return {"ok": False, "error": "no ghost session yet."}
        return {"ok": True, "session_id": s["id"], "state": s["state"], "mode": s["mode"],
                "goal": s["goal"], "callee": s["callee_label"],
                "duration": hintsmod.stamp(elapsed(s, self.now()) if s.get("live_at") else 0),
                "hint_count": self.store.hint_count(s["id"]),
                "intervene_allowed": s["intervene_allowed"],
                "end_reason": s.get("end_reason") or "",
                "summary_ready": s.get("summary") is not None}

    def hangup(self, sid: str | None) -> dict:
        s = self.find(sid)
        if s is None:
            return {"ok": False, "error": "no ghost session to end."}
        if s["state"] == ENDED:
            return {"ok": True, "session_id": s["id"], "state": ENDED, "already": True}
        remote = self.remote if (s.get("provider") or {}).get("remote") else None
        if remote is not None:  # the bridge ends the call; the summary arrives by sync
            sent = remote.hangup(s["id"])
            self.store.request_hangup(s["id"])
            self.store.end_once(s["id"], "hangup")
            return {"ok": True, "session_id": s["id"], "state": ENDED,
                    **({} if sent else {"note": "ended locally"})}
        self.store.request_hangup(s["id"])
        self.store.enqueue(s["id"], "hangup")
        deadline = time.monotonic() + HANGUP_WAIT_S
        while time.monotonic() < deadline:
            cur = self.store.get_session(s["id"]) or {}
            if cur.get("state") == ENDED:
                return {"ok": True, "session_id": s["id"], "state": ENDED}
            time.sleep(0.05)
        # No bridge answered within the budget: end it here (kill switch must not hang).
        self.store.end_once(s["id"], "hangup")
        return {"ok": True, "session_id": s["id"], "state": ENDED, "note": "ended locally"}

    def summary(self, sid: str | None, *, force: bool = False) -> dict:
        s = self.find(sid)
        if s is None:
            return {"ok": False, "error": "no ghost session yet."}
        if s.get("summary") is None:
            if s["state"] != ENDED:
                return {"ok": False, "error": f"{s['id']} is still {s['state']}; the summary "
                                              "is posted once the call ends."}
            if not force:
                return {"ok": False, "error": "the summary is still being built."}
            from .watcher import build_summary_here

            build_summary_here(self, s)
            s = self.store.get_session(s["id"]) or s
        from . import summary as summarymod

        return {"ok": True, "session_id": s["id"], "summary": s["summary"],
                "text": summarymod.render(s, s["summary"] or {})}


# ---- module-level service (Hermes calls plain functions) ------------------------------------
_service: GhostService | None = None


def configure(**kwargs: Any) -> GhostService:
    global _service
    _service = GhostService(**kwargs)
    return _service


def get_service() -> GhostService:
    global _service
    if _service is None:
        _service = GhostService()
    return _service


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _guard(fn: Callable[..., dict]) -> Callable[..., str]:
    def handler(args: dict | None = None, **kwargs: Any) -> str:
        try:
            return _json(fn(args if isinstance(args, dict) else {}, kwargs))
        except Exception as exc:  # noqa: BLE001 - never leak details (or secrets) to the model
            log.exception("hermes-ghost-call tool failed")
            return _json({"ok": False, "error": "internal error: " +
                          secretsmod.scrub(type(exc).__name__)})

    handler.__name__ = fn.__name__
    return handler


def _ghost_start(args: dict, kw: dict) -> dict:
    return get_service().start(goal=str(args.get("goal") or ""),
                               callee_label=str(args.get("callee_label") or ""),
                               mode=str(args.get("mode") or ""),
                               callee_number=str(args.get("callee_number") or ""),
                               origin_session=str(kw.get("session_id") or ""))


def _ghost_status(args: dict, kw: dict) -> dict:
    return get_service().status(args.get("session_id"))


def _ghost_hint_latest(args: dict, kw: dict) -> dict:
    svc = get_service()
    s = svc.find(args.get("session_id"))
    if s is None:
        return {"ok": False, "error": "no ghost session yet."}
    try:
        n = max(1, min(int(args.get("n") or 5), 40))
    except (TypeError, ValueError):
        n = 5
    rows = svc.store.hints(s["id"], limit=n)
    return {"ok": True, "session_id": s["id"], "state": s["state"],
            "hints": [{"id": h["n"], "line": hintsmod.format_line(h["t"], h["text"])}
                      for h in rows],
            "note": "During a live call the plugin pushes hints itself; do not poll this."}


def _ghost_speak(args: dict, kw: dict) -> dict:
    svc = get_service()
    s = svc.find(args.get("session_id"))
    if s is None or s["state"] != LIVE:
        return {"ok": False, "error": "no live ghost session."}
    raw = args.get("hint_id")
    n = None
    if raw not in (None, "", "latest"):
        try:
            n = int(str(raw).lstrip("#h"))
        except ValueError:
            return {"ok": False, "error": "hint_id must be a number from the ticker."}
        if svc.store.get_hint(s["id"], n) is None:
            return {"ok": False, "error": f"no hint {n} in {s['id']}."}
    elif svc.store.hint_count(s["id"]) == 0:
        return {"ok": False, "error": "no hint yet."}
    cid = svc.store.enqueue(s["id"], "speak", {"n": n})
    return {"ok": True, "session_id": s["id"], "queued": cid,
            "where": "your earpiece only (never the other party)"}


def _ghost_intervene(args: dict, kw: dict) -> dict:
    """Defense in depth: the pre_tool_call hook already blocked this without an allow."""
    svc = get_service()
    s = svc.find(args.get("session_id"))
    unattended = safety.is_unattended(svc.ctx, kw)
    refusal = safety.intervene_refusal(s, unattended=unattended)
    if refusal:
        return {"ok": False, "error": refusal}
    assert s is not None
    text = " ".join(str(args.get("text") or "").split())[:safety.MAX_INTERVENE_CHARS]
    if not text:
        return {"ok": False, "error": "text is required."}
    cid = svc.store.enqueue(s["id"], "intervene", {"text": text, "unattended": unattended})
    return {"ok": True, "session_id": s["id"], "queued": cid,
            "note": "The first Intervene line is prefixed with an AI-assistant disclosure."}


def _ghost_hangup(args: dict, kw: dict) -> dict:
    return get_service().hangup(args.get("session_id"))


def _ghost_summary(args: dict, kw: dict) -> dict:
    return get_service().summary(args.get("session_id"), force=bool(args.get("force", True)))


_SID = {"type": "string", "description": "Ghost session id (default: the current one)."}
SCHEMAS: dict[str, dict] = {
    "ghost_start": {
        "name": "ghost_start",
        "description": (
            "Start a Ghost Call: the assistant silently listens to the user's own phone call and "
            "pushes one-line hints to the user only. It never joins the call as a voice and "
            "never calls anyone as an AI persona. Returns two one-time join links in web mode "
            "(give 'you' to the user, 'them' goes to the other party) or, in phone mode, rings "
            "the user's phone first. Room mode (speakerphone) returns one link: the user opens it "
            "on a laptop next to a phone call on speaker (any call app), and Ghost listens "
            "through that one microphone. After starting, stay quiet: hints are pushed "
            "automatically; do not ask the user questions during the call."),
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "What the user wants from the call, "
                     "with facts, e.g. 'book a table Friday 8pm for 4, budget $80, name Winch'."},
            "callee_label": {"type": "string", "description": "Who is on the other side, "
                             "e.g. 'Bella Napoli'."},
            "mode": {"type": "string", "enum": ["web", "phone", "room"], "description":
                     "web (default unless phone calling is set up), phone (rings both sides), "
                     "or room (speakerphone: one page listens to a call on speaker)."},
            "callee_number": {"type": "string", "description": "Phone mode only: E.164."}},
            "required": ["goal"]}},
    "ghost_status": {
        "name": "ghost_status",
        "description": "State, duration, hint count and the Intervene flag of a ghost session.",
        "parameters": {"type": "object", "properties": {"session_id": _SID}}},
    "ghost_hint_latest": {
        "name": "ghost_hint_latest",
        "description": ("The last N hints of a ghost session, for questions after the fact. "
                        "Never poll this during a live call: the plugin pushes hints itself."),
        "parameters": {"type": "object", "properties": {
            "session_id": _SID, "n": {"type": "integer", "minimum": 1, "maximum": 40}}}},
    "ghost_speak": {
        "name": "ghost_speak",
        "description": ("Whisper a hint (by id, default the latest) into the user's earpiece. "
                        "The audio plays for the user only, never for the other party."),
        "parameters": {"type": "object", "properties": {
            "session_id": _SID, "hint_id": {"type": "string",
                                            "description": "Hint number or 'latest'."}}}},
    "ghost_intervene": {
        "name": "ghost_intervene",
        "description": (
            "Speak a short phrase to the OTHER party on the call. Off by default and refused "
            "unless the user typed /ghost intervene allow for this session; always refused in "
            "cron or unattended runs. The first line is prefixed with an AI disclosure. Only "
            "use it when the user explicitly asks you to say something to the other party."),
        "parameters": {"type": "object", "properties": {
            "session_id": _SID, "text": {"type": "string", "maxLength": 240}},
            "required": ["text"]}},
    "ghost_hangup": {
        "name": "ghost_hangup",
        "description": "End the ghost session now (the listener and any legs) within ~1 s.",
        "parameters": {"type": "object", "properties": {"session_id": _SID}}},
    "ghost_summary": {
        "name": "ghost_summary",
        "description": ("The post-call summary: agreed, date, amount, open questions, draft "
                        "follow-up SMS. It is also posted once automatically after hangup."),
        "parameters": {"type": "object", "properties": {"session_id": _SID}}},
}
HANDLERS: dict[str, Callable[..., str]] = {
    "ghost_start": _guard(_ghost_start),
    "ghost_status": _guard(_ghost_status),
    "ghost_hint_latest": _guard(_ghost_hint_latest),
    "ghost_speak": _guard(_ghost_speak),
    "ghost_intervene": _guard(_ghost_intervene),
    "ghost_hangup": _guard(_ghost_hangup),
    "ghost_summary": _guard(_ghost_summary),
}
