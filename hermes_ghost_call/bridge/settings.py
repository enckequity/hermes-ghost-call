"""Bridge settings. Secrets come only through ``secrets.get``; nothing here is ever logged."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .. import secrets as secretsmod

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766


@dataclass
class BridgeSettings:
    public_base_url: str = ""
    openai_api_key: str = field(default="", repr=False)
    twilio_account_sid: str = field(default="", repr=False)
    twilio_auth_token: str = field(default="", repr=False)
    twilio_from_number: str = ""
    db_path: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    trust_proxy: bool = False
    link_ttl_s: float = 600.0  # a join link works this long, once
    media_ttl_s: float = 60.0  # a media token works this long, once
    tick_s: float = 0.2  # control loop: commands, hangup, duration cap
    end_grace_s: float = 0.3
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BridgeSettings:
        env = os.environ if environ is None else environ  # non-secret config only

        def secret(name: str) -> str:
            return secretsmod.get(name, environ)

        bind_all = bool(env.get("PORT")) and not env.get("HERMES_GHOST_BRIDGE_HOST")
        raw_port = env.get("HERMES_GHOST_BRIDGE_PORT") or env.get("PORT") or str(DEFAULT_PORT)
        try:
            port = int(raw_port)
        except ValueError:
            port = DEFAULT_PORT
        return cls(
            public_base_url=secret("PUBLIC_BASE_URL").strip().rstrip("/"),
            openai_api_key=secret("OPENAI_API_KEY"),
            twilio_account_sid=secret("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=secret("TWILIO_AUTH_TOKEN"),
            twilio_from_number=secret("TWILIO_FROM_NUMBER"),
            db_path=env.get("HERMES_GHOST_DB") or "",
            host=env.get("HERMES_GHOST_BRIDGE_HOST") or ("0.0.0.0" if bind_all else DEFAULT_HOST),  # noqa: S104
            port=port,
            trust_proxy=(env.get("HERMES_GHOST_TRUST_PROXY") or "").strip() == "1",
            environ={**env, **{n: secret(n) for n in secretsmod.SECRET_NAMES}},
        )

    def missing_env(self) -> list[str]:
        """Names (never values) of what web mode needs and is unset."""
        out = []
        if not self.openai_api_key:
            out.append("OPENAI_API_KEY")
        if not self.public_base_url:
            out.append("PUBLIC_BASE_URL")
        return out

    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token
                    and self.twilio_from_number and self.public_base_url)

    def ws_base_url(self) -> str:
        base = self.public_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base
