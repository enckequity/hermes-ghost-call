"""GhostRuntime: what reaches whom. Hints/whispers to 'you' only; 'them' hears only the user's mic
and, with a sticky allow, Intervene (disclosure first)."""
from __future__ import annotations

import asyncio
import base64

import pytest
from conftest import FakeLLM, FakeStt

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


def make_session(store, **cfg):
    config = {"hint_mode": "auto", "min_hint_interval_s": 4, "max_hints_per_session": 40,
              "store_transcripts": True, "hint_timeout_ms": 500, "user_name": "Winch",
              "max_duration_s": 900, "model": "m", "stt_model": "gpt-4o-mini-transcribe",
              **cfg}
    return store.create_session(goal="book Friday 8pm for 4, $80, name Winch",
                                callee_label="Bella Napoli", mode="web", config=config)


@pytest.fixture
def settings():
    return BridgeSettings(public_base_url="https://ghost.example.test", openai_api_key="k",
                          tick_s=0.01)


async def joined(store, settings, llm=None, stt=None, **cfg):
    s = make_session(store, **cfg)
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=llm or FakeLLM(),
                      stt_connect=stt or FakeStt())
    you, them = Sink(), Sink()
    await rt.attach("you", you)
    await rt.attach("them", them)
    return rt, you, them


async def test_audio_is_relayed_both_ways_and_fed_to_stt(store, settings):
    stt = FakeStt()
    rt, you, them = await joined(store, settings, stt=stt)
    frame = bytes(range(160))
    await rt.on_audio("you", frame)
    await rt.on_audio("them", frame[::-1])
    assert base64.b64decode(them.msgs[-1]["payload"]) == frame
    assert base64.b64decode(you.msgs[-1]["payload"]) == frame[::-1]
    assert set(stt.conns) == {"you", "them"}
    assert stt.conns["you"].sent[0]["session"]["type"] == "transcription"
    await rt.end("hangup")


async def test_hints_and_captions_go_to_you_only(store, settings):
    llm = FakeLLM(["they said Saturday only → offer Sunday"])
    rt, you, them = await joined(store, settings, llm=llm)
    await rt._on_final("them", "We only have Saturday, sorry.")
    hint = next(m for m in you.msgs if m["event"] == "hint")
    assert hint["line"].endswith("· they said Saturday only → offer Sunday")
    assert "hint" not in them.kinds() and "caption" not in them.kinds()
    assert {"peer"} >= set(them.kinds())
    await rt.end("hangup")


async def test_speak_whispers_to_you_only(store, settings):
    llm = FakeLLM(["say Winch"])
    rt, you, them = await joined(store, settings, llm=llm)
    await rt._on_final("them", "What name is the booking under?")
    before = list(them.msgs)
    assert await rt.speak() == "spoken"
    whisper = next(m for m in you.msgs if m["event"] == "whisper")
    assert whisper["text"] == "say Winch" and whisper["pcm24"]
    assert them.msgs == before  # nothing at all reached the other party
    assert llm.tts_calls == ["say Winch"]
    assert store.get_hint(rt.sid, 1)["spoken_at"] is not None
    await rt.end("hangup")


async def test_speak_falls_back_to_local_speech_when_tts_fails(store, settings):
    from hermes_ghost_call.llm import LLMError

    llm = FakeLLM(["say Winch"])
    llm.tts_error = LLMError("timeout")
    rt, you, them = await joined(store, settings, llm=llm)
    await rt._on_final("them", "What name is the booking under?")
    await rt.speak("1")
    whisper = next(m for m in you.msgs if m["event"] == "whisper")
    assert "pcm24" not in whisper and whisper["fallback"] == "timeout"
    await rt.end("hangup")


async def test_speak_without_hint(store, settings):
    rt, you, them = await joined(store, settings)
    assert await rt.speak() == "no_hint"
    await rt.end("hangup")


async def test_intervene_refused_without_sticky_allow(store, settings):
    llm = FakeLLM()
    rt, you, them = await joined(store, settings, llm=llm)
    out = await rt.intervene("Could we do 7:30 instead?")
    assert out.startswith("refused") and "/ghost intervene allow" in out
    assert "media" not in them.kinds() and llm.tts_calls == [] and rt.far_leg_log == []
    await rt.end("hangup")


async def test_intervene_refused_when_unattended_even_if_allowed(store, settings):
    rt, you, them = await joined(store, settings)
    store.set_intervene(rt.sid, True, by="user")
    assert (await rt.intervene("hello", unattended=True)).startswith("refused")
    assert rt.far_leg_log == []
    await rt.end("hangup")


async def test_intervene_with_allow_discloses_first_then_not_again(store, settings):
    llm = FakeLLM()
    rt, you, them = await joined(store, settings, llm=llm)
    store.set_intervene(rt.sid, True, by="user")
    assert await rt.intervene("Could we do 7:30 instead?") == "spoken"
    assert await rt.intervene("Thank you!") == "spoken"
    assert rt.far_leg_log[0] == ("This is an AI assistant speaking on behalf of Winch. "
                                 "Could we do 7:30 instead?")
    assert rt.far_leg_log[1] == "Thank you!"
    assert llm.tts_calls == rt.far_leg_log
    assert "media" in them.kinds()
    await rt.end("hangup")


async def test_intervene_command_queue_respects_db_flag(store, settings):
    rt, you, them = await joined(store, settings)
    cid = store.enqueue(rt.sid, "intervene", {"text": "hi"})
    for cmd in store.take_commands(rt.sid):
        await rt._run_command(cmd)
    assert store.get_command(cid)["result"].startswith("refused")
    await rt.end("hangup")


async def test_hangup_request_ends_quickly_and_writes_one_summary(store, settings):
    summary_json = ('{"outcome":"agreed","agreed":"Table for 4 Sunday 19:30",'
                    '"date":"Sunday 19:30","amount":"$80","open_questions":["deposit?"],'
                    '"follow_up_sms":"Hi, confirming"}')
    llm = FakeLLM(["they said Saturday only → offer Sunday", summary_json])
    rt, you, them = await joined(store, settings, llm=llm)
    await rt._on_final("them", "We only have Saturday or Sunday.")
    store.request_hangup(rt.sid)
    await asyncio.wait_for(rt.ended.wait(), 1.0)
    await rt.summary_task
    s = store.get_session(rt.sid)
    assert s["state"] == ENDED and s["end_reason"] == "hangup"
    assert s["summary"]["agreed"] == "Table for 4 Sunday 19:30"
    assert s["summary"]["hint_count"] == 1 and s["summary"]["source"] == "model"
    assert "ended" in you.kinds() and "ended" in them.kinds()
    assert not store.set_summary_once(rt.sid, {"agreed": "other"})


async def test_page_stop_ends_session(store, settings):
    rt, you, them = await joined(store, settings)
    await rt.on_client_event("them", {"event": "stop"})
    assert store.get_session(rt.sid)["end_reason"] == "them_hung_up"
    await rt.summary_task


async def test_duration_cap(store, settings, clock):
    rt, you, them = await joined(store, settings, max_duration_s=60)
    clock.advance(61)
    await asyncio.wait_for(rt.ended.wait(), 1.0)
    assert store.get_session(rt.sid)["end_reason"] == "duration_cap"
    await rt.summary_task


async def test_hint_button_is_an_explicit_ask(store, settings):
    llm = FakeLLM(["confirm 4 people at 19:30"])
    rt, you, them = await joined(store, settings, llm=llm, hint_mode="tap")
    await rt._on_final("them", "We can do 7:30 on Friday.")
    assert llm.calls == []
    await rt.on_client_event("you", {"event": "hint"})
    assert any(m["event"] == "hint" for m in you.msgs)
    await rt.on_client_event("them", {"event": "hint"})  # the other page cannot ask
    assert len(llm.calls) == 1
    await rt.end("hangup")


async def test_no_key_means_relay_only_with_a_notice(store, settings):
    s = make_session(store)
    rt = GhostRuntime(s["id"], store=store, settings=settings, client=None, stt_connect=None)
    you, them = Sink(), Sink()
    await rt.attach("you", you)
    await rt.attach("them", them)
    assert any(m["event"] == "notice" for m in you.msgs)
    await rt.on_audio("you", b"\x7f" * 160)
    assert them.kinds()[-1] == "media"
    await rt.end("hangup")
    await rt.summary_task
    assert store.get_session(rt.sid)["summary"]["source"] == "fallback"
