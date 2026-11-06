"""Registration, tools, the Intervene gate and the /ghost command."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import BASE, FakeCtx, start

from hermes_ghost_call import commands, hooks, register, tools
from hermes_ghost_call.session import ENDED, LIVE

ROOT = Path(__file__).resolve().parent.parent


def call(name, args=None, **kw):
    return json.loads(tools.HANDLERS[name](args or {}, **kw))


def test_register_matches_manifest():
    ctx = FakeCtx()
    register(ctx)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(ctx.tools) == set(manifest["provides_tools"])
    assert [h for h, _ in ctx.hooks] == manifest["provides_hooks"] == ["pre_tool_call"]
    assert "ghost" in ctx.commands and "ghost" in ctx.cli
    assert ctx.skills and ctx.skills[0]["path"].exists()
    assert all(t["toolset"] == "ghost" for t in ctx.tools.values())
    tools._service = None


def test_no_pre_llm_call_hook_and_kwargs_hooks():
    import inspect

    ctx = FakeCtx()
    register(ctx)
    names = [h for h, _ in ctx.hooks]
    assert "pre_llm_call" not in names and "transform_llm_output" not in names
    for _, cb in ctx.hooks:  # Plugin Doctor: callbacks must accept **kwargs
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in inspect.signature(cb).parameters.values())
    tools._service = None


def test_manifest_versions_agree():
    import tomllib

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    from hermes_ghost_call import __version__

    assert manifest["version"] == project["version"] == __version__
    assert manifest["python_dependencies"] == project["dependencies"]
    assert "optional-dependencies" not in project  # no pytest/ruff in any install extra
    assert not any(d.startswith(("pytest", "ruff")) for d in project["dependencies"])


def test_ghost_start_web_links(svc):
    out = start(svc)
    assert out["ok"] and out["mode"] == "web"
    sid = out["session_id"]
    assert out["links"]["you"].startswith(f"{BASE}/g/{sid}/you?t=")
    assert out["links"]["them"].startswith(f"{BASE}/g/{sid}/them?t=")
    assert out["links"]["you"] != out["links"]["them"]
    s = svc.store.get_session(sid)
    assert s["state"] == "created" and s["config"]["user_name"] == "Winch"
    assert not s["intervene_allowed"]


def test_ghost_start_needs_goal_and_base_url(svc, env):
    assert not svc.start(goal="  ")["ok"]
    env.pop("PUBLIC_BASE_URL")
    assert "PUBLIC_BASE_URL" in svc.start(goal="book")["error"]


def test_one_active_session_and_daily_cap(svc, ctx):
    first = start(svc)
    second = start(svc)
    assert not second["ok"] and first["session_id"] in second["error"]
    svc.hangup(first["session_id"])
    ctx.config["daily_session_limit"] = 2
    assert start(svc)["ok"]
    svc.hangup(None)
    third = start(svc)
    assert not third["ok"] and "daily" in third["error"]


def test_twilio_request_falls_back_to_web_when_incomplete(svc):
    out = start(svc, mode="twilio", callee_number="+12125550123")
    assert out["ok"] and out["mode"] == "web" and "web mode" in out["note"]


def test_twilio_mode_checks_allowlist(svc, env, ctx):
    env.update({"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok",
                "TWILIO_FROM_NUMBER": "+12125550100"})
    ctx.config["user_number"] = "+12125550111"
    refused = start(svc, mode="twilio", callee_number="+12125550123")
    assert not refused["ok"] and "allowlist" in refused["error"].lower()
    env["HERMES_GHOST_TEST_ALLOWLIST"] = "+12125550111,+12125550123"
    ok = start(svc, mode="twilio", callee_number="+12125550123")
    assert ok["ok"] and ok["mode"] == "twilio" and "links" not in ok
    prov = svc.store.get_session(ok["session_id"])["provider"]
    assert prov == {"user_number": "+12125550111", "callee_number": "+12125550123"}


def test_twilio_emergency_callee_blocked_even_with_allow_all(svc, env, ctx):
    env.update({"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok",
                "TWILIO_FROM_NUMBER": "+12125550100", "HERMES_GHOST_ALLOW_ALL": "1"})
    ctx.config["user_number"] = "+12125550111"
    out = start(svc, mode="twilio", callee_number="911")
    assert not out["ok"] or out["mode"] == "web"


def test_status_and_hint_latest(svc, store):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    store.add_hint(sid, 65, "they said Saturday only → offer Sunday")
    st = call("ghost_status")
    assert st["state"] == LIVE and st["hint_count"] == 1 and st["intervene_allowed"] is False
    latest = call("ghost_hint_latest", {"n": 3})
    assert latest["hints"] == [{"id": 1, "line": "01:05 · they said Saturday only → offer Sunday"}]


def test_speak_queues_for_the_earpiece(svc, store):
    sid = start(svc)["session_id"]
    assert not call("ghost_speak")["ok"]  # not live
    store.mark_live(sid)
    assert "no hint yet" in call("ghost_speak")["error"]
    store.add_hint(sid, 3, "say Winch")
    out = call("ghost_speak", {"hint_id": "1"})
    assert out["ok"] and "earpiece" in out["where"]
    cmd = store.take_commands(sid)[0]
    assert cmd["kind"] == "speak" and cmd["payload"] == {"n": 1}
    assert not call("ghost_speak", {"hint_id": "9"})["ok"]


# ---- intervene ---------------------------------------------------------------------------
def test_intervene_blocked_without_allow(svc, store):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    verdict = hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "hi"})
    assert verdict["action"] == "block" and "/ghost intervene allow" in verdict["message"]
    out = call("ghost_intervene", {"text": "hi"})
    assert not out["ok"] and store.take_commands(sid) == []


def test_intervene_allowed_only_after_the_user_types_allow(svc, store, ctx):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    reply = commands.ghost_command(f"intervene allow {sid}", session_id="s1")
    assert "Intervene ON" in reply and "AI" in reply
    assert hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "7:30 works"}) is None
    out = call("ghost_intervene", {"text": "7:30 works"})
    assert out["ok"]
    cmd = store.take_commands(sid)[0]
    assert cmd["kind"] == "intervene" and cmd["payload"]["text"] == "7:30 works"
    commands.ghost_command(f"intervene deny {sid}")
    assert hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "x"})["action"] \
        == "block"


def test_intervene_blocked_under_cron_even_when_allowed(svc, store, ctx):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    store.set_intervene(sid, True, by="user")
    ctx.cron = {"job_id": "nightly"}
    v = hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "x"})
    assert v["action"] == "block" and "unattended" in v["message"]
    assert not call("ghost_intervene", {"text": "x"})["ok"]
    assert "cannot be allowed" in commands.ghost_command(f"intervene allow {sid}")
    ctx.cron = None
    ctx.surface = "cron"
    assert hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "x"})["action"] \
        == "block"


def test_hook_fails_closed(svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(svc, "find", boom)
    v = hooks.pre_tool_call(tool_name="ghost_intervene", args={"text": "x"})
    assert v["action"] == "block"


def test_hook_never_touches_other_tools_and_never_approves(svc):
    for name in tools.HANDLERS:
        if name == "ghost_intervene":
            continue
        assert hooks.pre_tool_call(tool_name=name, args={}) is None
    v = hooks.pre_tool_call(tool_name="ghost_intervene", args={})
    assert v["action"] != "approve"


def test_no_tool_can_turn_intervene_on(svc, store):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    for name in tools.HANDLERS:
        for args in ({}, {"session_id": sid, "intervene": True, "allow": True,
                          "intervene_allowed": True, "text": "x", "goal": "x"}):
            tools.HANDLERS[name](args)
    assert not store.get_session(sid)["intervene_allowed"]
    for schema in tools.SCHEMAS.values():
        assert "allow" not in json.dumps(schema["parameters"]).lower()


# ---- hangup / summary / commands ------------------------------------------------------------
def test_hangup_without_bridge_ends_within_budget(svc, store):
    import time

    sid = start(svc)["session_id"]
    store.mark_live(sid)
    t0 = time.monotonic()
    out = call("ghost_hangup")
    assert out["ok"] and out["state"] == ENDED and time.monotonic() - t0 < 1.6
    assert store.get_session(sid)["state"] == ENDED


def test_summary_tool_force_builds_once(svc, store):
    sid = start(svc)["session_id"]
    store.mark_live(sid)
    store.add_turn(sid, "them", "Friday 7:30 works", 4)
    store.end_once(sid, "hangup")
    out = call("ghost_summary")
    assert out["ok"] and out["summary"] is not None and "Ghost call ended" in out["text"]
    again = call("ghost_summary")
    assert again["summary"] == out["summary"]


def test_slash_start_prints_links(svc):
    reply = commands.ghost_command("start book a table Friday 8pm for 4 with Bella Napoli")
    assert "/you?t=" in reply and "/them?t=" in reply and "never joins" in reply
    s = svc.store.active_sessions()[0]
    assert s["callee_label"] == "Bella Napoli"


def test_slash_usage_and_subcommands(svc, store):
    assert commands.ghost_command("").startswith("Usage")
    assert commands.ghost_command("nope").startswith("Usage")
    sid = start(svc)["session_id"]
    assert sid in commands.ghost_command("status")
    store.mark_live(sid)
    store.add_turn(sid, "them", "We only have Saturday", 2)
    assert "them: We only have Saturday" in commands.ghost_command("transcript")
    assert sid in commands.ghost_command("log 3")
    assert "ended" in commands.ghost_command("hangup")


def test_check_names_only_never_values(svc, env):
    env["TWILIO_AUTH_TOKEN"] = "twilio-secret-value-xyz"
    import hermes_ghost_call.cli as cli

    rows = cli.run_checks(env, svc.config(), http_get=lambda url, t: 200)
    text = commands.ghost_command("check")
    for value in (env["OPENAI_API_KEY"], env["TWILIO_AUTH_TOKEN"]):
        assert value not in text and all(value not in d for _, _, d in rows)
    assert "OPENAI_API_KEY" in text
