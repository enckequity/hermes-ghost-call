"""Bridge settings. Secrets come only through ``secrets.get``; nothing here is ever logged."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .. import secrets as secretsmod

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
TENANTS_ENV = "GHOST_CALL_BRIDGE_TENANTS"
DEFAULT_HINT_BASE_URL = "https://openrouter.ai/api/v1"  # any OpenAI-compatible chat API
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class Tenant:
    """One workspace on the shared bridge. Its hint model key never lives here: the plugin sends
    it per session and the bridge keeps it in memory only."""
    workspace: str
    hint_base_url: str = ""
    hint_model: str = ""
    daily_session_limit: int = 20


def parse_tenants(raw: str) -> dict[str, Tenant]:
    """``{"<sha256 of the workspace token>": {"workspace": ..., "hint_base_url": ...,
    "hint_model": ..., "daily_session_limit": ...}}``. Anything malformed raises ValueError."""
    if not (raw or "").strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{TENANTS_ENV} must be a JSON object")
    out: dict[str, Tenant] = {}
    seen: set[str] = set()
    for digest, conf in data.items():
        if not isinstance(digest, str) or not _SHA256.match(digest) or not isinstance(conf, dict):
            raise ValueError(f"{TENANTS_ENV}: keys are lowercase sha256 hex of a token")
        ws = conf.get("workspace")
        if not isinstance(ws, str) or not _WORKSPACE.match(ws) or ws in seen:
            raise ValueError(f"{TENANTS_ENV}: each tenant needs a unique workspace id")
        base = str(conf.get("hint_base_url") or "").strip().rstrip("/")
        if base and urlsplit(base).scheme != "https":
            raise ValueError(f"{TENANTS_ENV}: hint_base_url must be https")
        seen.add(ws)
        out[digest] = Tenant(workspace=ws, hint_base_url=base,
                             hint_model=str(conf.get("hint_model") or "").strip(),
                             daily_session_limit=int(max(1, min(500, int(
                                 conf.get("daily_session_limit") or 20)))))
    return out


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
    tenants: dict[str, Tenant] = field(default_factory=dict, repr=False)
    telnyx_api_key: str = field(default="", repr=False)
    telnyx_connection_id: str = ""
    telnyx_public_key: str = ""  # base64 Ed25519 key that signs webhooks; unset = all refused
    caller_number: str = ""  # caller ID on both phone legs
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
            tenants=parse_tenants(env.get(TENANTS_ENV) or ""),
            telnyx_api_key=secret("GHOST_CALL_TELNYX_API_KEY"),
            telnyx_connection_id=(env.get("GHOST_CALL_TELNYX_CONNECTION_ID") or "").strip(),
            telnyx_public_key=(env.get("GHOST_CALL_TELNYX_PUBLIC_KEY") or "").strip(),
            caller_number=(env.get("GHOST_CALL_CALLER_NUMBER") or "").strip(),
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

    def phone_ready(self) -> bool:
        return bool(self.telnyx_api_key and self.telnyx_connection_id and self.caller_number
                    and self.telnyx_public_key and self.public_base_url.startswith("https://"))

    def ws_base_url(self) -> str:
        base = self.public_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base
