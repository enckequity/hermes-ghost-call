"""Plugin settings with defaults and hard clamps. A snapshot is stored on each session row so the
bridge (a separate process without ``ctx``) applies exactly the limits the plugin promised."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import hints as hintsmod
from .llm import DEFAULT_MODEL, DEFAULT_VOICE

DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
DEFAULTS: dict[str, Any] = {
    "hint_mode": "auto",
    "min_hint_interval_s": hintsmod.MIN_INTERVAL_FLOOR_S,
    "max_hints_per_session": hintsmod.MAX_HINTS_CEILING,
    "model": DEFAULT_MODEL,
    "stt_model": DEFAULT_STT_MODEL,
    "voice": DEFAULT_VOICE,
    "max_duration_s": 900,
    "daily_session_limit": 20,
    "store_transcripts": True,
    "user_name": "",
    "user_number": "",
    "hint_timeout_ms": 900,
}
MAX_DURATION_HARD_S = 3600
MIN_DURATION_S = 60


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def resolve(get: Callable[[str, Any], Any] | None) -> dict[str, Any]:
    """Every setting, clamped. ``get`` is ``ctx.get_config`` (or None in tests)."""
    def raw(key: str) -> Any:
        if get is None:
            return DEFAULTS[key]
        try:
            value = get(key, None)
        except Exception:  # noqa: BLE001
            value = None
        return DEFAULTS[key] if value is None else value

    mode = str(raw("hint_mode") or "auto").strip().lower()
    return {
        "hint_mode": mode if mode in ("auto", "tap") else "auto",
        "min_hint_interval_s": max(hintsmod.MIN_INTERVAL_FLOOR_S,
                                   _num(raw("min_hint_interval_s"), 4)),
        "max_hints_per_session": int(max(1, min(hintsmod.MAX_HINTS_CEILING,
                                                _num(raw("max_hints_per_session"), 40)))),
        "model": str(raw("model") or DEFAULT_MODEL).strip(),
        "stt_model": str(raw("stt_model") or DEFAULT_STT_MODEL).strip(),
        "voice": str(raw("voice") or DEFAULT_VOICE).strip(),
        "max_duration_s": int(max(MIN_DURATION_S, min(MAX_DURATION_HARD_S,
                                                      _num(raw("max_duration_s"), 900)))),
        "daily_session_limit": int(max(1, min(500, _num(raw("daily_session_limit"), 20)))),
        "store_transcripts": _bool(raw("store_transcripts"), True),
        "user_name": str(raw("user_name") or "").strip()[:60],
        "user_number": str(raw("user_number") or "").strip(),
        "hint_timeout_ms": int(max(300, min(3000, _num(raw("hint_timeout_ms"), 900)))),
    }
