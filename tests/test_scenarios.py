"""Demo scenarios (scripts/dev_demo.py) and the bargaining hint shapes they rely on."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hermes_ghost_call import commands, hints, tools

ROOT = Path(__file__).resolve().parent.parent


def _demo():
    spec = importlib.util.spec_from_file_location("dev_demo", ROOT / "scripts" / "dev_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scenarios_fit_the_goal_limit():
    for name, scenario in _demo().SCENARIOS.items():
        assert len(scenario["goal"]) <= tools.MAX_GOAL_CHARS, name
        assert scenario["brief"], name


def test_isp_scenario_sets_the_callee_label(svc):
    goal = _demo().SCENARIOS["isp"]["goal"]
    reply = commands.ghost_command(f"start {goal}")
    assert "/you?t=" in reply
    s = svc.store.active_sessions()[0]
    assert s["callee_label"] == "Northwind Fiber retention"
    assert "$60/month" in s["goal"] and "FastNet" in s["goal"]


@pytest.mark.parametrize("line", [
    "ask: is $65 your best price?",
    "they want a 2-year contract → decline, you want no contract",
    "$70 is still over your target → mention the competitor's lower offer",
    "they agreed $60 → ask for a confirmation number",
    "hold: the first offer is rarely the best",
])
def test_bargaining_hints_survive_cleaning(line):
    assert hints.clean_hint(line) == line


def test_prompt_carries_bargaining_rules():
    assert "never accept the first offer" in hints.SYSTEM_PROMPT
    assert "never suggest saying them" in hints.SYSTEM_PROMPT


def test_demo_env_fills_only_known_unset_keys(tmp_path):
    path = tmp_path / "demo.env"
    path.write_text("# comment\nOPENAI_API_KEY= \"sk-test-123\" \nPUBLIC_BASE_URL=\n"
                    "GHOST_USER_NAME=Ana\nTWILIO_AUTH_TOKEN=nope\n", encoding="utf-8")
    environ = {"GHOST_USER_NAME": "Winch"}
    _demo().load_demo_env(path, environ)
    assert environ == {"OPENAI_API_KEY": "sk-test-123", "GHOST_USER_NAME": "Winch"}


def test_demo_env_missing_file_is_fine(tmp_path):
    environ: dict = {}
    _demo().load_demo_env(tmp_path / "absent.env", environ)
    assert environ == {}


def test_messages_point_at_their_latest_line():
    turns = [{"speaker": "them", "text": "$79 on a 2-year contract"},
             {"speaker": "you", "text": "no contract"},
             {"speaker": "them", "text": "I can waive the router fee"}]
    msgs = hints.build_messages(goal="lower my bill", callee_label="", turns=turns,
                                previous_hints=[])
    assert "REACT TO THEIR LATEST LINE: I can waive the router fee" in msgs[1]["content"]


def test_port_free_sees_a_listener():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert _demo().port_free("127.0.0.1", port) is False
    assert _demo().port_free("127.0.0.1", port) is True
