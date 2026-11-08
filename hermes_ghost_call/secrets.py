"""Profile-scoped secret lookup.

Hermes can serve several profiles from one process, so credentials are never read from the bare
process environment inside Hermes. ``agent.secret_scope.get_secret`` is the profile-scoped route.
Outside Hermes (the bridge as its own process, unit tests) that module is absent; then, and only
then, the process environment is used. A scope error (multiplexing on, no profile scope bound)
fails closed: the secret is reported missing, never read from ``os.environ``.

No function here logs, prints or returns a secret inside an error message.
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping

SECRET_NAMES = frozenset({
    "OPENAI_API_KEY", "PUBLIC_BASE_URL",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "GHOST_CALL_BRIDGE_URL", "GHOST_CALL_BRIDGE_TOKEN", "OPENROUTER_API_KEY",
    "GHOST_CALL_TELNYX_API_KEY", "GHOST_CALL_BRIDGE_SIGNING_KEY", "TEAMSHIFT_USAGE_TOKEN",
})


def get(name: str, environ: Mapping[str, str] | None = None) -> str:
    """Value of secret ``name`` or ``""`` when unset. ``environ`` (tests) wins when given."""
    if environ is not None:
        return str(environ.get(name) or "")
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name) or ""
    try:
        return str(get_secret(name) or "")
    except Exception:  # noqa: BLE001  UnscopedSecretError etc.: fail closed, no os.environ
        return ""


class Environ(Mapping[str, str]):
    """Process environment where the secret names go through :func:`get`."""

    def __getitem__(self, key: str) -> str:
        if key in SECRET_NAMES:
            value = get(key)
            if not value:
                raise KeyError(key)
            return value
        return os.environ[key]

    def __iter__(self) -> Iterator[str]:
        yield from {*os.environ, *(n for n in SECRET_NAMES if get(n))}

    def __len__(self) -> int:
        return sum(1 for _ in self)


def scrub(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace any secret value that leaked into ``text`` (an exception message, say)."""
    out = str(text or "")
    for name in SECRET_NAMES:
        if name in ("PUBLIC_BASE_URL", "GHOST_CALL_BRIDGE_URL"):
            continue
        value = get(name, environ)
        if value and len(value) >= 6 and value in out:
            out = out.replace(value, f"<{name}>")
    return out
