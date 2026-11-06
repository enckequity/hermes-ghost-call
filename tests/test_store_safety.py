"""SQLite invariants (write-once summary, one-time tokens) and the hard safety rules."""
from __future__ import annotations

import pytest

from hermes_ghost_call import safety
from hermes_ghost_call.session import ENDED, LIVE


def _session(store, **kw):
    return store.create_session(goal="g", callee_label="", mode="web", config={}, **kw)


def test_summary_is_write_once(store):
    s = _session(store)
    assert store.set_summary_once(s["id"], {"agreed": "first"})
    assert not store.set_summary_once(s["id"], {"agreed": "second"})
    assert store.get_session(s["id"])["summary"]["agreed"] == "first"


def test_summary_post_is_claimed_once(store):
    s = _session(store)
    assert not store.claim_summary_post(s["id"])  # nothing to post yet
    store.set_summary_once(s["id"], {"agreed": "x"})
    assert store.claim_summary_post(s["id"])
    assert not store.claim_summary_post(s["id"])


def test_end_once(store):
    s = _session(store)
    assert store.end_once(s["id"], "hangup")
    assert not store.end_once(s["id"], "other")
    cur = store.get_session(s["id"])
    assert cur["state"] == ENDED and cur["end_reason"] == "hangup"


def test_tokens_are_one_time_hashed_and_expire(store, clock):
    s = _session(store)
    tok = store.mint_token(s["id"], "you", "link", 60)
    assert store.peek_token(s["id"], "you", "link", tok)
    assert not store.peek_token(s["id"], "them", "link", tok)  # role-bound
    assert store.consume_token(s["id"], "you", "link", tok)
    assert not store.consume_token(s["id"], "you", "link", tok)
    late = store.mint_token(s["id"], "you", "link", 60)
    clock.advance(61)
    assert not store.consume_token(s["id"], "you", "link", late)
    raw = store._all("SELECT hash FROM tokens")
    assert all(tok not in r["hash"] and late not in r["hash"] for r in raw)


def test_commands_are_taken_once(store):
    s = _session(store)
    store.enqueue(s["id"], "speak", {"n": 1})
    assert len(store.take_commands(s["id"])) == 1
    assert store.take_commands(s["id"]) == []
    with pytest.raises(ValueError):
        store.enqueue(s["id"], "approve_hint")


def test_hint_numbers_are_per_session(store):
    a, b = _session(store), _session(store)
    assert store.add_hint(a["id"], 1, "x")["n"] == 1
    assert store.add_hint(a["id"], 2, "y")["n"] == 2
    assert store.add_hint(b["id"], 1, "z")["n"] == 1


def test_disclosure_flag_flips_once(store):
    s = _session(store)
    assert store.mark_disclosed(s["id"]) and not store.mark_disclosed(s["id"])


def test_mark_live_only_from_created(store):
    s = _session(store)
    assert store.mark_live(s["id"]) and not store.mark_live(s["id"])
    assert store.get_session(s["id"])["state"] == LIVE


# ---- numbers --------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["911", "112", "999", "000", "110", "119", "100", "101", "102",
                                 "103", "+1 911", "tel:112", "988"])
def test_emergency_numbers_blocked(raw):
    e164, why = safety.validate_number(raw)
    assert e164 is None and why in ("emergency_number", "short_code")


@pytest.mark.parametrize("raw", ["+19005551234", "+1 976 555 1234", "+449091234567"])
def test_premium_numbers_blocked(raw):
    assert safety.validate_number(raw) == (None, "premium_number")


@pytest.mark.parametrize("raw", ["12345", "55555", "+1234", "2125550123", "", None, "abc"])
def test_short_codes_and_non_e164_blocked(raw):
    assert safety.validate_number(raw)[0] is None


def test_valid_e164_passes():
    assert safety.validate_number("+1 (212) 555-0123") == ("+12125550123", "")


def test_allowlist_fails_closed():
    assert not safety.check_allowlist("+12125550123", {}).ok
    assert not safety.check_allowlist("+12125550123", {safety.ALLOWLIST_ENV: ""}).ok
    assert not safety.check_allowlist("+12125550123",
                                      {safety.ALLOWLIST_ENV: "+12125550188"}).ok
    assert safety.check_allowlist("+12125550123", {safety.ALLOWLIST_ENV: "+12125550123"}).ok
    assert safety.check_allowlist("+12125550123", {safety.ALLOW_ALL_ENV: "1"}).ok
    assert not safety.check_allowlist("+12125550123", {safety.ALLOW_ALL_ENV: "true"}).ok


def test_allow_all_never_unblocks_emergency_numbers():
    assert not safety.check_dialable("911", {safety.ALLOW_ALL_ENV: "1"}).ok
    assert not safety.check_dialable("+19005551234", {safety.ALLOW_ALL_ENV: "1"}).ok


# ---- unattended / intervene --------------------------------------------------------------
class Ctx:
    def __init__(self, cron=None, surface=None, boom=False):
        self.cron, self.surface, self.boom = cron, surface, boom

    def current_cron_execution(self):
        if self.boom:
            raise RuntimeError("x")
        return self.cron

    def session_surface(self):
        return self.surface


def test_unattended_detection():
    assert not safety.is_unattended(Ctx())
    assert safety.is_unattended(Ctx(cron={"job": "daily"}))
    assert safety.is_unattended(Ctx(surface="cron"))
    assert safety.is_unattended(Ctx(boom=True))  # cannot tell: fail closed
    assert safety.is_unattended(None, {"platform": "cron"})
    assert safety.is_unattended(None, {"unattended": True})


def test_intervene_refusals():
    live = {"id": "g1", "state": "live", "intervene_allowed": False}
    assert "Only the user" in safety.intervene_refusal(live, unattended=False)
    assert safety.intervene_refusal({**live, "intervene_allowed": True}, unattended=False) == ""
    assert "unattended" in safety.intervene_refusal({**live, "intervene_allowed": True},
                                                    unattended=True)
    assert safety.intervene_refusal(None, unattended=False)
    assert safety.intervene_refusal({**live, "state": "ended", "intervene_allowed": True},
                                    unattended=False)


def test_disclosure_comes_first_once():
    first = safety.intervene_text("Could we do 7:30?", disclosed=False, user_name="Winch")
    assert first.startswith("This is an AI assistant speaking on behalf of Winch.")
    assert safety.intervene_text("Thanks!", disclosed=True, user_name="Winch") == "Thanks!"
