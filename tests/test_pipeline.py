"""HintEngine: gate before model, timeout skip, rate limit with coalescing, caps, tap mode."""
from __future__ import annotations

import asyncio

import pytest

from hermes_ghost_call.llm import LLMError
from hermes_ghost_call.pipeline import HintEngine


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class ScriptedModel:
    def __init__(self, replies, delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.calls = []

    async def __call__(self, messages, cfg):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else "SILENT"
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(store, model, *, cfg=None, mono=None, sleeps=None):
    config = {"hint_mode": "auto", "min_hint_interval_s": 4, "max_hints_per_session": 40,
              "store_transcripts": True, "hint_timeout_ms": 200, **(cfg or {})}
    session = store.create_session(goal="book Friday 8pm for 4, $80", callee_label="Bella",
                                   mode="web", config=config)
    store.mark_live(session["id"])
    session = store.get_session(session["id"])
    pushed = []
    recorded_sleeps = sleeps if sleeps is not None else []

    async def fake_sleep(d):
        recorded_sleeps.append(d)

    eng = HintEngine(store=store, session=session, client=None, on_hint=pushed.append,
                     hint_fn=model, monotonic=mono or Mono(), sleep=fake_sleep)
    return eng, pushed


async def test_fillers_never_reach_the_model(store):
    model = ScriptedModel(["should not be used"])
    eng, pushed = make(store, model)
    for text in ("uh-huh", "okay", "one second", "Hello, Bella Napoli, how can I help you?"):
        await eng.on_utterance("them", text)
    await eng.on_utterance("you", "Hi, I'd like a table for four on Friday at eight")
    assert model.calls == [] and pushed == []
    assert all(d.startswith("silent:") for d in eng.decisions)


async def test_meaningful_them_utterance_yields_one_hint(store):
    model = ScriptedModel(["they said Saturday only → offer Friday 19:30 or Sunday"])
    eng, pushed = make(store, model)
    assert await eng.on_utterance("them", "We only have Saturday, sorry.") == "hint"
    assert len(model.calls) == 1
    assert pushed[0]["text"] == "they said Saturday only → offer Friday 19:30 or Sunday"
    assert store.hint_count(eng.sid) == 1 and pushed[0]["n"] == 1


async def test_model_silent_is_not_a_hint_and_spends_no_rate(store):
    model = ScriptedModel(["SILENT", "say Winch"])
    eng, pushed = make(store, model)
    assert await eng.on_utterance("them", "Let me look at the book for Friday") == "silent:model"
    assert await eng.on_utterance("them", "What name is it under?") == "hint"
    assert [p["text"] for p in pushed] == ["say Winch"]


async def test_timeout_skips_rather_than_stalls(store):
    model = ScriptedModel(["late hint"], delay=0.5)
    eng, pushed = make(store, model, cfg={"hint_timeout_ms": 50})
    assert await eng.on_utterance("them", "The set menu is 95 dollars.") == "timeout"
    assert pushed == [] and store.hint_count(eng.sid) == 0


async def test_model_error_is_swallowed(store):
    model = ScriptedModel([LLMError("http_500")])
    eng, pushed = make(store, model)
    assert await eng.on_utterance("them", "We need a deposit of 20 dollars.") == "error:http_500"
    assert pushed == []


async def test_rate_limit_coalesces_instead_of_stacking(store):
    mono = Mono()
    model = ScriptedModel(["they said Saturday only → offer Sunday",
                           "deposit $20 → ask if refundable"])
    sleeps = []
    eng, pushed = make(store, model, mono=mono, sleeps=sleeps)
    assert await eng.on_utterance("them", "We only have Saturday.") == "hint"
    mono.t += 1.0
    assert await eng.on_utterance("them", "And there is a deposit of 20 dollars.") == "coalesced"
    mono.t += 0.5
    assert await eng.on_utterance("them", "It is per table, 20 dollars total.") == "coalesced"
    assert len(model.calls) == 1  # nothing stacked inside the window
    # The single deferred attempt is scheduled for when the window opens.
    await asyncio.sleep(0)
    assert sleeps and sleeps[0] == pytest.approx(3.0, abs=0.05)
    mono.t += 3.0
    await eng._deferred  # runs the one coalesced attempt on the latest context
    assert len(model.calls) == 2 and len(pushed) == 2
    assert "20 dollars total" in model.calls[1][1]["content"]  # latest context was used


async def test_interval_never_below_four_seconds(store):
    mono = Mono()
    model = ScriptedModel(["one", "two"])
    eng, pushed = make(store, model, cfg={"min_hint_interval_s": 0.5}, mono=mono)
    await eng.on_utterance("them", "We only have Saturday.")
    mono.t += 2.0
    assert await eng.on_utterance("them", "Price is 95 per person.") == "coalesced"
    await eng.close()


async def test_hard_cap_per_session(store):
    mono = Mono()
    model = ScriptedModel(["Saturday only → offer Sunday", "95 is over budget → ask set menu",
                           "deposit 20 → ask refundable", "parking costs extra",
                           "kids menu exists"])
    eng, pushed = make(store, model, cfg={"max_hints_per_session": 3}, mono=mono)
    texts = ["We have Saturday at 7", "It costs 95 dollars", "Deposit is 20 dollars",
             "Parking is 10 dollars", "Kids menu is 12 dollars"]
    for t in texts:
        await eng.on_utterance("them", t)
        mono.t += 5
    assert len(pushed) == 3 and eng.decisions[-1] == "cap"


async def test_tap_mode_hints_only_on_request(store):
    model = ScriptedModel(["confirm 4 people at 19:30"])
    eng, pushed = make(store, model, cfg={"hint_mode": "tap"})
    assert await eng.on_utterance("them", "We can do 7:30 on Friday.") == "tap_mode"
    assert model.calls == []
    assert await eng.request_hint() == "hint"
    assert "The user tapped Hint" in model.calls[0][1]["content"]


async def test_transcripts_stay_in_memory_when_storage_is_off(store):
    model = ScriptedModel(["x → y"])
    eng, _ = make(store, model, cfg={"store_transcripts": False})
    await eng.on_utterance("them", "We only have Saturday.")
    assert store.turns(eng.sid) == [] and len(eng.turns) == 1


async def test_close_cancels_deferred(store):
    mono = Mono()
    model = ScriptedModel(["a → b", "c → d"])
    calls = []

    async def slow_sleep(d):
        calls.append(d)
        await asyncio.sleep(10)

    session = store.create_session(goal="g", callee_label="", mode="web",
                                   config={"hint_timeout_ms": 200})
    store.mark_live(session["id"])
    eng = HintEngine(store=store, session=store.get_session(session["id"]), client=None,
                     hint_fn=model, monotonic=mono, sleep=slow_sleep)
    await eng.on_utterance("them", "We only have Saturday.")
    await eng.on_utterance("them", "The price is 95 dollars.")
    await asyncio.sleep(0)
    await eng.close()
    assert len(model.calls) == 1
