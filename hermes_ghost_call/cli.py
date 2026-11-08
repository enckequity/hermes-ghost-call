"""``hermes ghost serve`` and ``hermes ghost doctor`` (also behind ``/ghost check``).

Heavy imports stay inside the handlers. Every check reports names, never values.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from . import config as configmod
from . import mode as modemod
from . import remote as remotemod
from . import safety
from . import secrets as secretsmod


def setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="ghost_cmd")
    sub.add_parser("serve", help="Run the ghost bridge (web pages, media, transcription)")
    sub.add_parser("doctor", help="Check env vars, mode and bridge health (names only)")
    mint = sub.add_parser("bridge-token", help="Mint a shared-bridge workspace token from "
                                               "GHOST_CALL_BRIDGE_SIGNING_KEY")
    mint.add_argument("workspace")


def make_handler(ctx: Any = None) -> Callable[[argparse.Namespace], int]:
    def handler(args: argparse.Namespace) -> int:
        cmd = getattr(args, "ghost_cmd", None)
        if cmd == "serve":
            return serve()
        if cmd == "doctor":
            getter = getattr(ctx, "get_config", None) if ctx is not None else None
            return doctor(configmod.resolve(getter))
        if cmd == "bridge-token":
            return bridge_token(args.workspace)
        print("usage: hermes ghost {serve|doctor|bridge-token}")
        return 2

    return handler


def serve() -> int:
    from .bridge.__main__ import main

    return main()


def bridge_token(workspace: str, environ: Mapping[str, str] | None = None) -> int:
    from .bridge.auth import mint
    from .bridge.settings import credential

    try:
        print(mint(credential("GHOST_CALL_BRIDGE_SIGNING_KEY", environ), workspace))
    except ValueError as exc:
        print(f"bridge-token: {exc}")
        return 2
    return 0


def _default_http_get(url: str, timeout: float) -> int:
    import httpx

    try:
        return httpx.get(url, timeout=timeout).status_code
    except Exception:  # noqa: BLE001
        return 0


def run_checks(environ: Mapping[str, str], cfg: dict | None = None,
               http_get: Callable[[str, float], int] | None = None
               ) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) rows. Details name variables, never values."""
    cfg = cfg or configmod.resolve(None)
    out: list[tuple[str, bool, str]] = []
    chosen = modemod.resolve(environ, has_numbers=bool(cfg.get("user_number")))
    out.append(("mode", True, f"{chosen.name} ({chosen.how})"))
    for name in modemod.WEB_ENV:
        ok = bool(str(environ.get(name) or "").strip())
        out.append((f"env {name}", ok, "set" if ok else "MISSING"))
    twilio = [n for n in modemod.TWILIO_ENV if n != "PUBLIC_BASE_URL"]
    t_set = [n for n in twilio if str(environ.get(n) or "").strip()]
    out.append(("twilio (optional)", True,
                "configured" if len(t_set) == len(twilio) and cfg.get("user_number") else
                f"{len(t_set)}/{len(twilio)} vars set; web mode is used"))
    allow = safety.parse_allowlist(environ.get(safety.ALLOWLIST_ENV))
    allow_all = str(environ.get(safety.ALLOW_ALL_ENV) or "").strip() == "1"
    out.append(("dial allowlist", True,
                f"{len(allow)} number(s)" if allow else
                ("ALLOW_ALL developer opt-out is ON" if allow_all
                 else "empty: Twilio dialing refused (web mode dials nothing)")))
    out.append(("limits", True,
                f"hint every >= {cfg['min_hint_interval_s']:g}s, <= "
                f"{cfg['max_hints_per_session']} hints, <= {cfg['max_duration_s']}s, "
                f"{cfg['daily_session_limit']}/day, transcripts "
                f"{'on' if cfg['store_transcripts'] else 'off'}"))
    if remotemod.configured(environ):
        hint_key = bool(str(environ.get(remotemod.HINT_KEY_ENV) or "").strip())
        out.append(("shared bridge", hint_key,
                    f"configured ({remotemod.URL_ENV}); hints need {remotemod.HINT_KEY_ENV}: "
                    + ("set" if hint_key else "MISSING")))
        return out
    base = str(environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        code = (http_get or _default_http_get)(f"{base}/healthz", 3.0)
        out.append(("bridge health", code == 200,
                    "reachable" if code == 200 else
                    f"not reachable at PUBLIC_BASE_URL/healthz (status {code or 'none'}); "
                    "run hermes ghost serve"))
    else:
        out.append(("bridge health", False, "PUBLIC_BASE_URL not set"))
    return out


def doctor(cfg: dict | None = None) -> int:
    rows = run_checks(secretsmod.Environ(), cfg)
    for name, ok, detail in rows:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in rows) else 1

