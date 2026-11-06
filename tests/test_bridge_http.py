"""FastAPI routes: healthz, one-time links, CSP, websocket auth and relay, rate limiting."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_ghost_call.bridge.app import create_app
from hermes_ghost_call.bridge.settings import BridgeSettings
from hermes_ghost_call.session import ENDED

BASE = "http://testserver"


@pytest.fixture
def settings():
    return BridgeSettings(public_base_url=BASE, tick_s=0.02)


@pytest.fixture
def app(settings, store):
    return create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                      janitor=False)


def new_session(store, mode="web"):
    s = store.create_session(goal="book a table <b>Friday</b>", callee_label="Bella",
                             mode=mode, config={"user_name": "Winch", "max_duration_s": 900})
    toks = {r: store.mint_token(s["id"], r, "link", 600) for r in ("you", "them")}
    return s["id"], toks


def test_healthz(app):
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"ok": True, "service": "hermes-ghost-call"}


def test_pages_need_a_valid_token_and_escape_input(app, store):
    sid, toks = new_session(store)
    with TestClient(app) as c:
        assert c.get(f"/g/{sid}/you?t=wrong").status_code == 404
        assert c.get(f"/g/nope/you?t={toks['you']}").status_code == 404
        assert c.get(f"/g/{sid}/them?t={toks['you']}").status_code == 404  # role-bound
        r = c.get(f"/g/{sid}/you?t={toks['you']}")
        assert r.status_code == 200
        assert "Ghost is listening (muted to the other party)" in r.text
        assert "headphones" in r.text.lower()
        assert "<b>Friday</b>" not in r.text and "&lt;b&gt;Friday" in r.text
        csp = r.headers["content-security-policy"]
        assert "script-src 'nonce-" in csp and "default-src 'none'" in csp
        assert r.headers["cache-control"] == "no-store"
        them = c.get(f"/g/{sid}/them?t={toks['them']}")
        assert them.status_code == 200 and "Ghost" not in them.text.split("<script")[0]


def test_join_is_single_use(app, store):
    sid, toks = new_session(store)
    with TestClient(app) as c:
        r = c.post(f"/g/{sid}/join/you", json={"t": toks["you"]})
        assert r.status_code == 200 and r.json()["media_token"]
        assert c.post(f"/g/{sid}/join/you", json={"t": toks["you"]}).status_code == 404
        assert c.get(f"/g/{sid}/you?t={toks['you']}").status_code == 404
        assert c.post(f"/g/{sid}/join/them", json={"t": "x"}).status_code == 404
        assert c.post(f"/g/{sid}/join/them", content=b"not json").status_code == 404


def test_twilio_sessions_have_no_web_pages(app, store):
    sid, toks = new_session(store, mode="twilio")
    with TestClient(app) as c:
        assert c.get(f"/g/{sid}/you?t={toks['you']}").status_code == 404


def test_websocket_needs_media_token_and_relays(app, store):
    sid, toks = new_session(store)
    with TestClient(app) as c:
        m_you = c.post(f"/g/{sid}/join/you", json={"t": toks["you"]}).json()["media_token"]
        m_them = c.post(f"/g/{sid}/join/them", json={"t": toks["them"]}).json()["media_token"]
        with c.websocket_connect(f"/g/{sid}/ws/you") as bad:
            bad.send_text(json.dumps({"event": "start", "token": m_them}))  # wrong role
            with pytest.raises(WebSocketDisconnect):
                bad.receive_text()
        with c.websocket_connect(f"/g/{sid}/ws/you") as you, \
                c.websocket_connect(f"/g/{sid}/ws/them") as them:
            you.send_text(json.dumps({"event": "start", "token": m_you}))
            them.send_text(json.dumps({"event": "start", "token": m_them}))
            frame = bytes([0x55]) * 160
            you.send_text(json.dumps({"event": "media",
                                      "payload": base64.b64encode(frame).decode()}))
            got = None
            for _ in range(6):
                msg = json.loads(them.receive_text())
                if msg["event"] == "media":
                    got = base64.b64decode(msg["payload"])
                    break
            assert got == frame
            assert store.get_session(sid)["state"] == "live"
            them.send_text(json.dumps({"event": "stop"}))
        assert store.get_session(sid)["state"] == ENDED


def test_rate_limit_on_stable_host_key(app, store, settings):
    sid, _ = new_session(store)
    with TestClient(app) as c:
        codes = [c.get(f"/g/{sid}/you?t=x", headers={"X-Forwarded-For": f"10.0.0.{i}"}
                       ).status_code for i in range(45)]
        assert codes[-1] == 429  # spoofed X-Forwarded-For is ignored without TRUST_PROXY


def test_trust_proxy_uses_forwarded_for(settings, store):
    settings.trust_proxy = True
    app = create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                     janitor=False)
    sid, _ = new_session(store)
    with TestClient(app) as c:
        codes = [c.get(f"/g/{sid}/you?t=x", headers={"X-Forwarded-For": f"10.0.0.{i}"}
                       ).status_code for i in range(45)]
        assert 429 not in codes


async def test_janitor_settles_expired_and_hung_up_sessions(settings, store, clock):
    app = create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                     janitor=False)
    bridge = app.state.bridge
    old, _ = new_session(store)
    clock.advance(700)
    hung, _ = new_session(store)
    store.request_hangup(hung)
    await bridge.janitor_once()
    assert store.get_session(old)["end_reason"] == "link_expired"
    assert store.get_session(hung)["end_reason"] == "hangup"
    assert store.get_session(old)["summary"]["source"] == "fallback"
