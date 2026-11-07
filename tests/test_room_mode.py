"""Room (speakerphone) mode: one page and one mic hear both sides of a call on a phone speaker.

Nothing is relayed, there is no Them link, speakers are unlabelled ("call"), Intervene is
impossible, and hints, whispers and the summary still work."""
from __future__ import annotations

import json

import pytest
from conftest import FakeLLM, FakeStt
from fastapi.testclient import TestClient

from hermes_ghost_call import commands, hints, mode, safety
from hermes_ghost_call.bridge.app import create_app
from hermes_ghost_call.bridge.live import GhostRuntime
from hermes_ghost_call.bridge.settings import BridgeSettings
from hermes_ghost_call.session import ENDED


class Sink:
    def __init__(self) -> None:
        self.msgs: list[dict] = []

    async def __call__(self, msg: dict) -> None:
        self.msgs.append(msg)

    def kinds(self) -> list[str]:
        return [m["event"] for m in self.msgs]


CFG = {"hint_mode": "auto", "min_hint_interval_s": 4, "max_hints_per_session": 40,
       "store_transcripts": True, "hint_timeout_ms": 500, "user_name": "Winch",
       "max_duration_s": 900, "model": "m", "stt_model": "gpt-4o-mini-transcribe"}


def room_session(store):
    return store.create_session(goal="lower my internet bill, target $60/month or less",
                                callee_label="Northwind Fiber", mode="room", config=CFG)


# ---- mode + start ------------------------------------------------------------------------------
def test_room_is_explicit_only():
    env = {"OPENAI_API_KEY": "k", "PUBLIC_BASE_URL": "http://localhost:8766"}
    assert mode.resolve(env, "room").name == "room"
    assert mode.resolve(env, "").name == "web"  # never chosen automatically


def test_slash_start_room_gives_one_listening_link(svc):
    reply = commands.ghost_command("start room lower my internet bill with Northwind Fiber")
    assert "Listening page" in reply and "/you?t=" in reply
    assert "/them?t=" not in reply
    s = svc.store.active_sessions()[0]
    assert s["mode"] == "room" and s["callee_label"] == "Northwind Fiber"
    assert s["goal"].startswith("lower my internet bill")


def test_slash_start_with_a_number_asks_for_twilio(svc):
    reply = commands.ghost_command("start +12125550123 book a table with Bella Napoli")
    s = svc.store.active_sessions()[0]
    # Twilio is not configured in the test env, so it falls back to web and says why
    assert s["mode"] == "web" and "Twilio is not fully configured" in reply
    assert not s["goal"].startswith("+1")


def test_intervene_is_refused_in_room_mode(store):
    s = room_session(store)
    store.mark_live(s["id"])
    store.set_intervene(s["id"], True)
    reason = safety.intervene_refusal(store.get_session(s["id"]), unattended=False)
    assert "speakerphone" in reason


# ---- runtime -----------------------------------------------------------------------------------
@pytest.fixture
def settings():
    return BridgeSettings(public_base_url="https://ghost.example.test", openai_api_key="k",
                          tick_s=0.01)


async def test_room_runtime_relays_nothing_and_labels_speech_call(store, settings):
    llm = FakeLLM(["they said $70 → counter with the competitor's price"])
    stt = FakeStt()
    s = room_session(store)
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=llm, stt_connect=stt)
    you = Sink()
    await rt.attach("you", you)
    assert store.get_session(s["id"])["state"] == "live"
    await rt.on_audio("you", bytes(160))
    assert "media" not in you.kinds()  # no echo of the mic back to the page
    await rt._on_final("you", "I can offer seventy dollars a month for six months.")
    caption = next(m for m in you.msgs if m["event"] == "caption")
    assert caption["role"] == "call"
    assert rt.engine.turns[-1]["speaker"] == "call"
    assert "hint" in you.kinds()
    prompt = llm.calls[-1][1]["content"]
    assert "call: I can offer seventy dollars" in prompt
    assert "LATEST LINE ON THE CALL (speaker unknown)" in prompt
    out = await rt.intervene("hello")
    assert out.startswith("refused") and rt.far_leg_log == []
    await rt.speak()
    assert "whisper" in you.kinds()
    await rt.end("hangup")
    assert store.get_session(s["id"])["state"] == ENDED


def test_gate_and_private_amounts_treat_call_like_them():
    assert hints.gate("call", "I can offer $70 a month for six months").ok
    turns = [{"speaker": "call", "text": "I can offer $60 for 12 months"}]
    assert not hints.leaks_private("they offered $60 → ask about fees",
                                   "target $60/month or less", turns)


# ---- HTTP --------------------------------------------------------------------------------------
def test_room_pages_and_socket(store):
    app = create_app(BridgeSettings(public_base_url="http://testserver", tick_s=0.02),
                     store=store, client=None, stt_connect=None, twilio=None, janitor=False)
    s = room_session(store)
    sid = s["id"]
    you_tok = store.mint_token(sid, "you", "link", 600)
    them_tok = store.mint_token(sid, "them", "link", 600)
    with TestClient(app) as c:
        assert c.get(f"/g/{sid}/them?t={them_tok}").status_code == 404  # no Them page
        page = c.get(f"/g/{sid}/you?t={you_tok}")
        assert page.status_code == 200
        assert 'data-room="1"' in page.text and "Start listening" in page.text
        assert "on speaker" in page.text
        media = c.post(f"/g/{sid}/join/you", json={"t": you_tok}).json()["media_token"]
        with c.websocket_connect(f"/g/{sid}/ws/you") as ws:
            ws.send_text(json.dumps({"event": "start", "token": media}))
            ws.send_text(json.dumps({"event": "stop"}))
            kinds = []
            for _ in range(5):
                try:
                    kinds.append(json.loads(ws.receive_text())["event"])
                except Exception:  # noqa: BLE001  closed after the end
                    break
        assert store.get_session(sid)["state"] == ENDED
        assert "media" not in kinds


def test_far_field_session_update_and_prompt():
    from hermes_ghost_call.bridge import stt

    near = stt.session_update("gpt-4o-mini-transcribe")["session"]["audio"]["input"]
    far = stt.session_update("gpt-4o-mini-transcribe", far_field=True,
                             prompt="Northwind,  $60/month")
    far = far["session"]["audio"]["input"]
    assert near["noise_reduction"] == {"type": "near_field"}
    assert "prompt" not in near["transcription"]
    assert far["noise_reduction"] == {"type": "far_field"}
    assert far["turn_detection"]["threshold"] == stt.FAR_FIELD_VAD < 0.5
    assert far["transcription"]["prompt"] == "Northwind, $60/month"


def test_agc_lifts_a_quiet_voice_but_not_hiss():
    import array

    from hermes_ghost_call.bridge import stt

    def chunk(level):
        return array.array("h", [level if i % 2 else -level for i in range(2400)]).tobytes()

    def rms(pcm):
        a = array.array("h")
        a.frombytes(pcm)
        return (sum(v * v for v in a) / len(a)) ** 0.5

    agc = stt.Agc()
    assert rms(agc(chunk(40))) == 40  # below the floor: untouched
    out = [rms(agc(chunk(400))) for _ in range(6)]
    assert out[-1] > 2000  # a quiet phone voice is lifted toward the target
    assert rms(agc(chunk(9000))) <= 9000  # a loud voice is never boosted


def test_transcriber_vocabulary_is_names_and_numbers_only():
    from hermes_ghost_call.bridge import stt

    goal = ("get a lower price on my home internet. I pay $89/month for 500 Mbps. FastNet offers "
            "1 Gbps at $55/month. Target $60/month or less. Name Winch")
    vocab = stt.vocabulary("Northwind Fiber", goal)
    for word in ("Northwind", "FastNet", "$89/month", "$60/month", "500 Mbps", "Winch"):
        assert word in vocab
    for sentence_word in ("lower", "price", "Target", "less", "Name"):
        assert sentence_word not in vocab.split(", ")


def test_prompt_echo_on_noise_is_dropped():
    from hermes_ghost_call.bridge import stt

    vocab = "Northwind, Fiber, $89/month, FastNet, $55/month, $60/month, Winch"
    assert stt.echoes_prompt("Northwind Fiber, FastNet, $55/month, $60/month", vocab)
    assert not stt.echoes_prompt("I can offer $60 a month for twelve months.", vocab)
    assert not stt.echoes_prompt("Thank you for calling Northwind Fiber.", vocab)
    assert not stt.echoes_prompt("FastNet", vocab)  # too short to judge
