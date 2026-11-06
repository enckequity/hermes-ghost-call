"""Secrets never leak (planted marker keys), secret_scope routing, and the summary."""
from __future__ import annotations

import json
import logging
import re
import sys
import types
from pathlib import Path

import pytest
from conftest import MARKER_KEY, MARKER_TOKEN, FakeLLM, start

from hermes_ghost_call import secrets as secretsmod
from hermes_ghost_call import summary as summarymod
from hermes_ghost_call import tools
from hermes_ghost_call.bridge.settings import BridgeSettings
from hermes_ghost_call.bridge.twilio_io import TwilioRest
from hermes_ghost_call.llm import LLMError, OpenAIClient

PKG = Path(__file__).resolve().parent.parent / "hermes_ghost_call"


def test_secret_scope_is_preferred(monkeypatch):
    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = lambda name, default=None: "scoped-" + name
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "from-environ")
    assert secretsmod.get("OPENAI_API_KEY") == "scoped-OPENAI_API_KEY"


def test_scope_error_fails_closed(monkeypatch):
    mod = types.ModuleType("agent.secret_scope")

    def boom(name, default=None):
        raise RuntimeError("UnscopedSecretError")

    mod.get_secret = boom
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "from-environ")
    assert secretsmod.get("OPENAI_API_KEY") == ""  # never falls back to os.environ


def test_no_bare_environ_reads_of_secret_names():
    pattern = re.compile(r"os\.environ(\.get)?\s*[\[(]\s*[\"'](" +
                         "|".join(secretsmod.SECRET_NAMES) + r")[\"']")
    for path in PKG.rglob("*.py"):
        if path.name == "secrets.py":
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_no_dotenv_reading():
    for path in PKG.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "dotenv" not in src and "open('.env'" not in src and 'open(".env"' not in src


def test_reprs_hide_credentials():
    s = BridgeSettings(openai_api_key=MARKER_KEY, twilio_auth_token=MARKER_TOKEN)
    assert MARKER_KEY not in repr(s) and MARKER_TOKEN not in repr(s)
    assert MARKER_KEY not in repr(OpenAIClient(MARKER_KEY))
    assert MARKER_TOKEN not in repr(TwilioRest("AC1", MARKER_TOKEN))


def test_scrub_replaces_values():
    env = {"OPENAI_API_KEY": MARKER_KEY, "TWILIO_AUTH_TOKEN": MARKER_TOKEN}
    text = secretsmod.scrub(f"failed with Bearer {MARKER_KEY} and {MARKER_TOKEN}", env)
    assert MARKER_KEY not in text and MARKER_TOKEN not in text and "<OPENAI_API_KEY>" in text


def test_tool_errors_never_carry_secrets(svc, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(**kw):
        raise RuntimeError(f"auth failed for {MARKER_KEY}")

    monkeypatch.setattr(svc, "start", boom)
    out = tools.HANDLERS["ghost_start"]({"goal": "x"})
    assert MARKER_KEY not in out and json.loads(out)["ok"] is False
    for rec in caplog.records:
        assert MARKER_KEY not in rec.getMessage()


def test_tool_outputs_never_carry_secrets(svc, store):
    outs = [tools.HANDLERS["ghost_start"]({"goal": "book a table"})]
    sid = store.active_sessions()[0]["id"]
    store.mark_live(sid)
    for name in tools.HANDLERS:
        outs.append(tools.HANDLERS[name]({"session_id": sid, "text": "x"}))
    for out in outs:
        assert MARKER_KEY not in out


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"\x00\x00" * 10

    def json(self):
        return self._payload


class _Http:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.seen = resp, exc, []

    async def post(self, url, headers=None, content=None, timeout=None):
        self.seen.append((url, headers, content))
        if self.exc:
            raise self.exc
        return self.resp


async def test_openai_errors_are_codes_without_the_key(caplog):
    caplog.set_level(logging.DEBUG)
    client = OpenAIClient(MARKER_KEY, http=_Http(exc=OSError(f"boom {MARKER_KEY}")))
    with pytest.raises(LLMError) as err:
        await client.chat([{"role": "user", "content": "x"}])
    assert err.value.code == "network" and MARKER_KEY not in str(err.value)
    assert all(MARKER_KEY not in r.getMessage() for r in caplog.records)
    client = OpenAIClient(MARKER_KEY, http=_Http(_Resp(401)))
    with pytest.raises(LLMError) as err:
        await client.chat([{"role": "user", "content": "x"}])
    assert err.value.code == "http_401"


async def test_chat_request_shape():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "say Winch"}}]}))
    client = OpenAIClient("k", http=http)
    assert await client.chat([{"role": "user", "content": "x"}], model="gpt-4.1-mini") == \
        "say Winch"
    body = json.loads(http.seen[0][2])
    assert body["model"] == "gpt-4.1-mini" and body["temperature"] == 0.2
    assert body["max_completion_tokens"] == 60


async def test_reasoning_models_get_no_temperature():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "x"}}]}))
    await OpenAIClient("k", http=http).chat([], model="gpt-5-mini")
    assert "temperature" not in json.loads(http.seen[0][2])


# ---- summary -------------------------------------------------------------------------------
async def test_summary_from_model_is_cleaned():
    llm = FakeLLM([json.dumps({"outcome": "agreed", "agreed": "Table for 4, Fri 19:30",
                               "date": "Fri 19:30", "amount": "$80",
                               "open_questions": ["deposit?", "parking", "a", "b"],
                               "follow_up_sms": "Hi! Confirming 4 at 19:30 Friday. Winch",
                               "extra": "dropped"})])
    out = await summarymod.build(llm, goal="g", callee_label="Bella", user_name="Winch",
                                 turns=[{"speaker": "them", "text": "ok 19:30", "t": 3}],
                                 hint_lines=["h1"], model="m", stats={"duration_s": 95})
    assert out["source"] == "model" and out["outcome"] == "agreed"
    assert len(out["open_questions"]) == 3 and "extra" not in out and out["duration_s"] == 95


async def test_summary_fallbacks():
    llm = FakeLLM(["not json"])
    out = await summarymod.build(llm, goal="g", callee_label="", user_name="", turns=[
        {"speaker": "them", "text": "x", "t": 0}], hint_lines=[], model="m", stats={})
    assert out["source"] == "fallback" and "bad_json" in out["note"]
    empty = await summarymod.build(llm, goal="g", callee_label="", user_name="", turns=[],
                                   hint_lines=[], model="m", stats={})
    assert empty["note"] == "nothing was transcribed"


def test_render_result_message():
    session = {"callee_label": "Bella Napoli"}
    text = summarymod.render(session, {
        "agreed": "Table for 4, Fri 19:30", "date": "Fri 19:30", "amount": "$80",
        "open_questions": ["deposit refundable?"], "follow_up_sms": "Hi, confirming",
        "source": "model", "duration_s": 192, "hint_count": 7})
    lines = text.splitlines()
    assert lines[0] == "\U0001F47B Ghost call ended · Bella Napoli · 03:12 · 7 hints"
    assert "Agreed: Table for 4, Fri 19:30" in lines
    assert "Date: Fri 19:30 · Amount: $80" in lines
    assert 'Draft SMS: "Hi, confirming"' in lines


def test_summary_tool_needs_ended_call(svc):
    sid = start(svc)["session_id"]
    out = svc.summary(sid)
    assert not out["ok"] and "still" in out["error"]
