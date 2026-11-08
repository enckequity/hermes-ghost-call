"""The live ticker: one chat message that grows as hints arrive, then one result message.

A daemon thread per session, bound to the contextvars of the call that started it, so
``ctx.post_message`` lands in the conversation where ``/ghost start`` (or ``ghost_start``) ran.
It is one-way: it posts and edits, never asks, never waits for a reply.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from typing import Any

from . import hints as hintsmod
from . import summary as summarymod
from .llm import OpenAIClient
from .session import CREATED, ENDED, LIVE, elapsed

log = logging.getLogger(__name__)

POLL_S = 0.25
TICKER_LINES = 8
SUMMARY_WAIT_S = 25.0  # how long the bridge gets to write the summary before we build it here
POST_ATTEMPTS = 3
GHOST = "\U0001F47B"


def ticker_text(session: dict, lines: list[str], *, state: str) -> str:
    label = session.get("callee_label") or "call"
    head = f"{GHOST} ghost · {label} · " + ("live" if state == LIVE else state)
    body = "\n".join(lines[-TICKER_LINES:]) if lines else "listening…"
    return f"{head}\n{body}"


def build_summary_here(svc: Any, session: dict) -> bool:
    """Plugin-side fallback when the bridge never wrote a summary. Write-once; never raises."""
    sid = session["id"]
    cfg = session.get("config") or {}
    key = svc.secret("OPENAI_API_KEY")
    turns = svc.store.turns(sid) if cfg.get("store_transcripts", True) else []
    hint_rows = svc.store.hints(sid)
    stats = {"duration_s": round(elapsed(session, svc.now()), 1),
             "hint_count": len(hint_rows), "turn_count": len(turns),
             "end_reason": session.get("end_reason") or ""}
    box: list[dict] = []

    def work() -> None:
        async def go() -> dict:
            client = OpenAIClient(key) if key and turns else None
            try:
                return await summarymod.build(
                    client, goal=session.get("goal", ""),
                    callee_label=session.get("callee_label", ""),
                    user_name=str(cfg.get("user_name") or ""), turns=turns,
                    hint_lines=[h["text"] for h in hint_rows], model=str(cfg.get("model") or ""),
                    stats=stats)
            finally:
                if client is not None:
                    await client.aclose()

        try:
            box.append(asyncio.run(go()))
        except Exception:  # noqa: BLE001
            log.exception("ghost local summary failed")

    thread = threading.Thread(target=work, daemon=True, name=f"ghost-summary-{sid}")
    thread.start()
    thread.join(summarymod.SUMMARY_TIMEOUT_S + 5)
    result = box[0] if box else {**summarymod.fallback("summary unavailable"), **stats}
    return svc.store.set_summary_once(sid, result)


class TickerWatcher:
    def __init__(self, svc: Any, sid: str, *, poll_s: float = POLL_S) -> None:
        self.svc = svc
        self.sid = sid
        self.poll_s = poll_s
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._ticker: Any = None
        self._ticker_text = ""
        self._lines: list[str] = []
        self._result_done = False
        self._result_fails = 0
        self.posts: list[str] = []  # texts posted (tests)

    def start(self) -> threading.Thread:
        bound = contextvars.copy_context()
        self.thread = threading.Thread(target=bound.run, args=(self.run,), daemon=True,
                                       name=f"ghost-ticker-{self.sid}")
        self.thread.start()
        return self.thread

    def run(self) -> None:
        try:
            session = self.svc.store.get_session(self.sid) or {}
            cap = float((session.get("config") or {}).get("max_duration_s", 900))
            deadline = time.monotonic() + cap + 600 + 120
            while not self.stop.is_set() and time.monotonic() < deadline:
                if self.step():
                    break
                self.stop.wait(self.poll_s)
        except Exception:  # noqa: BLE001 - a watcher must never take anything down
            log.exception("ghost ticker crashed for %s", self.sid)
        finally:
            watchers = getattr(self.svc, "watchers", None)
            if isinstance(watchers, dict):
                watchers.pop(self.sid, None)

    # -- one poll; True when finished --
    def step(self) -> bool:
        store = self.svc.store
        session = store.get_session(self.sid)
        if session is None:
            return True
        remote = getattr(self.svc, "remote", None)
        if remote is not None and (session.get("provider") or {}).get("remote"):
            remote.sync(store, self.sid)
        self.svc.expire_stale()
        session = store.get_session(self.sid) or session
        state = session["state"]
        fresh = store.unposted_hints(self.sid)
        if fresh:
            self._lines += [hintsmod.format_line(h["t"], h["text"]) for h in fresh]
            store.mark_hints_posted(self.sid, fresh[-1]["n"])
        if state == LIVE or (state == ENDED and self._ticker is not None) or fresh:
            self._render(session, LIVE if state == LIVE else "ended")
        if state == CREATED:
            return False
        if state != ENDED:
            return False
        return self._result(session)

    def _render(self, session: dict, state: str) -> None:
        text = ticker_text(session, self._lines, state=state)
        if text == self._ticker_text:
            return
        if self._ticker is not None and getattr(self._ticker, "edit", None) is not None:
            try:
                res = self._ticker.edit(text)
                if not (isinstance(res, dict) and res.get("ok") is False):
                    self._ticker_text = text
                    return
            except Exception:  # noqa: BLE001
                pass
        handle = self._post(text)
        if handle is not None:
            self._ticker = handle
            self._ticker_text = text

    def _result(self, session: dict) -> bool:
        if self._result_done:
            return True
        if session.get("summary") is None:
            waited = self.svc.now() - float(session.get("ended_at") or self.svc.now())
            if waited < SUMMARY_WAIT_S:
                return False
            build_summary_here(self.svc, session)
            session = self.svc.store.get_session(self.sid) or session
            if session.get("summary") is None:
                return False
        if not self.svc.store.claim_summary_post(self.sid):
            self._result_done = True  # someone else delivered it: exactly one result message
            return True
        handle = self._post(summarymod.render(session, session["summary"]))
        if handle is None:
            self.svc.store.release_summary_post(self.sid)
            self._result_fails += 1
            if self._result_fails >= POST_ATTEMPTS:
                return True
            return False
        self._result_done = True
        return True

    def _post(self, text: str) -> Any:
        poster = getattr(self.svc.ctx, "post_message", None)
        if poster is None:
            return None
        try:
            handle = poster(text)
        except Exception:  # noqa: BLE001
            return None
        if getattr(handle, "ok", True) is False:
            return None
        self.posts.append(text)
        return handle
