"""Transcriber protocol, audio conversions, Twilio TwiML/signatures/dial guards."""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeStt
from fastapi.testclient import TestClient

from hermes_ghost_call.bridge import audio, stt, twilio_io
from hermes_ghost_call.bridge.app import create_app
from hermes_ghost_call.bridge.settings import BridgeSettings


# ---- audio ---------------------------------------------------------------------------------
def test_ulaw_round_trip_is_close():
    for s in (-30000, -1000, -10, 0, 10, 1000, 30000):
        u = audio._lin_to_ulaw(s)
        assert abs(audio.ULAW_TO_LIN[u] - s) <= max(16, abs(s) // 16)


def test_upsample_and_decimate_lengths():
    ulaw = bytes([0xFF]) * 160  # 20 ms of silence
    pcm = audio.ulaw_to_pcm24k(ulaw)
    assert len(pcm) == 160 * 3 * 2
    back = audio.pcm24k_to_ulaw(pcm)
    assert len(back) == 160 and set(back) <= {0xFF, 0x7F}


def test_frames_pad_last():
    out = audio.frames(b"\x00" * 170, 160)
    assert [len(f) for f in out] == [160, 160] and out[1].endswith(b"\xff" * 150)


# ---- stt -------------------------------------------------------------------------------------
def test_session_update_shape():
    ev = stt.session_update("gpt-4o-mini-transcribe")
    inp = ev["session"]["audio"]["input"]
    assert ev["type"] == "session.update" and ev["session"]["type"] == "transcription"
    assert inp["format"] == {"type": "audio/pcm", "rate": 24000}
    assert inp["turn_detection"]["type"] == "server_vad"
    assert inp["turn_detection"]["silence_duration_ms"] == 500
    assert stt.session_update("gpt-live-transcribe")["session"]["audio"]["input"][
        "turn_detection"] is None


async def test_transcriber_batches_audio_and_reports_utterances():
    finals, partials = [], []

    async def on_final(role, text):
        finals.append((role, text))

    async def on_partial(role, text):
        partials.append(text)

    connect = FakeStt()
    tr = stt.Transcriber("them", connect=connect, model="gpt-4o-mini-transcribe",
                         on_final=on_final, on_partial=on_partial)
    assert await tr.start()
    conn = connect.conns["them"]
    for _ in range(10):
        tr.feed(bytes([0xFF]) * 160)
    await asyncio.sleep(0.05)
    appends = [e for e in conn.sent if e["type"] == "input_audio_buffer.append"]
    assert len(appends) == 2  # 100 ms per append
    conn.inbox.put_nowait({"type": "conversation.item.input_audio_transcription.delta",
                           "item_id": "i1", "delta": "We only"})
    conn.inbox.put_nowait({"type": "conversation.item.input_audio_transcription.delta",
                           "item_id": "i1", "delta": " have"})
    conn.inbox.put_nowait({"type": "conversation.item.input_audio_transcription.completed",
                           "item_id": "i1", "transcript": " We only have  Saturday. "})
    await asyncio.sleep(0.05)
    assert partials == ["We only", "We only have"]
    assert finals == [("them", "We only have Saturday.")]
    await tr.close()


async def test_transcriber_connect_failure_is_contained():
    async def boom(role):
        raise OSError("Authorization: Bearer sk-SECRET")

    async def nop(role, text):
        return None

    tr = stt.Transcriber("you", connect=boom, model="m", on_final=nop)
    assert not await tr.start()
    assert tr.failed == "OSError" and "sk-SECRET" not in tr.failed
    tr.feed(b"\x00" * 160)  # ignored, no crash
    await tr.close()


async def test_client_vad_commits_after_silence():
    connect = FakeStt()

    async def nop(role, text):
        return None

    tr = stt.Transcriber("them", connect=connect, model="gpt-live-transcribe", on_final=nop)
    await tr.start()
    loud = bytes([0x00]) * 160  # mu-law 0x00 is near full scale
    quiet = bytes([0xFF]) * 160
    for _ in range(20):
        tr.feed(loud)
    for _ in range(30):
        tr.feed(quiet)
    await asyncio.sleep(0.05)
    commits = [e for e in connect.conns["them"].sent if e["type"] == "input_audio_buffer.commit"]
    assert len(commits) == 1
    await tr.close()


# ---- twilio ----------------------------------------------------------------------------------
def test_signature_round_trip():
    params = {"CallSid": "CA1", "CallStatus": "completed"}
    sig = twilio_io.signature("tok", "https://x/y", params)
    assert twilio_io.valid_signature("tok", "https://x/y", params, sig)
    assert not twilio_io.valid_signature("tok", "https://x/y", params, "bad")
    assert not twilio_io.valid_signature("", "https://x/y", params, sig)


def test_leg_twiml_is_receive_only_and_never_speaks_to_them():
    you = twilio_io.leg_twiml(sid="g1", role="you", ws_url="wss://b/twilio/g1/media/you",
                              token="t1", status_url="https://b/s")
    them = twilio_io.leg_twiml(sid="g1", role="them", ws_url="wss://b/twilio/g1/media/them",
                               token="t2", status_url="https://b/s")
    for xml in (you, them):
        assert "<Start><Stream" in xml and 'track="inbound_track"' in xml
        assert "<Connect>" not in xml  # a bidirectional stream could talk; we never use one
        assert "ghost-g1" in xml
    assert "<Say" in you and "<Say" not in them


@pytest.fixture
def tw_settings():
    return BridgeSettings(public_base_url="https://ghost.example.test",
                          twilio_account_sid="AC1", twilio_auth_token="tok",
                          twilio_from_number="+12125550100", tick_s=0.02,
                          environ={"HERMES_GHOST_TEST_ALLOWLIST": "+12125550111"})


class FakeTwilio:
    def __init__(self):
        self.calls = []
        self.announced = []

    async def create_call(self, *, to, from_, url, status_url):
        self.calls.append(to)
        return f"CA{len(self.calls)}"

    async def hangup(self, sid):
        return None

    async def conference_sid(self, name):
        return "CF1"

    async def announce(self, conf, call_sid, url):
        self.announced.append((call_sid, url))


def _twilio_session(store, callee="+12125550123"):
    return store.create_session(goal="g", callee_label="Bella", mode="twilio", config={},
                                provider={"user_number": "+12125550111",
                                          "callee_number": callee})["id"]


def _signed(settings, path, params):
    url = settings.public_base_url + path
    return {"X-Twilio-Signature": twilio_io.signature(settings.twilio_auth_token, url, params)}


def test_twiml_requires_signature(tw_settings, store):
    sid = _twilio_session(store)
    app = create_app(tw_settings, store=store, client=None, stt_connect=None,
                     twilio=FakeTwilio(), janitor=False)
    with TestClient(app) as c:
        assert c.post(f"/twilio/{sid}/twiml/you", data={}).status_code == 403
        path = f"/twilio/{sid}/twiml/you"
        r = c.post(path, data={"CallSid": "CA1"},
                   headers=_signed(tw_settings, path, {"CallSid": "CA1"}))
        assert r.status_code == 200 and "<Stream" in r.text


async def test_dial_guards_use_the_allowlist(tw_settings, store):
    fake = FakeTwilio()
    app = create_app(tw_settings, store=store, client=None, stt_connect=None, twilio=fake,
                     janitor=False)
    bridge = app.state.bridge
    sid = _twilio_session(store)  # callee not on the allowlist
    await bridge.janitor_once()  # dials "you" (allowlisted)
    assert fake.calls == ["+12125550111"]
    await bridge.dial_them(sid)
    assert fake.calls == ["+12125550111"]  # callee refused
    assert any(e["kind"] == "dial_refused" for e in store.events(sid))


async def test_dial_refuses_emergency_user_number(tw_settings, store):
    fake = FakeTwilio()
    tw_settings.environ = {"HERMES_GHOST_ALLOW_ALL": "1"}
    app = create_app(tw_settings, store=store, client=None, stt_connect=None, twilio=fake,
                     janitor=False)
    sid = store.create_session(goal="g", callee_label="", mode="twilio", config={},
                               provider={"user_number": "911", "callee_number": "112"})["id"]
    await app.state.bridge.janitor_once()
    assert fake.calls == [] and store.get_session(sid)["state"] == "ended"


async def test_twilio_whisper_is_an_announcement_to_the_user_leg_only(tw_settings, store):
    from hermes_ghost_call.bridge.live import GhostRuntime

    fake = FakeTwilio()
    sid = _twilio_session(store)
    store.set_provider(sid, {"you_call_sid": "CAyou", "them_call_sid": "CAthem"})
    rt = GhostRuntime(sid, store=store, settings=tw_settings, client=None, stt_connect=None,
                      twilio=fake)
    await rt.attach("you", None)
    store.add_hint(sid, 3, "say Winch")
    assert await rt.speak() == "spoken"
    assert [c for c, _ in fake.announced] == ["CAyou"]
    assert (await rt.intervene("hi")).startswith("refused")
    assert [c for c, _ in fake.announced] == ["CAyou"]
    store.set_intervene(sid, True, by="user")
    assert await rt.intervene("hi") == "spoken"
    assert fake.announced[-1][0] == "CAthem"
    token = fake.announced[-1][1].split("t=")[1]
    assert rt.take_announce_text("them", token).startswith("This is an AI assistant")
    assert rt.take_announce_text("them", token) is None  # one-time
    await rt.end("hangup")
