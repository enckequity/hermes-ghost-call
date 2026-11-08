"""Client for the shared bridge (docs/BRIDGE_API.md), used when GHOST_CALL_BRIDGE_URL is set with
either GHOST_CALL_BRIDGE_TOKEN or TEAMSHIFT_USAGE_TOKEN plus TEAMSHIFT_WORKSPACE_NAME.

The workspace makes outbound HTTPS requests only. The local store stays the source for the
ticker, the summary and the tools: ``create`` starts the session on the bridge, and ``sync``
(called by the ticker watcher, at most once a second) pushes whispers and mirrors hints, state,
usage and the summary back. Mirroring is idempotent: hints by their bridge number, the summary
write-once. Stdlib only (``urllib``). No redirects, so the token never reaches a second host.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from . import summary as summarymod
from .session import ENDED, LIVE, Store

log = logging.getLogger(__name__)

URL_ENV = "GHOST_CALL_BRIDGE_URL"
TOKEN_ENV = "GHOST_CALL_BRIDGE_TOKEN"  # owner: a signed <workspace>.<sig> token
USAGE_TOKEN_ENV = "TEAMSHIFT_USAGE_TOKEN"  # customer boxes: the existing usage token...
WORKSPACE_ENV = "TEAMSHIFT_WORKSPACE_NAME"  # ...sent with the workspace name in a header
HINT_KEY_ENV = "OPENROUTER_API_KEY"  # the workspace's own key for hints and the summary
API = "/v1"
TIMEOUT_S = 10.0
POLL_S = 1.0
UNREACHABLE_FAIL_S = 120.0
BACKOFF_STATUSES = (429, 503)  # the bridge is busy, not gone: wait, never fail the session
BACKOFF_MIN_S, BACKOFF_MAX_S = 2.0, 10.0
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # never resend the token
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _env(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name) or "").strip()


def credentials(environ: Mapping[str, str]) -> dict[str, str]:
    """Auth headers for the bridge: GHOST_CALL_BRIDGE_TOKEN when set, else the workspace usage
    token plus its name, else none."""
    if token := _env(environ, TOKEN_ENV):
        return {"Authorization": f"Bearer {token}"}
    usage, name = _env(environ, USAGE_TOKEN_ENV), _env(environ, WORKSPACE_ENV)
    if usage and name:
        return {"Authorization": f"Bearer {usage}", "X-TeamShift-Workspace": name}
    return {}


def configured(environ: Mapping[str, str]) -> bool:
    return bool(_env(environ, URL_ENV) and credentials(environ))


class RemoteBridge:
    def __init__(self, environ: Mapping[str, str], *, timeout_s: float = TIMEOUT_S,
                 poll_s: float = POLL_S, opener: Any = None) -> None:
        self.environ = environ
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._open = (opener or _OPENER).open
        self._last: dict[str, float] = {}
        self._down_since: dict[str, float] = {}
        self._wait_until: dict[str, float] = {}  # back-off after a 429 or 503

    # -- HTTP --
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        base = str(self.environ.get(URL_ENV) or "").strip().rstrip("/")
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != "https" and not (parts.scheme == "http"
                                            and parts.hostname in _LOCAL_HOSTS):
            raise BridgeError(f"{URL_ENV} must be an https URL")
        creds = credentials(self.environ)
        if not creds:
            raise BridgeError(f"set {TOKEN_ENV}, or {USAGE_TOKEN_ENV} and {WORKSPACE_ENV}")
        req = urllib.request.Request(
            base + API + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={**creds, "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "ghost-call"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"HTTP {exc.code}: {_error_text(exc)}", exc.code,
                              _retry_after(exc)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise BridgeError(f"not reachable ({type(exc).__name__})") from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise BridgeError("answered with invalid JSON") from None
        if not isinstance(data, dict):
            raise BridgeError("answered with invalid JSON")
        return data

    @staticmethod
    def _path(sid: str, *rest: str) -> str:
        return "/sessions/" + "/".join(urllib.parse.quote(p, safe="") for p in (sid, *rest))

    # -- calls the service makes --
    def create(self, session: dict, *, numbers: dict[str, str] | None = None) -> dict:
        """Start ``session`` on the bridge. Returns ``{"links": {...}, ...}``; raises BridgeError.
        The hint key goes in this one request and is held in the bridge's memory only."""
        cfg = dict(session.get("config") or {})
        body: dict[str, Any] = {
            "goal": session["goal"], "callee_label": session.get("callee_label") or "",
            "mode": session["mode"], "config": cfg,
            "hint": {"api_key": str(self.environ.get(HINT_KEY_ENV) or ""),
                     "model": str(cfg.get("hint_model") or "")}}
        if numbers:
            body["numbers"] = numbers
        try:
            return self._request("PUT", self._path(session["id"]), body)
        except BridgeError as exc:
            if not exc.status:  # outcome unknown: make sure nothing stays live
                self.hangup(session["id"])
            raise

    def hangup(self, sid: str) -> bool:
        try:
            self._request("POST", self._path(sid, "hangup"), {})
        except BridgeError as exc:
            if exc.status != 409:
                log.warning("ghost bridge hangup failed: %s", exc)
                return False
        return True

    def health(self) -> str:
        """'' when the bridge takes sessions for this workspace, else why not."""
        try:
            self._request("GET", "/health")
        except BridgeError as exc:
            return str(exc)
        return ""

    # -- the watcher's poll --
    def sync(self, store: Store, sid: str, *, force: bool = False) -> None:
        """Push whispers, then mirror one ``updates`` page. Never raises."""
        session = store.get_session(sid)
        if session is None or (session["state"] == ENDED and session.get("summary") is not None):
            return
        now = time.monotonic()
        if now < self._wait_until.get(sid, 0.0) or (
                not force and now - self._last.get(sid, -1e9) < self.poll_s):
            return
        self._last[sid] = now
        try:
            self._push(store, sid)
            path = self._path(sid, "updates")
            cursor = (session.get("provider") or {}).get("bridge_cursor")
            if cursor:
                path += "?cursor=" + urllib.parse.quote(cursor, safe="")
            data = self._request("GET", path)
        except BridgeError as exc:
            self._unreachable(store, session, exc)
            return
        except Exception:  # noqa: BLE001 - a watcher must keep going
            log.exception("ghost bridge sync failed for %s", sid)
            return
        self._down_since.pop(sid, None)
        mirror(store, sid, data)

    def _push(self, store: Store, sid: str) -> None:
        for cmd in store.take_commands(sid):
            if cmd["kind"] == "speak":  # the command id makes a retried PUT a no-op
                n = (cmd.get("payload") or {}).get("n")
                try:
                    self._request("PUT", self._path(sid, "commands", f"c{cmd['id']}"),
                                  {"kind": "speak", "n": n})
                    result = "sent"
                except BridgeError as exc:
                    result = f"error {exc.status}"
            elif cmd["kind"] == "hangup":
                result = "sent" if self.hangup(sid) else "error"
            else:  # intervene never leaves the workspace
                result = "refused"
            store.finish_command(cmd["id"], result)

    def _unreachable(self, store: Store, session: dict, exc: BridgeError) -> None:
        sid = session["id"]
        if exc.status in BACKOFF_STATUSES:  # reachable and answering: back off, keep the session
            delay = exc.retry_after if exc.retry_after is not None else random.uniform(  # noqa: S311
                BACKOFF_MIN_S, BACKOFF_MAX_S)
            self._wait_until[sid] = time.monotonic() + delay
            self._down_since.pop(sid, None)
            return
        log.warning("ghost bridge sync failed for %s: %s", sid, exc)
        gone = exc.status in (401, 403, 404)
        first = self._down_since.setdefault(sid, time.monotonic())
        if not gone and time.monotonic() - first < UNREACHABLE_FAIL_S:
            return
        store.end_once(sid, "bridge_rejected" if gone else "bridge_unreachable")
        self._down_since.pop(sid, None)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Retry-After in seconds (clamped to 1..60), or None when absent or a date."""
    raw = (exc.headers.get("Retry-After") if exc.headers is not None else None) or ""
    try:
        return min(60.0, max(1.0, float(raw)))
    except ValueError:
        return None


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read() or b"{}")
        err = data.get("error") if isinstance(data, dict) else None
        text = err.get("message") if isinstance(err, dict) else err
    except Exception:  # noqa: BLE001
        text = ""
    return str(text or exc.reason or "error").replace("\n", " ")[:200]


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) \
        and value >= 0 else None


def _summary(raw: dict) -> dict:
    """Only the summary fields the ticker renders, in the shapes it expects."""
    out = summarymod._clean(raw)
    for key in ("duration_s", "hint_count", "turn_count"):
        out[key] = _num(raw.get(key)) or 0
    for key, limit in (("end_reason", 64), ("note", 200)):
        out[key] = raw[key][:limit] if isinstance(raw.get(key), str) else ""
    out["source"] = raw.get("source") if raw.get("source") in ("model", "fallback") else "fallback"
    out["hints"] = [h[:200] for h in raw.get("hints") or [] if isinstance(h, str)][-5:]
    return out


def mirror(store: Store, sid: str, data: Mapping[str, Any]) -> None:
    """Apply one ``updates`` page to the local store. Bridge text is untrusted data."""
    for h in data.get("hints") or []:
        if not isinstance(h, dict) or not isinstance(h.get("n"), int) or h["n"] < 1:
            continue
        text = h.get("text")
        if isinstance(text, str) and text.strip():
            store.put_hint(sid, h["n"], _num(h.get("t")) or 0.0, " ".join(text.split())[:200])
    info = data.get("session") if isinstance(data.get("session"), dict) else {}
    if info.get("consented") is True:
        store.mark_consent(sid)
    usage = info.get("usage") if isinstance(info.get("usage"), dict) else {}
    stt, tts = _num(usage.get("stt_seconds")), _num(usage.get("tts_chars"))
    if stt is not None or tts is not None:
        store.set_usage(sid, {"stt_seconds": stt or 0.0, "tts_chars": int(tts or 0)})
    state = info.get("state")
    if state == LIVE:
        store.mark_live(sid)
    if state == ENDED:
        reason = info.get("end_reason")
        store.end_once(sid, reason if isinstance(reason, str) and _REASON.match(reason)
                       else "ended")
        if isinstance(info.get("summary"), dict):
            store.set_summary_once(sid, _summary(info["summary"]))
    cursor = data.get("cursor")
    if isinstance(cursor, str) and len(cursor) <= 64:
        store.set_provider(sid, {"bridge_cursor": cursor})
