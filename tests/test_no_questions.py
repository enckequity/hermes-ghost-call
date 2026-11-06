"""The silent-by-default invariant: no code path creates an ask-the-user card or question."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import start

from hermes_ghost_call import commands, tools
from hermes_ghost_call.watcher import TickerWatcher

PKG = Path(__file__).resolve().parent.parent / "hermes_ghost_call"


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in PKG.rglob("*.py")}


def test_no_ask_choice_or_approval_anywhere_in_the_package():
    for name, src in _sources().items():
        code = re.sub(r'"""[\s\S]*?"""', "", src)  # docstrings may *mention* what is absent
        assert "ask_choice(" not in code, name
        assert not re.search(r"""["']action["']\s*:\s*["']approve""", code), name
        assert "register_hook(\"pre_llm_call\"" not in code, name
        assert "register_approval_transport" not in code, name
        assert "inject_message" not in code, name


def test_full_session_never_asks(svc, store, ctx):
    svc.start_watchers = False
    sid = start(svc)["session_id"]
    watcher = TickerWatcher(svc, sid)
    watcher.step()
    store.mark_live(sid)
    for i, text in enumerate(["they said Saturday only → offer Sunday", "say Winch",
                              "deposit $20 → ask if refundable"]):
        store.add_hint(sid, 10 * i, text)
        watcher.step()
    commands.ghost_command("speak")
    commands.ghost_command("status")
    tools.HANDLERS["ghost_intervene"]({"text": "hello"})
    store.end_once(sid, "hangup")
    store.set_summary_once(sid, {"agreed": "Table for 4, Sunday 19:30", "outcome": "agreed",
                                 "open_questions": [], "duration_s": 95, "hint_count": 3})
    for _ in range(3):
        watcher.step()
    assert ctx.asked == []
    texts = [m.text for m in ctx.messages] + [e for m in ctx.messages for e in m.edits]
    for text in texts:
        for line in text.splitlines():
            assert not re.match(r"^\s*(do|did|should|would|can|could) (you|i)\b", line, re.I)
            assert "approve" not in line.lower() and "yes/no" not in line.lower()


def test_ticker_is_one_message_edited_in_place(svc, store, ctx):
    sid = start(svc)["session_id"]
    watcher = TickerWatcher(svc, sid)
    store.mark_live(sid)
    watcher.step()
    for i in range(5):
        store.add_hint(sid, 5 * i, f"fact {i} → act {chr(65 + i)} {'x' * i}")
        watcher.step()
    assert len(ctx.messages) == 1
    ticker = ctx.messages[0]
    assert ticker.text.startswith("\U0001F47B ghost · Bella Napoli · live")
    assert "00:20 · fact 4" in ticker.text and len(ticker.edits) == 5


def test_exactly_one_result_message_even_with_two_watchers(svc, store, ctx):
    sid = start(svc)["session_id"]
    a, b = TickerWatcher(svc, sid), TickerWatcher(svc, sid)
    store.mark_live(sid)
    store.end_once(sid, "hangup")
    store.set_summary_once(sid, {"agreed": "Table for 4", "outcome": "agreed", "duration_s": 60,
                                 "hint_count": 0, "open_questions": []})
    assert a.step() and b.step()
    results = [m for m in ctx.messages if "Ghost call ended" in m.text]
    assert len(results) == 1 and "Agreed: Table for 4" in results[0].text


def test_watcher_builds_summary_when_bridge_never_does(svc, store, ctx, clock):
    sid = start(svc)["session_id"]
    w = TickerWatcher(svc, sid)
    store.mark_live(sid)
    store.end_once(sid, "hangup")
    assert not w.step()  # gives the bridge time first
    clock.advance(30)
    assert w.step()
    assert store.get_session(sid)["summary"] is not None
    assert sum("Ghost call ended" in m.text for m in ctx.messages) == 1


def test_expired_links_give_one_result(svc, store, ctx, clock):
    sid = start(svc)["session_id"]
    w = TickerWatcher(svc, sid)
    clock.advance(700)
    w.step()
    assert store.get_session(sid)["end_reason"] == "link_expired"
    clock.advance(30)
    w.step()
    msgs = [m.text for m in ctx.messages]
    assert len(msgs) == 1 and "Nobody joined" in msgs[0]
