"""Hard safety rules. Everything here is fail-closed and none of it can be changed by the model.

* PSTN numbers (Twilio mode only): strict E.164, emergency / short-code / premium blocklist, and a
  developer allowlist (``HERMES_GHOST_TEST_ALLOWLIST``). Web mode dials nothing.
* Intervene (Hermes speaking on the far leg): only with a sticky per-session ``/ghost intervene
  allow`` typed by the human, never under cron or another unattended surface, and the first
  spoken words always disclose that an AI assistant is speaking.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ALLOWLIST_ENV = "HERMES_GHOST_TEST_ALLOWLIST"
ALLOW_ALL_ENV = "HERMES_GHOST_ALLOW_ALL"  # developer-only; the model cannot set env vars

# Emergency and service short codes, matched against the national digits and the full input.
EMERGENCY_NUMBERS = frozenset({
    "911", "112", "999", "000", "110", "119", "100", "101", "102", "103", "104", "105",
    "106", "107", "108", "109", "933", "988", "08", "061", "062", "063", "1122",
})
PREMIUM_PREFIXES = ("+1900", "+1976", "+44909", "+44908", "+4490", "+33899", "+49900",
                    "+39899", "+34803", "+34806", "+34807", "+61190", "+972190")
MIN_E164_DIGITS = 8
MAX_E164_DIGITS = 15
UNATTENDED_SURFACES = frozenset({"cron", "scheduler", "schedule", "batch", "unattended",
                                 "webhook", "automation"})
MAX_INTERVENE_CHARS = 240

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str = ""


OK = Check(True)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def validate_number(raw: object) -> tuple[str | None, str]:
    """``(e164, "")`` or ``(None, reason)``. Only clean E.164 passes; short codes never do."""
    text = "" if raw is None else str(raw).strip()
    text = re.sub(r"^tel:\s*", "", text, flags=re.IGNORECASE)
    compact = re.sub(r"[\s().-]", "", text)
    digits = _digits(compact)
    if not digits:
        return None, "no_number"
    if digits in EMERGENCY_NUMBERS or digits.lstrip("0") in EMERGENCY_NUMBERS:
        return None, "emergency_number"
    if not compact.startswith("+"):
        return None, "not_e164"
    if not _E164.match(compact):
        if len(digits) < MIN_E164_DIGITS:
            return None, "short_code"
        return None, "not_e164"
    # National part ending as an emergency code (e.g. +1 911) or a short code in disguise.
    for cc_len in (1, 2, 3):
        national = digits[cc_len:]
        if national in EMERGENCY_NUMBERS:
            return None, "emergency_number"
    if any(compact.startswith(p) for p in PREMIUM_PREFIXES):
        return None, "premium_number"
    if len(digits) > MAX_E164_DIGITS:
        return None, "not_e164"
    return compact, ""


def parse_allowlist(raw: str | None) -> frozenset[str]:
    out = set()
    for part in (raw or "").split(","):
        e164, _ = validate_number(part)
        if e164:
            out.add(e164)
    return frozenset(out)


def check_allowlist(number: str, environ: Mapping[str, str]) -> Check:
    """Fail-closed developer allowlist. Empty or unset allowlist refuses every number."""
    e164, why = validate_number(number)
    if e164 is None:
        return Check(False, why)
    if str(environ.get(ALLOW_ALL_ENV) or "").strip() == "1":
        return OK
    allow = parse_allowlist(environ.get(ALLOWLIST_ENV))
    if not allow:
        return Check(False, f"{ALLOWLIST_ENV} is empty: all dialing is refused")
    if e164 not in allow:
        return Check(False, "number is not on the developer allowlist")
    return OK


def check_dialable(number: str, environ: Mapping[str, str]) -> Check:
    e164, why = validate_number(number)
    if e164 is None:
        return Check(False, why)
    return check_allowlist(e164, environ)


# ---- unattended / intervene ------------------------------------------------------------------
def is_unattended(ctx: Any, kwargs: Mapping[str, Any] | None = None) -> bool:
    """True under cron or any surface without a human watching. Errors count as unattended."""
    kw = dict(kwargs or {})
    for key in ("platform", "source", "surface"):
        if str(kw.get(key) or "").strip().lower() in UNATTENDED_SURFACES:
            return True
    if kw.get("cron") or kw.get("cron_job_id") or kw.get("unattended"):
        return True
    if ctx is None:
        return False
    try:
        probe = getattr(ctx, "current_cron_execution", None)
        if probe is not None and probe():
            return True
    except Exception:  # noqa: BLE001 - cannot tell: fail closed
        return True
    try:
        surface = getattr(ctx, "session_surface", None)
        value = surface() if surface is not None else None
        name = value if isinstance(value, str) else getattr(value, "name", "") or ""
        if str(name).strip().lower() in UNATTENDED_SURFACES:
            return True
        if getattr(value, "unattended", False) is True:
            return True
    except Exception:  # noqa: BLE001
        return True
    return False


def intervene_refusal(session: dict | None, *, unattended: bool) -> str:
    """Why Intervene must not run for ``session`` right now, or ``""``."""
    if unattended:
        return "Intervene is always blocked in cron or unattended runs."
    if session is None:
        return "no such ghost session."
    if session.get("state") != "live":
        return "the ghost session is not live."
    if not session.get("intervene_allowed"):
        return (f"Intervene is off for {session['id']}. Only the user can turn it on, by typing "
                f"/ghost intervene allow {session['id']}.")
    return ""


def disclosure_line(user_name: str) -> str:
    who = (user_name or "").strip() or "the caller"
    return f"This is an AI assistant speaking on behalf of {who}."


def intervene_text(text: str, *, disclosed: bool, user_name: str) -> str:
    """The exact words spoken on the far leg: the disclosure first, the first time only."""
    body = " ".join(str(text or "").split())[:MAX_INTERVENE_CHARS]
    if disclosed:
        return body
    return f"{disclosure_line(user_name)} {body}".strip()
