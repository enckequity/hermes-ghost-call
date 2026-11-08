"""Web mode (two browser pages) or Twilio mode (two conference legs). Pure env logic.

``HERMES_GHOST_MODE=web|twilio`` or an explicit ``mode`` argument wins. Otherwise Twilio is used
only when it is fully configured; anything incomplete falls back to web mode (the hermes-phone
rule), so the demo never needs a Twilio Trust Hub.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

MODE_ENV = "HERMES_GHOST_MODE"
TWILIO = "twilio"
WEB = "web"
ROOM = "room"  # speakerphone: one page, one mic hears both sides of any call; never automatic
MODES = (WEB, TWILIO, ROOM)
WEB_ENV = ("OPENAI_API_KEY", "PUBLIC_BASE_URL")
TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL")


@dataclass
class Mode:
    name: str
    missing: list[str] = field(default_factory=list)  # env names still needed
    how: str = ""  # "explicit", "auto" or "fallback"
    note: str = ""


def _unset(environ: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    return [k for k in names if not str(environ.get(k) or "").strip()]


def resolve(environ: Mapping[str, str], requested: str = "", *, has_numbers: bool = False
            ) -> Mode:
    want = (requested or str(environ.get(MODE_ENV) or "")).strip().lower()
    web_missing = _unset(environ, WEB_ENV)
    twilio_missing = _unset(environ, TWILIO_ENV) + _unset(environ, ("OPENAI_API_KEY",))
    if not has_numbers:
        twilio_missing.append("user_number config + callee_number")
    if want == WEB:
        return Mode(WEB, web_missing, "explicit")
    if want == ROOM:
        return Mode(ROOM, web_missing, "explicit")
    if want in (TWILIO, "phone"):  # "phone" is the name the tool schema shows
        if not twilio_missing:
            return Mode(TWILIO, [], "explicit")
        return Mode(WEB, web_missing, "fallback",
                    "Twilio is not fully configured (" + ", ".join(twilio_missing)
                    + "), so this session uses web mode.")
    if not twilio_missing:
        return Mode(TWILIO, [], "auto")
    return Mode(WEB, web_missing, "auto")
