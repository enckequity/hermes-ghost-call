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
import re
import secrets
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .. import config as configmod
from .. import safety
from .. import summary as summarymod
from ..llm import OpenAIClient
from ..session import ACTIVE_STATES, CREATED, ENDED, LIVE, ROLES, Store, elapsed
from . import telnyx_io, twilio_io, webpage
from .auth import AuthBusy, Authenticator
from .live import GhostRuntime
from .settings import DEFAULT_HINT_BASE_URL, DEFAULT_HINT_MODEL, BridgeSettings, Tenant
from .stt import SttConnect, make_openai_connect

log = logging.getLogger(__name__)

RATE_LIMIT = 40  # requests per window per client key
RATE_WINDOW_S = 60.0
MAX_BODY = 2048
MAX_FRAME_BYTES = 4000  # one media message; a 20 ms frame is 160 bytes
START_TIMEOUT_S = 10.0
JANITOR_S = 1.0
PAGE_MODES = ("web", "room")  # modes served by browser pages (room: the You page only)
SHARED_MODES = (*PAGE_MODES, "phone")  # what a workspace may start on the shared bridge
MAX_API_BODY = 16384
DAY_S = 86400.0
API_RATE_LIMIT = 60  # per client IP, per minute: auth lookups and failed auth only
WORKSPACE_RATE_LIMIT = 150  # per verified workspace, per minute; a live session polls 60/min
SPEAK_GAP_S = 2.0
SEEN_MAX = 256
_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _headers(settings: BridgeSettings, nonce: str = "") -> dict[str, str]:
    connect = "'self'"
    ws_base = settings.ws_base_url()
    if ws_base.startswith(("wss://", "ws://")):
        parts = urlsplit(ws_base)  # origin only: a CSP source with a path matches that path alone
        connect += f" {parts.scheme}://{parts.netloc}"
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
                 clock: Callable[[], float], telnyx: telnyx_io.TelnyxRest | None = None) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.stt_connect = stt_connect
        self.twilio = twilio
        self.telnyx = telnyx
        self.clock = clock
        self.runtimes: dict[str, GhostRuntime] = {}
        self._dialing: set[str] = set()
        # Shared bridge, memory only: a workspace's hint model key per session (never written to
        # disk), the create response for an idempotent repeat, and command ids already taken.
        self.hint_keys: dict[str, str] = {}
        self.created: dict[str, dict] = {}
        # shortcut: lost on restart, so a speak retried across a restart may play twice;
        # persist in the commands table if that matters.
        self.seen_commands: dict[str, dict[str, None]] = {}  # sid -> ordered cmd ids, last 256
        self.last_speak: dict[str, float] = {}

    def runtime(self, sid: str) -> GhostRuntime | None:
        rt = self.runtimes.get(sid)
        if rt is not None and not rt.ended.is_set():
            return rt
        session = self.store.get_session(sid)
        if session is None or session["state"] not in ACTIVE_STATES:
            return None
        hint_client: OpenAIClient | None | bool = True
        if session.get("workspace"):  # hints bill the workspace's own key, never the bridge's
            key = self.hint_keys.get(sid, "")
            base = (session.get("config") or {}).get("hint_base_url") or DEFAULT_HINT_BASE_URL
            hint_client = OpenAIClient(key, base_url=base) if key else None
        rt = GhostRuntime(sid, store=self.store, settings=self.settings, client=self.client,
                          stt_connect=self.stt_connect, twilio=self.twilio, clock=self.clock,
                          on_finished=self.forget, hint_client=hint_client,
                          telnyx=self.telnyx)
        self.runtimes[sid] = rt
        return rt

    def forget(self, sid: str) -> None:
        self.hint_keys.pop(sid, None)
        self.created.pop(sid, None)
        self.seen_commands.pop(sid, None)
        self.last_speak.pop(sid, None)

    async def settle_unattached(self, session: dict, reason: str) -> None:
        """End a session no runtime owns, and write its (transcript-free) summary once."""
        sid = session["id"]
        if not self.store.end_once(sid, reason):
            return
        self.forget(sid)
        await self.phone_hangup(session)
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

    # -- phone mode on Telnyx ---------------------------------------------------------------
    async def phone_hangup(self, session: dict) -> None:
        if session.get("mode") != "phone" or self.telnyx is None:
            return
        provider = (self.store.get_session(session["id"]) or session).get("provider") or {}
        for key in ("you_ccid", "them_ccid"):
            if provider.get(key):
                with contextlib.suppress(Exception):
                    await self.telnyx.hangup(provider[key])

    async def dial_phone(self, sid: str, leg: str) -> bool:
        """Ring one leg. The bridge re-checks the number itself, whatever the plugin sent."""
        session = self.store.get_session(sid)
        if session is None or self.telnyx is None or session["state"] not in ACTIVE_STATES:
            return False
        provider = session.get("provider") or {}
        if provider.get(f"{leg}_ccid"):
            return True
        check = safety.check_dialable(provider.get(f"{leg}_number", ""), self.settings.environ)
        if not check.ok:
            self.store.add_event(sid, "dial_refused", {"leg": leg, "reason": check.reason})
            await self.settle_unattached(session, "dial_refused")
            return False
        try:
            ccid = await self.telnyx.dial(
                to=provider[f"{leg}_number"], from_=self.settings.caller_number,
                webhook_url=f"{self.settings.public_base_url}/telnyx/webhook",
                state=telnyx_io.client_state(sid, leg))
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(sid, "dial_failed", {"leg": leg, "code": str(exc)[:40]})
            await self.settle_unattached(session, "dial_failed")
            return False
        self.store.set_provider(sid, {f"{leg}_ccid": ccid})
        self.store.add_event(sid, f"dialed_{leg}", {})
        return True

    async def on_phone_event(self, kind: str, payload: dict) -> None:
        """One verified webhook. Replays are harmless: every step is guarded."""
        state = telnyx_io.read_client_state(payload.get("client_state"))
        sid, leg = state.get("sid"), state.get("leg")
        session = self.store.get_session(sid) if isinstance(sid, str) and len(sid) <= 32 \
            else None
        if session is None or session["mode"] != "phone" or leg not in ROLES:
            return
        provider = session.get("provider") or {}
        ccid = payload.get("call_control_id")
        if not isinstance(ccid, str) or not ccid or ccid != provider.get(f"{leg}_ccid"):
            return
        if kind == "call.hangup":
            rt = self.runtimes.get(sid)
            if rt is not None and not rt.ended.is_set():
                await rt.end(f"{leg}_hung_up")
            elif session["state"] in ACTIVE_STATES:
                await self.settle_unattached(session, f"{leg}_hung_up")
            return
        if session["state"] not in ACTIVE_STATES or self.telnyx is None:
            return
        try:
            if kind == "call.answered" and leg == "you":
                await self.dial_phone(sid, "them")
            elif kind == "call.answered" and leg == "them" and \
                    not provider.get("disclosure_started"):
                self.store.set_provider(sid, {"disclosure_started": True})
                await self.telnyx.speak(ccid, telnyx_io.DISCLOSURE,
                                        state=telnyx_io.client_state(sid, "them", "disclosure"))
                usage = session.get("usage") or {}
                self.store.set_usage(sid, {**usage, "tts_chars": int(usage.get("tts_chars") or 0)
                                           + len(telnyx_io.DISCLOSURE)})
            elif kind == "call.speak.ended" and leg == "them" and \
                    state.get("kind") == "disclosure" and payload.get("status") != "completed":
                # The notice was cut off or failed: nobody heard it, so never bridge.
                await self.settle_unattached(session, "disclosure_failed")
            elif kind == "call.speak.ended" and leg == "them" and \
                    state.get("kind") == "disclosure" and self.store.mark_consent(sid):
                # Only now does any audio leave the call: bridge the legs, then fork each one.
                await self.telnyx.bridge(provider["you_ccid"], ccid)
                for role in ROLES:
                    token = self.store.mint_token(sid, role, "media", self.settings.media_ttl_s)
                    await self.telnyx.stream(
                        provider[f"{role}_ccid"],
                        f"{self.settings.ws_base_url()}/telnyx/{sid}/media/{role}?t={token}")
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(sid, "phone_failed", {"code": str(exc)[:40]})
            await self.settle_unattached(session, "phone_failed")

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
               clock: Callable[[], float] | None = None, janitor: bool = True,
               telnyx: telnyx_io.TelnyxRest | None | bool = True) -> FastAPI:
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
    if telnyx is True:
        telnyx = telnyx_io.TelnyxRest(settings.telnyx_api_key, settings.telnyx_connection_id) \
            if settings.phone_ready() else None
    bridge = Bridge(settings, the_store, client=client or None, stt_connect=stt_connect or None,
                    twilio=twilio or None, clock=the_clock, telnyx=telnyx or None)

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
    if settings.base_path():
        app.add_middleware(_StripPrefix, prefix=settings.base_path())
    app.state.bridge = bridge
    app.state.store = the_store
    hits: dict[str, deque[float]] = {}

    def limited(request: Request) -> bool:
        key = _client_ip(request, settings.trust_proxy)
        return _over(hits, key, RATE_LIMIT, the_clock())
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

    _add_shared_routes(app, bridge)
    return app


# ---- shared bridge: workspace API (docs/BRIDGE_API.md) and phone mode -----------------------
def _err(status: int, code: str, message: str) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 429:
        headers["Retry-After"] = "5"
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers=headers)


def _snapshot(store: Store, session: dict) -> dict:
    return {"state": session["state"], "end_reason": session.get("end_reason") or None,
            "consented": session.get("consent_at") is not None,
            "usage": {"stt_seconds": 0.0, "tts_chars": 0, **(session.get("usage") or {})},
            "summary": session.get("summary"), "hint_count": store.hint_count(session["id"])}


def _add_shared_routes(app: FastAPI, bridge: Bridge) -> None:
    settings, store = bridge.settings, bridge.store

    authn = Authenticator(settings, clock=bridge.clock)

    api_hits: dict[str, deque[float]] = {}

    async def caller(request: Request) -> Tenant | JSONResponse:
        # The per-IP limit counts only requests that cost work (an auth-service lookup, or a
        # failed auth): workspaces can share an egress IP, and verified ones must not lock each
        # other out. Verified requests count against their own workspace only.
        ip_key = "ip:" + _client_ip(request, settings.trust_proxy)
        counted = False

        def admit() -> bool:
            nonlocal counted
            counted = True
            return not _over(api_hits, ip_key, API_RATE_LIMIT, bridge.clock())

        try:
            tenant = await authn.resolve(request.headers, admit=admit)
        except AuthBusy:
            return _err(429, "rate_limited", "too many sign-in checks, try again shortly")
        if tenant is None:
            if not counted and not admit():
                return _err(429, "rate_limited", "too many requests, slow down")
            return _err(401, "unauthorized", "bad workspace token")
        if _over(api_hits, "ws:" + tenant.workspace, WORKSPACE_RATE_LIMIT, bridge.clock()):
            return _err(429, "rate_limited", "too many requests, slow down")
        if tenant.disabled:
            return _err(403, "disabled", "this workspace is turned off on the bridge")
        return tenant

    async def body_of(request: Request) -> dict | None:
        raw = await request.body()
        if len(raw) > MAX_API_BODY:
            return None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def owned(tenant: Tenant, ref: str) -> dict | None:
        """A session of this workspace only; any other id reads as not found."""
        return store.tenant_session(tenant.workspace, ref) if _REF.match(ref) else None

    @app.get("/v1/health")
    async def v1_health(request: Request) -> Response:
        tenant = await caller(request)
        if isinstance(tenant, JSONResponse):
            return tenant
        return JSONResponse({"ok": True, "phone": bridge.telnyx is not None})

    @app.put("/v1/sessions/{ref}")
    async def v1_create(ref: str, request: Request) -> Response:
        tenant = await caller(request)
        if isinstance(tenant, JSONResponse):
            return tenant
        if not _REF.match(ref):
            return _err(422, "bad_id", "session id must match [A-Za-z0-9_-]{1,64}")
        existing = owned(tenant, ref)
        if existing is not None:
            cached = bridge.created.get(existing["id"])
            return JSONResponse(cached or {"session_id": ref, "state": existing["state"],
                                           "mode": existing["mode"], "links": {}})
        body = await body_of(request)
        if body is None:
            return _err(400, "bad_body", "body must be a JSON object")
        mode = str(body.get("mode") or "web")
        goal = " ".join(str(body.get("goal") or "").split())[:400]
        if mode not in SHARED_MODES:
            return _err(422, "bad_mode", "mode must be web, room or phone")
        if not goal:
            return _err(422, "goal_required", "a goal is required")
        if mode == "phone" and bridge.telnyx is None:
            return _err(422, "phone_unavailable", "phone mode is not available on this bridge")
        if store.active_sessions(tenant.workspace):
            return _err(422, "session_active", "another session is still active")
        if store.sessions_since(bridge.clock() - DAY_S, tenant.workspace) >= \
                tenant.daily_session_limit:
            return _err(422, "daily_limit", "daily session limit reached")
        raw_cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
        cfg = configmod.resolve(lambda key, _default: raw_cfg.get(key))  # clamped here too
        hint = body.get("hint") if isinstance(body.get("hint"), dict) else {}
        cfg.update(store_transcripts=False,  # transcripts stay in memory for the summary only
                   # never cfg["model"]: an unprefixed id fails on the hint gateway
                   model=str(hint.get("model") or tenant.hint_model or DEFAULT_HINT_MODEL)[:120],
                   hint_base_url=tenant.hint_base_url or DEFAULT_HINT_BASE_URL)
        provider: dict[str, str] = {}
        if mode == "phone":
            numbers = body.get("numbers") if isinstance(body.get("numbers"), dict) else {}
            for leg in ROLES:
                check = safety.check_dialable(str(numbers.get(leg) or ""), settings.environ)
                if not check.ok:
                    return _err(422, "number_refused", f"the {leg} number was refused: "
                                                       f"{check.reason}")
                provider[f"{leg}_number"] = safety.validate_number(numbers[leg])[0] or ""
        session = store.create_session(
            goal=goal, callee_label=" ".join(str(body.get("callee_label") or "").split())[:80],
            mode=mode, config=cfg, provider=provider, session_id="g" + secrets.token_hex(8),
            workspace=tenant.workspace, client_ref=ref)
        sid = session["id"]
        key = hint.get("api_key")
        if isinstance(key, str) and 0 < len(key) <= 512:
            bridge.hint_keys[sid] = key
        links: dict[str, str] = {}
        if mode in PAGE_MODES:
            for role in ("you",) if mode == "room" else ROLES:
                tok = store.mint_token(sid, role, "link", settings.link_ttl_s)
                links[role] = f"{settings.public_base_url}/g/{sid}/{role}?t={tok}"
        elif not await bridge.dial_phone(sid, "you"):
            return _err(502, "dial_failed", "the call could not be placed")
        out = {"session_id": ref, "state": CREATED, "mode": mode, "links": links,
               "expires_in": int(settings.link_ttl_s)}
        bridge.created[sid] = out
        return JSONResponse(out, status_code=201)

    @app.get("/v1/sessions/{ref}/updates")
    async def v1_updates(ref: str, request: Request, cursor: str = "") -> Response:
        tenant = await caller(request)
        if isinstance(tenant, JSONResponse):
            return tenant
        session = owned(tenant, ref)
        if session is None:
            return _err(404, "not_found", "no such session")
        after = int(cursor) if cursor.isdigit() and len(cursor) < 10 else 0
        hints = [{"n": h["n"], "t": h["t"], "text": h["text"]}
                 for h in store.hints(session["id"]) if h["n"] > after]
        last = hints[-1]["n"] if hints else after
        return JSONResponse({"cursor": str(last), "session": _snapshot(store, session),
                             "hints": hints}, headers={"Cache-Control": "no-store"})

    @app.put("/v1/sessions/{ref}/commands/{cmd_id}")
    async def v1_command(ref: str, cmd_id: str, request: Request) -> Response:
        tenant = await caller(request)
        if isinstance(tenant, JSONResponse):
            return tenant
        session = owned(tenant, ref)
        if session is None or not _REF.match(cmd_id):
            return _err(404, "not_found", "no such session")
        body = await body_of(request) or {}
        if body.get("kind") != "speak":  # intervene does not exist on the shared bridge
            return _err(422, "bad_command", "only speak is accepted")
        seen = bridge.seen_commands.setdefault(session["id"], {})
        if cmd_id in seen:
            return JSONResponse({"ok": True, "repeat": True})
        if session["state"] != LIVE:
            return _err(409, "not_live", "the session is not live")
        now = bridge.clock()
        if now - bridge.last_speak.get(session["id"], -SPEAK_GAP_S) < SPEAK_GAP_S:
            return _err(429, "too_fast", "one whisper every 2 seconds")
        bridge.last_speak[session["id"]] = now
        n = body.get("n")
        seen[cmd_id] = None
        if len(seen) > SEEN_MAX:
            seen.pop(next(iter(seen)))
        store.enqueue(session["id"], "speak", {"n": n if isinstance(n, int) else None})
        return JSONResponse({"ok": True})

    @app.post("/v1/sessions/{ref}/hangup")
    async def v1_hangup(ref: str, request: Request) -> Response:
        tenant = await caller(request)
        if isinstance(tenant, JSONResponse):
            return tenant
        session = owned(tenant, ref)
        if session is None:
            return _err(404, "not_found", "no such session")
        if session["state"] != ENDED:
            store.request_hangup(session["id"])
            store.enqueue(session["id"], "hangup")
        return JSONResponse({"ok": True, "state": session["state"]})

    @app.post("/telnyx/webhook")
    async def telnyx_webhook(request: Request) -> Response:
        raw = await request.body()
        if not settings.telnyx_public_key or not telnyx_io.verify(
                settings.telnyx_public_key, raw, request.headers):
            return PlainTextResponse("invalid signature", status_code=403)
        try:
            data = (json.loads(raw) or {}).get("data") or {}
        except (ValueError, AttributeError):
            return JSONResponse({"ok": True})
        if isinstance(data, dict) and isinstance(data.get("payload"), dict):
            await bridge.on_phone_event(str(data.get("event_type") or ""), data["payload"])
        return JSONResponse({"ok": True})

    @app.websocket("/telnyx/{sid}/media/{role}")
    async def telnyx_media(ws: WebSocket, sid: str, role: str, t: str = "") -> None:
        await ws.accept()
        session = store.get_session(sid) if role in ROLES and len(sid) <= 32 else None
        if (session is None or session["mode"] != "phone" or session["state"] not in
                ACTIVE_STATES or session.get("consent_at") is None
                or not store.consume_token(sid, role, "media", t)):
            await _close(ws, 1008)
            return
        rt = bridge.runtime(sid)
        if rt is None:
            await _close(ws, 1008)
            return
        await rt.attach(role, None)  # receive-only
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


# ---- helpers ------------------------------------------------------------------------------
def _over(hits: dict[str, deque[float]], key: str, limit: int, now: float) -> bool:
    """Sliding-window limit: True when ``key`` already had ``limit`` hits in the window."""
    q = hits.setdefault(key, deque())
    while q and now - q[0] > RATE_WINDOW_S:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    if len(hits) > 1024:
        for k in [k for k, v in hits.items() if not v or now - v[-1] > RATE_WINDOW_S]:
            hits.pop(k, None)
    return False


def _client_ip(request: Request, trust_proxy: bool) -> str:
    key = request.client.host if request.client else "?"
    if trust_proxy:
        key = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or key
    return key


class _StripPrefix:
    """Serve the same routes with or without the proxy's path prefix (``/bridge/ghost``), so it
    works whether the proxy strips the prefix or passes it through."""

    def __init__(self, app: Any, prefix: str) -> None:
        self.app, self.prefix = app, prefix

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path") or ""
        if scope["type"] in ("http", "websocket") and (
                path == self.prefix or path.startswith(self.prefix + "/")):
            path = path[len(self.prefix):] or "/"
            scope = {**scope, "path": path, "raw_path": path.encode("utf-8")}
        await self.app(scope, receive, send)


def factory() -> FastAPI:
    """``uvicorn --factory hermes_ghost_call.bridge.app:factory``: config from env and the
    credentials directory only."""
    return create_app(BridgeSettings.from_env())


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
