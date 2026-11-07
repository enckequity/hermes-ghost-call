"""FastAPI bridge: health, the two web call pages, media websockets and the Twilio webhooks.

Everything external is injectable (``client``, ``stt_connect``, ``twilio``, ``clock``) so the
tests run with no network. Every refusal on the web routes looks the same (a generic 404) so a
probe never learns whether a session id exists. Tokens are hashed at rest and never logged.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import secrets
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .. import safety
from .. import summary as summarymod
from ..llm import OpenAIClient
from ..session import ACTIVE_STATES, CREATED, ROLES, Store, elapsed
from . import twilio_io, webpage
from .live import GhostRuntime
from .settings import BridgeSettings
from .stt import SttConnect, make_openai_connect

log = logging.getLogger(__name__)

RATE_LIMIT = 40  # requests per window per client key
RATE_WINDOW_S = 60.0
MAX_BODY = 2048
MAX_FRAME_BYTES = 4000  # one media message; a 20 ms frame is 160 bytes
START_TIMEOUT_S = 10.0
JANITOR_S = 1.0
PAGE_MODES = ("web", "room")  # modes served by browser pages (room: the You page only)


def _headers(settings: BridgeSettings, nonce: str = "") -> dict[str, str]:
    connect = "'self'"
    ws_base = settings.ws_base_url()
    if ws_base.startswith(("wss://", "ws://")):
        connect += " " + ws_base
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    style = f"'nonce-{nonce}'" if nonce else "'unsafe-inline'"
    csp = (f"default-src 'none'; script-src {script}; style-src {style}; "
           f"connect-src {connect}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Permissions-Policy": "microphone=(self)"}


class Bridge:
    """Runtime registry plus the janitor that settles sessions nobody is attached to."""

    def __init__(self, settings: BridgeSettings, store: Store, *, client: OpenAIClient | None,
                 stt_connect: SttConnect | None, twilio: twilio_io.TwilioRest | None,
                 clock: Callable[[], float]) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.stt_connect = stt_connect
        self.twilio = twilio
        self.clock = clock
        self.runtimes: dict[str, GhostRuntime] = {}
        self._dialing: set[str] = set()

    def runtime(self, sid: str) -> GhostRuntime | None:
        rt = self.runtimes.get(sid)
        if rt is not None and not rt.ended.is_set():
            return rt
        session = self.store.get_session(sid)
        if session is None or session["state"] not in ACTIVE_STATES:
            return None
        rt = GhostRuntime(sid, store=self.store, settings=self.settings, client=self.client,
                          stt_connect=self.stt_connect, twilio=self.twilio, clock=self.clock,
                          on_finished=lambda s: None)
        self.runtimes[sid] = rt
        return rt

    async def settle_unattached(self, session: dict, reason: str) -> None:
        """End a session no runtime owns, and write its (transcript-free) summary once."""
        sid = session["id"]
        if not self.store.end_once(sid, reason):
            return
        fresh = self.store.get_session(sid) or session
        stats = {"duration_s": round(elapsed(fresh, self.clock()), 1),
                 "hint_count": self.store.hint_count(sid), "turn_count": 0, "end_reason": reason}
        result = await summarymod.build(None, goal=fresh.get("goal", ""),
                                        callee_label=fresh.get("callee_label", ""),
                                        user_name="", turns=[], hint_lines=[], model="",
                                        stats=stats)
        self.store.set_summary_once(sid, result)

    async def janitor_once(self) -> None:
        now = self.clock()
        for session in self.store.active_sessions():
            sid = session["id"]
            rt = self.runtimes.get(sid)
            if rt is not None and not rt.ended.is_set():
                continue
            if session.get("hangup_requested"):
                await self.settle_unattached(session, "hangup")
            elif session["state"] == CREATED and now - session["created_at"] > \
                    self.settings.link_ttl_s:
                await self.settle_unattached(session, "link_expired")
            elif session["mode"] == "twilio" and session["state"] == CREATED:
                await self.dial_you(session)
        for sid in [s for s, rt in self.runtimes.items() if rt.ended.is_set()
                    and (rt.summary_task is None or rt.summary_task.done())]:
            self.runtimes.pop(sid, None)

    # -- Twilio dialing (both legs are checked against the blocklist and allowlist here too) --
    async def dial_you(self, session: dict) -> None:
        sid = session["id"]
        provider = session.get("provider") or {}
        if provider.get("you_call_sid") or sid in self._dialing or self.twilio is None:
            return
        self._dialing.add(sid)
        number = provider.get("user_number", "")
        check = safety.check_dialable(number, self.settings.environ)
        if not check.ok:
            await self.settle_unattached(session, f"refused_{check.reason[:40]}")
            return
        try:
            call_sid = await self.twilio.create_call(
                to=number, from_=self.settings.twilio_from_number,
                url=self._url(sid, "twiml", "you"), status_url=self._url(sid, "status", "you"))
            self.store.set_provider(sid, {"you_call_sid": call_sid})
            self.store.add_event(sid, "dialed_you", {})
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(sid, "dial_failed", {"leg": "you", "code": str(exc)[:40]})
            await self.settle_unattached(session, "dial_failed")

    async def dial_them(self, sid: str) -> None:
        session = self.store.get_session(sid)
        if session is None or self.twilio is None:
            return
        provider = session.get("provider") or {}
        if provider.get("them_call_sid"):
            return
        number = provider.get("callee_number", "")
        check = safety.check_dialable(number, self.settings.environ)
        if not check.ok:
            self.store.add_event(sid, "dial_refused", {"leg": "them", "reason": check.reason})
            rt = self.runtime(sid)
            if rt is not None:
                await rt.end("dial_refused")
            return
        try:
            call_sid = await self.twilio.create_call(
                to=number, from_=self.settings.twilio_from_number,
                url=self._url(sid, "twiml", "them"), status_url=self._url(sid, "status", "them"))
            self.store.set_provider(sid, {"them_call_sid": call_sid})
            self.store.add_event(sid, "dialed_them", {})
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(sid, "dial_failed", {"leg": "them", "code": str(exc)[:40]})

    def _url(self, sid: str, kind: str, role: str) -> str:
        return f"{self.settings.public_base_url}/twilio/{sid}/{kind}/{role}"

    async def shutdown(self) -> None:
        for rt in list(self.runtimes.values()):
            if not rt.ended.is_set():
                await rt.end("bridge_shutdown")
            if rt.summary_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(rt.summary_task, 5)


def create_app(settings: BridgeSettings, *, store: Store | None = None,
               client: OpenAIClient | None | bool = True, stt_connect: SttConnect | None | bool
               = True, twilio: twilio_io.TwilioRest | None | bool = True,
               clock: Callable[[], float] | None = None, janitor: bool = True) -> FastAPI:
    the_store = store if store is not None else Store(path=settings.db_path or None)
    the_clock = clock or the_store.clock
    if client is True:
        client = OpenAIClient(settings.openai_api_key) if settings.openai_api_key else None
    if stt_connect is True:
        stt_connect = make_openai_connect(settings.openai_api_key) \
            if settings.openai_api_key else None
    if twilio is True:
        twilio = twilio_io.TwilioRest(settings.twilio_account_sid, settings.twilio_auth_token) \
            if settings.twilio_ready() else None
    bridge = Bridge(settings, the_store, client=client or None, stt_connect=stt_connect or None,
                    twilio=twilio or None, clock=the_clock)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(_janitor(bridge)) if janitor else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await bridge.shutdown()
            if bridge.client is not None:
                await bridge.client.aclose()

    app = FastAPI(title="hermes-ghost-call bridge", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.bridge = bridge
    app.state.store = the_store
    hits: dict[str, deque[float]] = {}

    def limited(request: Request) -> bool:
        key = request.client.host if request.client else "?"
        if settings.trust_proxy:
            fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            key = fwd or key
        now = the_clock()
        q = hits.setdefault(key, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        if len(hits) > 1024:
            for k in [k for k, v in hits.items() if not v or now - v[-1] > RATE_WINDOW_S]:
                hits.pop(k, None)
        return False

    def invalid(status: int = 404) -> Response:
        return HTMLResponse(webpage.render_error(), status_code=status,
                            headers=_headers(settings))

    def usable(sid: str, role: str, token: str) -> dict | None:
        if len(sid) > 32 or role not in ROLES or not token or len(token) > 128:
            return None
        session = the_store.get_session(sid)
        if session is None or session["mode"] not in PAGE_MODES or \
                session["state"] not in ACTIVE_STATES:
            return None
        if session["mode"] == "room" and role != "you":  # speakerphone: one page only
            return None
        if session.get("hangup_requested"):
            return None
        if not the_store.peek_token(sid, role, "link", token):
            return None
        return session

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "service": "hermes-ghost-call"}

    @app.get("/g/{sid}/{role}")
    async def page(sid: str, role: str, request: Request, t: str = "") -> Response:
        if limited(request):
            return invalid(429)
        session = usable(sid, role, t)
        if session is None:
            return invalid()
        nonce = secrets.token_urlsafe(16)
        cfg = session.get("config") or {}
        if role == "you":
            html = webpage.render_you(sid=sid, goal=session["goal"],
                                      callee_label=session.get("callee_label", ""), nonce=nonce,
                                      tap_mode=cfg.get("hint_mode") == "tap",
                                      room=session["mode"] == "room")
        else:
            html = webpage.render_them(sid=sid, caller=str(cfg.get("user_name") or ""),
                                       nonce=nonce)
        return HTMLResponse(html, headers=_headers(settings, nonce))

    @app.post("/g/{sid}/join/{role}")
    async def join(sid: str, role: str, request: Request) -> Response:
        hdr = _headers(settings)
        gone = JSONResponse({"error": "unavailable"}, status_code=404, headers=hdr)
        if limited(request):
            return JSONResponse({"error": "unavailable"}, status_code=429, headers=hdr)
        try:
            body = json.loads((await request.body())[:MAX_BODY] or b"{}")
            token = body.get("t") if isinstance(body, dict) else ""
        except ValueError:
            return gone
        if not isinstance(token, str) or usable(sid, role, token) is None:
            return gone
        if not the_store.consume_token(sid, role, "link", token):
            return gone
        media = the_store.mint_token(sid, role, "media", settings.media_ttl_s)
        return JSONResponse({"media_token": media}, headers=hdr)

    @app.websocket("/g/{sid}/ws/{role}")
    async def web_media(ws: WebSocket, sid: str, role: str) -> None:
        await ws.accept()
        token = await _await_start_token(ws)
        session = the_store.get_session(sid) if role in ROLES else None
        if (token is None or session is None or session["mode"] not in PAGE_MODES
                or (session["mode"] == "room" and role != "you")
                or session["state"] not in ACTIVE_STATES
                or not the_store.consume_token(sid, role, "media", token)):
            await _close(ws, 1008)
            return
        rt = bridge.runtime(sid)
        if rt is None:
            await _close(ws, 1008)
            return
        lock = asyncio.Lock()

        async def send(msg: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(msg))

        await rt.attach(role, send)
        try:
            while not rt.ended.is_set():
                raw = await ws.receive_text()
                msg = _parse(raw)
                if msg is None:
                    continue
                if msg.get("event") == "media":
                    chunk = _b64(msg.get("payload"))
                    if chunk:
                        await rt.on_audio(role, chunk)
                else:
                    await rt.on_client_event(role, msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await rt.detach(role)
            await _close(ws, 1000)

    # -- Twilio mode ---------------------------------------------------------------------------
    async def verified_form(request: Request) -> dict[str, str] | None:
        params = twilio_io.parse_form(await request.body())
        url = twilio_io.public_url(settings.public_base_url, request.url.path,
                                   request.url.query)
        if twilio_io.valid_signature(settings.twilio_auth_token, url, params,
                                     request.headers.get("X-Twilio-Signature")):
            return params
        return None

    def twilio_session(sid: str) -> dict | None:
        session = the_store.get_session(sid)
        if session is None or session["mode"] != "twilio" or \
                session["state"] not in ACTIVE_STATES:
            return None
        return session

    @app.post("/twilio/{sid}/twiml/{role}")
    async def twiml(sid: str, role: str, request: Request) -> Response:
        if await verified_form(request) is None:
            return PlainTextResponse("invalid signature", status_code=403)
        if role not in ROLES or twilio_session(sid) is None:
            return PlainTextResponse("refused", status_code=403)
        token = the_store.mint_token(sid, role, "media", settings.media_ttl_s)
        xml = twilio_io.leg_twiml(sid=sid, role=role,
                                  ws_url=f"{settings.ws_base_url()}/twilio/{sid}/media/{role}",
                                  token=token, status_url=bridge._url(sid, "status", role))
        if role == "you":
            asyncio.get_running_loop().call_later(
                0.5, lambda: asyncio.ensure_future(bridge.dial_them(sid)))
        return Response(xml, media_type="text/xml")

    @app.post("/twilio/{sid}/status/{role}")
    async def status(sid: str, role: str, request: Request) -> Response:
        params = await verified_form(request)
        if params is None:
            return PlainTextResponse("invalid signature", status_code=403)
        session = the_store.get_session(sid)
        if session is None:
            return PlainTextResponse("unknown", status_code=404)
        state = (params.get("CallStatus") or params.get("StatusCallbackEvent") or "").lower()
        if state in ("completed", "busy", "failed", "no-answer", "canceled", "conference-end"):
            reason = "call_completed" if state in ("completed", "conference-end") \
                else f"{role}_{state.replace('-', '_')}"
            rt = bridge.runtimes.get(sid)
            if rt is not None and not rt.ended.is_set():
                await rt.end(reason)
            elif session["state"] in ACTIVE_STATES:
                await bridge.settle_unattached(session, reason)
        return JSONResponse({"ok": True})

    @app.post("/twilio/{sid}/announce/{role}")
    async def announce(sid: str, role: str, request: Request, t: str = "") -> Response:
        if await verified_form(request) is None:
            return PlainTextResponse("invalid signature", status_code=403)
        rt = bridge.runtimes.get(sid)
        text = rt.take_announce_text(role, t) if rt is not None and role in ROLES else None
        xml = twilio_io.say_twiml(text) if text else twilio_io.empty_twiml()
        return Response(xml, media_type="text/xml")

    @app.websocket("/twilio/{sid}/media/{role}")
    async def twilio_media(ws: WebSocket, sid: str, role: str) -> None:
        await ws.accept()
        token = await _await_twilio_start(ws)
        if (role not in ROLES or token is None or twilio_session(sid) is None
                or not the_store.consume_token(sid, role, "media", token)):
            await _close(ws, 1008)
            return
        rt = bridge.runtime(sid)
        if rt is None:
            await _close(ws, 1008)
            return
        await rt.attach(role, None)  # receive-only: nothing is ever sent back on this socket
        try:
            while not rt.ended.is_set():
                msg = _parse(await ws.receive_text())
                if msg is None:
                    continue
                if msg.get("event") == "media":
                    chunk = _b64((msg.get("media") or {}).get("payload"))
                    if chunk:
                        await rt.on_audio(role, chunk)
                elif msg.get("event") == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await rt.detach(role)
            await _close(ws, 1000)

    return app


# ---- helpers ------------------------------------------------------------------------------
async def _janitor(bridge: Bridge) -> None:
    while True:
        try:
            await bridge.janitor_once()
        except Exception:  # noqa: BLE001
            log.exception("ghost janitor failed")
        await asyncio.sleep(JANITOR_S)


def _parse(raw: str) -> dict | None:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return msg if isinstance(msg, dict) else None


def _b64(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_FRAME_BYTES * 2:
        return b""
    try:
        return base64.b64decode(value, validate=True)[:MAX_FRAME_BYTES]
    except (binascii.Error, ValueError):
        return b""


async def _await_start_token(ws: WebSocket) -> str | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), START_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    msg = _parse(raw)
    if not msg or msg.get("event") != "start" or not isinstance(msg.get("token"), str):
        return None
    return msg["token"]


async def _await_twilio_start(ws: WebSocket) -> str | None:
    async def loop() -> str | None:
        while True:
            msg = _parse(await ws.receive_text())
            if msg is None:
                return None
            if msg.get("event") == "start":
                params = (msg.get("start") or {}).get("customParameters") or {}
                token = params.get("token")
                return token if isinstance(token, str) else None
            if msg.get("event") != "connected":
                return None

    try:
        return await asyncio.wait_for(loop(), START_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)
