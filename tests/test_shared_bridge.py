"""Shared bridge: the workspace API, tenant isolation, consent, phone mode on Telnyx, the
per-session hint model, usage, white-label text, and the plugin client end to end. No network:
the plugin's HTTP goes straight into the FastAPI app, Telnyx and the models are fakes."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import FakeCtx, FakeLLM, FakeStt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_ghost_call import commands, hooks, remote, safety, tools
from hermes_ghost_call import summary as summarymod
from hermes_ghost_call.bridge import auth as authmod
from hermes_ghost_call.bridge import live, telnyx_io, webpage
from hermes_ghost_call.bridge.app import create_app, factory
from hermes_ghost_call.bridge.live import GhostRuntime
from hermes_ghost_call.bridge.settings import BridgeSettings, parse_overrides
from hermes_ghost_call.session import ENDED, LIVE, Store
from hermes_ghost_call.watcher import ticker_text

SIGNING_KEY = "test-signing-key-not-a-secret"
TOKEN_A = authmod.mint(SIGNING_KEY, "ws-a")
TOKEN_B = authmod.mint(SIGNING_KEY, "ws-b")
HINT_KEY = "sk-or-MARKER-workspace-hint-key"
BASE = "https://ghost.example.test"
VENDORS = re.compile(r"openai|twilio|telnyx|openrouter|hermes|gpt|anthropic|claude|linq|nous",
                     re.IGNORECASE)
SIGNER = Ed25519PrivateKey.generate()
PUBLIC_KEY = base64.b64encode(SIGNER.public_key().public_bytes(
    Encoding.Raw, PublicFormat.Raw)).decode()


def auth(token: str = TOKEN_A) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signed(body: dict, *, stamp: int | None = None, key=SIGNER) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if stamp is None else stamp)
    sig = base64.b64encode(key.sign(ts.encode() + b"|" + raw)).decode()
    return raw, {"telnyx-timestamp": ts, "telnyx-signature-ed25519": sig,
                 "content-type": "application/json"}


class FakeTelnyx:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def dial(self, *, to, from_, webhook_url, state):
        leg = telnyx_io.read_client_state(state)["leg"]
        self.calls.append(("dial", leg, to, from_))
        return f"cc-{leg}"

    async def speak(self, ccid, text, *, state="", self_only=False):
        self.calls.append(("speak", ccid, text, self_only))

    async def bridge(self, ccid, other):
        self.calls.append(("bridge", ccid, other))

    async def stream(self, ccid, url):
        self.calls.append(("stream", ccid, url))

    async def hangup(self, ccid):
        self.calls.append(("hangup", ccid))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def settings():
    overrides = parse_overrides(json.dumps({
        "ws-a": {"hint_base_url": "https://hints.example.test/v1", "hint_model": "model-a",
                 "daily_session_limit": 3},
        "ws-off": {"disabled": True}}))
    return BridgeSettings(public_base_url=BASE, tick_s=0.02, signing_key=SIGNING_KEY,
                          overrides=overrides,
                          telnyx_public_key=PUBLIC_KEY, caller_number="+12125550100",
                          environ={"HERMES_GHOST_ALLOW_ALL": "1"})


@pytest.fixture
def phone():
    return FakeTelnyx()


@pytest.fixture
def app(settings, store, phone):
    return create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                      telnyx=phone, janitor=False)


def create(c, ref="loc1", token=TOKEN_A, **body):
    payload = {"goal": "lower my bill, target $60", "callee_label": "Northwind", "mode": "web",
               "config": {}, "hint": {"api_key": HINT_KEY}, **body}
    return c.put(f"/v1/sessions/{ref}", json=payload, headers=auth(token))


def path_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


# ---- tenants and auth -------------------------------------------------------------------------
def test_overrides_are_validated():
    assert parse_overrides("") == {}
    for bad in ('[]', '{"Bad Name": {}}', '{"a": []}', '{"a": {"hint_base_url": "http://x"}}'):
        with pytest.raises(ValueError):
            parse_overrides(bad)


def test_signed_tokens(app):
    ws, sig = TOKEN_A.split(".")
    wrong_key = authmod.mint("some-other-key", "ws-a")
    swapped = f"ws-b.{sig}"  # ws-a's signature under ws-b's name
    with TestClient(app) as c:
        assert c.get("/v1/health", headers=auth()).status_code == 200
        for bad in (wrong_key, swapped, ws, f"{ws}.", f"WS-A.{sig}", f"{TOKEN_A}x",
                    f"-ws.{sig}", "", "a" * 600):
            assert c.get("/v1/health", headers=auth(bad)).status_code == 401, bad
        off = c.get("/v1/health", headers=auth(authmod.mint(SIGNING_KEY, "ws-off")))
        assert off.status_code == 403 and off.json()["error"]["code"] == "disabled"
        assert create(c, token=authmod.mint(SIGNING_KEY, "ws-off")).status_code == 403
    with pytest.raises(ValueError):
        authmod.mint(SIGNING_KEY, "Not_Valid")
    with pytest.raises(ValueError):
        authmod.mint("", "ws-a")


def test_no_signing_key_refuses_every_signed_token(settings, store, phone):
    settings.signing_key = ""
    app = create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                     telnyx=phone, janitor=False)
    with TestClient(app) as c:
        assert c.get("/v1/health", headers=auth()).status_code == 401


def test_bridge_token_cli_mints_from_the_credentials_dir(tmp_path, capsys):
    from hermes_ghost_call import cli

    (tmp_path / "GHOST_CALL_BRIDGE_SIGNING_KEY").write_text(SIGNING_KEY + "\n")
    env = {"CREDENTIALS_DIRECTORY": str(tmp_path), "GHOST_CALL_BRIDGE_SIGNING_KEY": "ignored"}
    assert cli.bridge_token("ws-a", env) == 0
    assert capsys.readouterr().out.strip() == TOKEN_A
    assert cli.bridge_token("ws-a", {"GHOST_CALL_BRIDGE_SIGNING_KEY": SIGNING_KEY}) == 0
    assert capsys.readouterr().out.strip() == TOKEN_A
    assert cli.bridge_token("Bad!", env) == 2
    assert cli.bridge_token("ws-a", {}) == 2


# ---- usage-token auth against a fake TeamShift auth service -------------------------------------
USAGE_NAME = "t-0123456789"
USAGE_TOKEN = "ts_usg_" + "ab" * 32


class FakeAuth:
    """A real HTTP server on loopback that answers like /api/workspace/usage."""

    def __init__(self) -> None:
        import http.server
        import threading

        self.status, self.delay, self.hits = 200, 0.0, []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fake.hits.append((self.path, self.headers.get("authorization"),
                                  self.headers.get("x-teamshift-workspace")))
                time.sleep(fake.delay)
                ok = (self.headers.get("authorization") == f"Bearer {USAGE_TOKEN}"
                      and self.headers.get("x-teamshift-workspace") == USAGE_NAME)
                code = fake.status if ok or fake.status != 200 else 401
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_auth():
    server = FakeAuth()
    yield server
    server.close()


def usage_hdr(token=USAGE_TOKEN, name=USAGE_NAME) -> dict:
    return {"Authorization": f"Bearer {token}", "X-TeamShift-Workspace": name}


def usage_app(settings, store, phone, fake_auth, clock=None):
    settings.auth_url = fake_auth.url
    settings.auth_timeout_s = 0.5
    return create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                      telnyx=phone, janitor=False, clock=clock)


def test_usage_token_valid_and_cached(settings, store, phone, fake_auth, clock):
    app = usage_app(settings, store, phone, fake_auth, clock)
    with TestClient(app) as c:
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 200
        assert fake_auth.hits == [("/api/workspace/usage", f"Bearer {USAGE_TOKEN}", USAGE_NAME)]
        assert c.put("/v1/sessions/u1", json={"goal": "g"}, headers=usage_hdr()).status_code == 201
        assert store.tenant_session(USAGE_NAME, "u1") is not None
        assert len(fake_auth.hits) == 1  # cache hit
        fake_auth.status = 500  # the cache still answers within 10 minutes...
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 200
        clock.advance(601)  # ...and after that the auth service is asked again
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401
        assert len(fake_auth.hits) == 2


def test_usage_token_refusals_fail_closed_and_are_not_cached(settings, store, phone, fake_auth):
    app = usage_app(settings, store, phone, fake_auth)
    with TestClient(app) as c:
        fake_auth.status = 401
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401
        assert len(fake_auth.hits) == 2  # a refusal is never cached
        fake_auth.status = 503
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401
        fake_auth.status, fake_auth.delay = 200, 1.0  # slower than the 0.5 s timeout
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401
        fake_auth.delay = 0.0
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 200
        n = len(fake_auth.hits)
        for hdr in (usage_hdr(name="t-012345678"), usage_hdr(name="ws-a"),
                    usage_hdr(name=""), usage_hdr(token=USAGE_TOKEN + "0"),
                    usage_hdr(token="ts_usg_" + "AB" * 32),
                    {"Authorization": f"Bearer {USAGE_TOKEN}"}):
            assert c.get("/v1/health", headers=hdr).status_code == 401
        assert len(fake_auth.hits) == n  # bad shapes never reach the auth service
        assert c.get("/v1/health", headers=usage_hdr(token="ts_usg_" + "cd" * 32)
                     ).status_code == 401  # well formed, wrong token


def test_usage_path_is_off_without_an_auth_url(app):
    with TestClient(app) as c:
        assert c.get("/v1/health", headers=usage_hdr()).status_code == 401


def test_plugin_sends_usage_token_when_no_bridge_token():
    env = {remote.URL_ENV: BASE, remote.USAGE_TOKEN_ENV: USAGE_TOKEN,
           remote.WORKSPACE_ENV: USAGE_NAME}
    assert remote.credentials(env) == usage_hdr()
    assert remote.configured(env)
    assert remote.credentials({**env, remote.TOKEN_ENV: TOKEN_A}) == auth()
    assert not remote.configured({remote.URL_ENV: BASE, remote.USAGE_TOKEN_ENV: USAGE_TOKEN})


# ---- deploy shape: factory, prefix, credentials directory ------------------------------------
def test_factory_reads_secrets_only_from_the_credentials_dir(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    for name, value in (("OPENAI_API_KEY", "file-openai"), ("GHOST_CALL_TELNYX_API_KEY", "file-tx"),
                        ("GHOST_CALL_TELNYX_PUBLIC_KEY", PUBLIC_KEY),
                        ("GHOST_CALL_BRIDGE_SIGNING_KEY", SIGNING_KEY)):
        (creds / name).write_text(value + "\n")
    env = {"CREDENTIALS_DIRECTORY": str(creds), "OPENAI_API_KEY": "env-openai",
           "GHOST_CALL_BRIDGE_PUBLIC_URL": "https://voice.example.test/bridge/ghost/",
           "PUBLIC_BASE_URL": "https://old.example.test", "HERMES_GHOST_DB": str(tmp_path / "g.db")}
    s = BridgeSettings.from_env(env)
    assert (s.openai_api_key, s.telnyx_api_key, s.telnyx_public_key, s.signing_key) == (
        "file-openai", "file-tx", PUBLIC_KEY, SIGNING_KEY)
    assert s.public_base_url == "https://voice.example.test/bridge/ghost"
    assert s.base_path() == "/bridge/ghost"
    assert BridgeSettings.from_env({"PUBLIC_BASE_URL": BASE}).public_base_url == BASE  # alias
    assert BridgeSettings.from_env({"CREDENTIALS_DIRECTORY": str(tmp_path / "none"),
                                    "OPENAI_API_KEY": "env"}).openai_api_key == ""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(factory()) as c:
        assert c.get("/bridge/ghost/v1/health", headers=auth()).status_code == 200


def test_routes_and_pages_work_under_the_proxy_prefix(settings, store, phone):
    settings.public_base_url = "https://voice.example.test/bridge/ghost"
    app = create_app(settings, store=store, client=None, stt_connect=None, twilio=None,
                     telnyx=phone, janitor=False)
    with TestClient(app) as c:
        link = create(c).json()["links"]["you"]
        assert link.startswith("https://voice.example.test/bridge/ghost/g/")
        page = c.get(path_of(link))  # proxy passes the prefix through
        assert page.status_code == 200
        assert "connect-src 'self' wss://voice.example.test;" in page.headers[
            "content-security-policy"]
        assert c.get(path_of(link).removeprefix("/bridge/ghost")).status_code == 200  # stripped
        assert c.get("/bridge/ghost/v1/health", headers=auth()).status_code == 200
        assert c.get("/bridge/ghostly/v1/health", headers=auth()).status_code == 404


def test_every_route_needs_a_known_workspace_token(app):
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 401
        assert c.get("/v1/health", headers=auth("nope")).status_code == 401
        assert c.get("/v1/health", headers=auth()).json()["ok"] is True
        assert create(c, token="nope").status_code == 401
        assert c.get("/v1/sessions/loc1/updates", headers=auth("nope")).status_code == 401


# ---- create, idempotency, bridge-side limits --------------------------------------------------
def test_create_serves_links_from_the_bridge_and_holds_the_key_in_memory_only(app, store):
    with TestClient(app) as c:
        r = create(c, config={"max_duration_s": 99999, "store_transcripts": True})
        assert r.status_code == 201
        body = r.json()
        assert set(body["links"]) == {"you", "them"}
        assert all(u.startswith(f"{BASE}/g/") for u in body["links"].values())
        again = create(c)
        assert again.status_code == 200 and again.json() == body  # idempotent by session id
        s = store.tenant_session("ws-a", "loc1")
        assert s["config"]["max_duration_s"] == 3600  # clamped on the bridge
        assert s["config"]["hint_timeout_ms"] == 2500
        assert s["config"]["store_transcripts"] is False
        assert s["config"]["model"] == "model-a"
        assert app.state.bridge.hint_keys[s["id"]] == HINT_KEY
        page = c.get(path_of(body["links"]["you"]))
        assert page.status_code == 200 and webpage.CONSENT_LINE in page.text
    db = b"".join(p.read_bytes() for p in Path(store.path).parent.glob("ghost.db*"))
    assert HINT_KEY.encode() not in db


def test_bridge_enforces_one_session_daily_cap_modes_and_numbers(app, store, clock):
    with TestClient(app) as c:
        assert create(c, mode="intervene").status_code == 422
        assert create(c, goal="").status_code == 422
        assert create(c, ref="bad id").status_code == 422
        assert create(c, ref="p1", mode="phone",
                      numbers={"you": "+12125550123", "them": "911"}).json()["error"][
            "code"] == "number_refused"
        assert create(c, ref="a1").status_code == 201
        assert create(c, ref="a2").json()["error"]["code"] == "session_active"
        store.end_once(store.tenant_session("ws-a", "a1")["id"], "hangup")
        for ref in ("b1", "b2"):  # the tenant's cap is 3 a day
            assert create(c, ref=ref).status_code == 201
            store.end_once(store.tenant_session("ws-a", ref)["id"], "hangup")
        assert create(c, ref="c9").json()["error"]["code"] == "daily_limit"
        assert create(c, ref="c9", token=TOKEN_B).status_code == 201  # caps are per workspace


# ---- isolation ----------------------------------------------------------------------------------
def test_workspace_b_cannot_read_or_touch_workspace_a(app, store):
    with TestClient(app) as c:
        assert create(c, ref="shared-id").status_code == 201
        a = store.tenant_session("ws-a", "shared-id")
        b_hdr = auth(TOKEN_B)
        assert c.get("/v1/sessions/shared-id/updates", headers=b_hdr).status_code == 404
        assert c.post("/v1/sessions/shared-id/hangup", headers=b_hdr).status_code == 404
        assert c.put("/v1/sessions/shared-id/commands/c1", json={"kind": "speak"},
                     headers=b_hdr).status_code == 404
        r = create(c, ref="shared-id", token=TOKEN_B)  # same id, its own session
        assert r.status_code == 201
        b = store.tenant_session("ws-b", "shared-id")
        assert b["id"] != a["id"] and b["workspace"] == "ws-b"
        assert store.get_session(a["id"])["state"] == "created"
        assert not store.get_session(a["id"]).get("hangup_requested")
        ua = c.get("/v1/sessions/shared-id/updates", headers=auth()).json()
        ub = c.get("/v1/sessions/shared-id/updates", headers=b_hdr).json()
        assert ua["session"]["state"] == ub["session"]["state"] == "created"
        store.add_hint(a["id"], 1.0, "they said $70 → counter")
        assert c.get("/v1/sessions/shared-id/updates", headers=b_hdr).json()["hints"] == []


# ---- updates, commands, usage -------------------------------------------------------------------
def test_updates_cursor_commands_and_usage(app, store):
    with TestClient(app) as c:
        create(c)
        sid = store.tenant_session("ws-a", "loc1")["id"]
        store.mark_live(sid)
        store.add_hint(sid, 3.0, "they said $70 → counter")
        store.add_hint(sid, 9.0, "ask about fees")
        store.set_usage(sid, {"stt_seconds": 12.5, "tts_chars": 40})
        first = c.get("/v1/sessions/loc1/updates", headers=auth()).json()
        assert [h["n"] for h in first["hints"]] == [1, 2] and first["cursor"] == "2"
        assert first["session"]["usage"] == {"stt_seconds": 12.5, "tts_chars": 40}
        nxt = c.get("/v1/sessions/loc1/updates?cursor=2", headers=auth()).json()
        assert nxt["hints"] == [] and nxt["cursor"] == "2"
        cmd = "/v1/sessions/loc1/commands/c7"
        assert c.put(cmd, json={"kind": "intervene", "text": "hi"},
                     headers=auth()).status_code == 422
        assert c.put(cmd, json={"kind": "speak", "n": 2}, headers=auth()).json() == {"ok": True}
        assert c.put(cmd, json={"kind": "speak", "n": 2}, headers=auth()).json()["repeat"]
        taken = store.take_commands(sid)
        assert [(t["kind"], t["payload"]) for t in taken] == [("speak", {"n": 2})]
        assert c.post("/v1/sessions/loc1/hangup", headers=auth()).status_code == 200
        assert store.get_session(sid)["hangup_requested"]


async def test_runtime_meters_transcription_seconds_and_speech_chars(store, settings):
    s = store.create_session(goal="g", callee_label="x", mode="web", workspace="ws-a",
                             client_ref="r", config={"max_duration_s": 900, "voice": "v"})
    store.mark_consent(s["id"])
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=FakeLLM(),
                      stt_connect=FakeStt(), hint_client=None)
    await rt.attach("you", lambda m: asyncio.sleep(0))
    for _ in range(50):
        await rt.on_audio("you", bytes(160))  # 50 x 20 ms
    store.add_hint(s["id"], 1.0, "say Winch")
    await rt.speak()
    await rt.end("hangup")
    assert store.get_session(s["id"])["usage"] == {"stt_seconds": 1.0, "tts_chars": 9}


# ---- consent (room and web) -------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["web", "room"])
async def test_nothing_is_transcribed_before_the_user_confirms(store, settings, mode):
    s = store.create_session(goal="g", callee_label="x", mode=mode, config={})
    stt = FakeStt()
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=None, stt_connect=stt)
    sent: list[dict] = []

    async def you(msg):
        sent.append(msg)

    await rt.attach("you", you)
    for _ in range(20):
        await rt.on_audio("you", bytes([0x55]) * 160)
    await asyncio.sleep(0.05)
    appends = [e for e in stt.conns["you"].sent if e["type"] == "input_audio_buffer.append"]
    assert appends == [] and rt.usage["stt_seconds"] == 0
    await rt.on_client_event("them", {"event": "consent"})  # only the user can confirm
    assert not rt.consented
    await rt.on_client_event("you", {"event": "consent"})
    assert store.get_session(s["id"])["consent_at"] is not None
    assert {"event": "consented"} in sent
    for _ in range(10):
        await rt.on_audio("you", bytes([0x55]) * 160)
    await asyncio.sleep(0.05)
    assert any(e["type"] == "input_audio_buffer.append" for e in stt.conns["you"].sent)
    await rt.end("hangup")


def test_user_pages_show_the_consent_line_and_a_confirm_button():
    for room in (False, True):
        html = webpage.render_you(sid="g1", goal="g", callee_label="x", nonce="n", room=room)
        assert "Let the other person know this call is being transcribed" in html
        assert 'id="consent-ok"' in html and "send({ event: 'consent' })" in html
    assert 'id="consent-ok"' not in webpage.render_them(sid="g1", caller="a", nonce="n")


# ---- phone mode on Telnyx -----------------------------------------------------------------------
def event(kind: str, sid: str, leg: str, ccid: str, extra: str = "") -> dict:
    return {"data": {"event_type": kind, "payload": {
        "call_control_id": ccid, "client_state": telnyx_io.client_state(sid, leg, extra)}}}


def post_event(c, body: dict, **kw):
    raw, hdr = signed(body, **kw)
    return c.post("/telnyx/webhook", content=raw, headers=hdr)


def test_webhooks_fail_closed_on_a_bad_stale_or_missing_signature(app, settings, store):
    with TestClient(app) as c:
        body = event("call.answered", "gx", "you", "cc-you")
        assert c.post("/telnyx/webhook", json=body).status_code == 403
        assert post_event(c, body, stamp=int(time.time()) - 900).status_code == 403
        assert post_event(c, body, key=Ed25519PrivateKey.generate()).status_code == 403
        raw, hdr = signed(body)
        assert c.post("/telnyx/webhook", content=raw + b" ", headers=hdr).status_code == 403
        assert post_event(c, body).status_code == 200
    settings.telnyx_public_key = ""
    with TestClient(create_app(settings, store=store, client=None, stt_connect=None,
                               twilio=None, telnyx=FakeTelnyx(), janitor=False)) as c:
        assert post_event(c, body).status_code == 403


def test_phone_flow_discloses_before_any_audio_reaches_transcription(app, store, phone):
    with TestClient(app) as c:
        r = create(c, mode="phone", numbers={"you": "+12125550123", "them": "+13105550199"})
        assert r.status_code == 201 and r.json()["links"] == {}
        sid = store.tenant_session("ws-a", "loc1")["id"]
        assert phone.calls == [("dial", "you", "+12125550123", "+12125550100")]
        post_event(c, event("call.answered", sid, "you", "cc-you"))
        assert phone.calls[-1] == ("dial", "them", "+13105550199", "+12125550100")
        post_event(c, event("call.answered", sid, "them", "cc-them"))
        post_event(c, event("call.answered", sid, "them", "cc-them"))  # replay: one disclosure
        speaks = [x for x in phone.calls if x[0] == "speak"]
        assert speaks == [("speak", "cc-them", telnyx_io.DISCLOSURE, False)]
        assert "stream" not in phone.kinds() and "bridge" not in phone.kinds()
        early = store.mint_token(sid, "them", "media", 60)
        with c.websocket_connect(f"/telnyx/{sid}/media/them?t={early}") as ws, \
                pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # no audio is accepted before the disclosure has played
        post_event(c, event("call.answered", sid, "you", "cc-forged"))  # unknown leg id
        post_event(c, event("call.speak.ended", sid, "them", "cc-them", "disclosure"))
        i_speak = phone.kinds().index("speak")
        assert phone.kinds()[i_speak + 1:] == ["bridge", "stream", "stream"]
        assert store.get_session(sid)["consent_at"] is not None
        assert store.get_session(sid)["usage"]["tts_chars"] == len(telnyx_io.DISCLOSURE)
        urls = {x[1]: x[2] for x in phone.calls if x[0] == "stream"}
        assert set(urls) == {"cc-you", "cc-them"}
        with c.websocket_connect(path_of(urls["cc-you"]).replace("wss://", "")) as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(json.dumps({"event": "media", "media": {
                "track": "inbound", "payload": base64.b64encode(bytes(160)).decode()}}))
            for _ in range(50):
                if store.get_session(sid)["state"] == LIVE:
                    break
                time.sleep(0.02)
            assert store.get_session(sid)["state"] == LIVE
            post_event(c, event("call.hangup", sid, "them", "cc-them"))
        assert store.get_session(sid)["state"] == ENDED
        assert store.get_session(sid)["end_reason"] == "them_hung_up"
        assert ("hangup", "cc-you") in phone.calls


async def test_phone_whisper_plays_on_the_users_leg_only(store, settings, phone):
    s = store.create_session(goal="g", callee_label="x", mode="phone", workspace="ws-a",
                             client_ref="r", config={}, provider={"you_ccid": "cc-you",
                                                                   "them_ccid": "cc-them"})
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=None, stt_connect=None,
                      telnyx=phone)
    await rt.attach("you", None)
    store.add_hint(s["id"], 1.0, "say Winch")
    await rt.speak()
    assert ("speak", "cc-you", "say Winch", True) in phone.calls
    assert not any(x[0] == "speak" and x[1] == "cc-them" for x in phone.calls)
    assert (await rt.intervene("hello")).startswith("refused")
    await rt.end("hangup")
    assert {("hangup", "cc-you"), ("hangup", "cc-them")} <= set(phone.calls)


# ---- hint model: the workspace's key, the tenant's base URL, memory only ------------------------
async def test_hint_client_uses_the_workspace_key_and_is_forgotten_after(app, store):
    bridge = app.state.bridge
    with TestClient(app) as c:
        create(c)
    sid = store.tenant_session("ws-a", "loc1")["id"]
    rt = bridge.runtime(sid)
    assert rt.hint_client is not rt.client
    assert rt.hint_client._key == HINT_KEY
    assert rt.hint_client._base == "https://hints.example.test/v1"
    assert rt.engine is None or rt.engine.timeout_s == 2.5
    await rt.end("hangup")
    await rt.summary_task
    assert sid not in bridge.hint_keys
    bridge.runtimes.clear()
    other = store.create_session(goal="g", callee_label="", mode="web", config={},
                                 workspace="ws-b", client_ref="nokey")
    assert bridge.runtime(other["id"]).hint_client is None  # never the bridge's own key


# ---- intervene is off ---------------------------------------------------------------------------
def test_intervene_is_disabled_for_shared_sessions(store):
    s = store.create_session(goal="g", callee_label="", mode="web", config={}, workspace="ws")
    store.mark_live(s["id"])
    store.set_intervene(s["id"], True)
    assert "disabled" in safety.intervene_refusal(store.get_session(s["id"]), unattended=False)
    local = {"id": "g1", "state": "live", "mode": "web", "intervene_allowed": True,
             "provider": {"remote": True}}
    assert "disabled" in safety.intervene_refusal(local, unattended=False)


# ---- white-label --------------------------------------------------------------------------------
def test_no_vendor_names_on_anything_the_user_or_the_model_reads():
    texts = [webpage.render_you(sid="g1", goal="g", callee_label="x", nonce="n"),
             webpage.render_you(sid="g1", goal="g", callee_label="x", nonce="n", room=True),
             webpage.render_them(sid="g1", caller="a", nonce="n"), webpage.render_error(),
             telnyx_io.DISCLOSURE, webpage.CONSENT_LINE, *live._NO_HINT.values(),
             summarymod.render({"callee_label": "x"}, summarymod.fallback("no model configured")),
             ticker_text({"callee_label": "x"}, ["00:01 · ask about fees"], state=LIVE),
             (Path(__file__).resolve().parent.parent / "skills" / "ghost-call" /
              "SKILL.md").read_text()]
    src = (Path(live.__file__).read_text())
    texts += re.findall(r'"text":\s*"([^"]+)"', src)  # every notice sent to the user's page
    for text in texts:
        assert not VENDORS.search(text), VENDORS.search(text)


# ---- the plugin client, end to end against the bridge app ---------------------------------------
class AppOpener:
    """urllib opener that serves requests from a TestClient (and records them)."""

    def __init__(self, client: TestClient) -> None:
        self.c = client
        self.seen: list[tuple[str, str, dict]] = []

    def open(self, req, timeout=None):
        url = urlsplit(req.full_url)
        body = json.loads(req.data) if req.data else None
        self.seen.append((req.get_method(), url.path, body or {}))
        r = self.c.request(req.get_method(), url.path + (f"?{url.query}" if url.query else ""),
                           headers=dict(req.header_items()), content=req.data)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "err", {},
                                         io.BytesIO(r.content))
        return _Resp(r.content)


class _Resp(io.BytesIO):
    pass


@pytest.fixture
def plugin(tmp_path, clock):
    local = Store(path=tmp_path / "plugin" / "ghost.db", clock=clock)
    env = {remote.URL_ENV: BASE, remote.TOKEN_ENV: TOKEN_A, remote.HINT_KEY_ENV: HINT_KEY}
    svc = tools.configure(ctx=FakeCtx(config={"user_number": "+12125550123"}), store=local,
                          environ=env, clock=clock, start_watchers=False)
    yield svc
    tools._service = None
    local.close()


def test_plugin_starts_on_the_bridge_and_mirrors_hints_and_summary(app, store, plugin):
    with TestClient(app) as c:
        opener = AppOpener(c)
        plugin._remote = remote.RemoteBridge(plugin.environ, opener=opener, poll_s=0)
        reply = commands.ghost_command("start lower my bill with Northwind")
        assert "/g/" in reply and BASE in reply and "Them" in reply
        local = plugin.store.active_sessions()[0]
        method, path, body = opener.seen[0]
        assert (method, path) == ("PUT", f"/v1/sessions/{local['id']}")
        assert body["hint"]["api_key"] == HINT_KEY and body["mode"] == "web"
        bsid = store.tenant_session("ws-a", local["id"])["id"]
        store.mark_live(bsid)
        store.add_hint(bsid, 4.0, "they said $70 → counter")
        for _ in range(2):  # a replayed page mirrors nothing twice
            plugin.remote.sync(plugin.store, local["id"], force=True)
            plugin.store.set_provider(local["id"], {"bridge_cursor": ""})
        assert [h["text"] for h in plugin.store.hints(local["id"])] == ["they said $70 → counter"]
        assert plugin.store.get_session(local["id"])["state"] == LIVE
        out = json.loads(tools.HANDLERS["ghost_speak"]({}))
        assert out["ok"]
        plugin.remote.sync(plugin.store, local["id"], force=True)
        assert [x[1] for x in opener.seen if x[0] == "PUT"][-1].startswith(
            f"/v1/sessions/{local['id']}/commands/c")
        assert store.take_commands(bsid)[0]["kind"] == "speak"
        blocked = hooks.pre_tool_call("ghost_intervene", {"text": "hi"})
        assert blocked["action"] == "block"
        assert "disabled" in commands.ghost_command("intervene allow")
        store.set_usage(bsid, {"stt_seconds": 30.0, "tts_chars": 12})
        assert plugin.hangup(None)["state"] == ENDED
        assert store.get_session(bsid)["hangup_requested"]
        store.end_once(bsid, "hangup")
        store.set_summary_once(bsid, {**summarymod.fallback("x"), "agreed": "$60 for 12 months",
                                      "source": "model", "duration_s": 61, "hint_count": 1,
                                      "end_reason": "hangup", "open_questions": "fees?"})
        plugin.remote.sync(plugin.store, local["id"], force=True)
        mirrored = plugin.store.get_session(local["id"])
        assert mirrored["summary"]["agreed"] == "$60 for 12 months"
        assert mirrored["summary"]["open_questions"] == ["fees?"]
        assert mirrored["usage"] == {"stt_seconds": 30.0, "tts_chars": 12}


def test_plugin_phone_start_sends_both_numbers(app, store, plugin, phone):
    with TestClient(app) as c:
        plugin._remote = remote.RemoteBridge(plugin.environ, opener=AppOpener(c))
        out = plugin.start(goal="book a table", callee_number="+13105550199")
        assert out["ok"] and out["mode"] == "phone" and out["links"] == {}
        s = store.tenant_session("ws-a", out["session_id"])
        assert s["provider"]["them_number"] == "+13105550199"
        assert phone.calls[0][:3] == ("dial", "you", "+12125550123")


def test_plugin_refuses_plain_http_and_unset_means_upstream(plugin):
    plugin._environ = {**plugin.environ, remote.URL_ENV: "http://bridge.example.test"}
    plugin._remote = None
    out = plugin.start(goal="book a table")
    assert not out["ok"] and "https" in out["error"]
    assert plugin.store.latest_session()["end_reason"] == "bridge_error"
    plugin._environ = {"PUBLIC_BASE_URL": BASE, "OPENAI_API_KEY": "k"}
    plugin._remote = None
    assert plugin.remote is None
    out = plugin.start(goal="book a table")
    assert out["ok"] and "/g/" in out["links"]["you"]
    assert not (plugin.store.get_session(out["session_id"])["provider"] or {}).get("remote")


def test_plugin_ends_the_session_when_the_bridge_forgets_it(plugin):
    class Gone:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, io.BytesIO(b"{}"))

    s = plugin.store.create_session(goal="g", callee_label="", mode="web", config={},
                                    provider={"remote": True})
    remote.RemoteBridge(plugin.environ, opener=Gone()).sync(plugin.store, s["id"], force=True)
    assert plugin.store.get_session(s["id"])["end_reason"] == "bridge_rejected"


def test_store_migrates_an_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, goal TEXT NOT NULL, callee_label TEXT"
               " NOT NULL DEFAULT '', mode TEXT NOT NULL, state TEXT NOT NULL, origin_session"
               " TEXT NOT NULL DEFAULT '', config TEXT NOT NULL DEFAULT '{}', created_at REAL"
               " NOT NULL, live_at REAL, ended_at REAL, end_reason TEXT, intervene_allowed"
               " INTEGER NOT NULL DEFAULT 0, intervene_by TEXT, disclosed INTEGER NOT NULL"
               " DEFAULT 0, hangup_requested REAL, summary TEXT, summary_posted INTEGER NOT NULL"
               " DEFAULT 0, provider TEXT NOT NULL DEFAULT '{}')")
    db.execute("INSERT INTO sessions (id, goal, mode, state, created_at) VALUES"
               " ('g1', 'g', 'web', 'ended', 1)")
    db.commit()
    db.close()
    s = Store(path=path)
    assert s.get_session("g1")["usage"] == {} and s.get_session("g1")["workspace"] == ""
    s.close()
