"""Bridge settings. Secrets come only through ``secrets.get``; nothing here is ever logged."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .. import secrets as secretsmod

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
PUBLIC_URL_ENV = "GHOST_CALL_BRIDGE_PUBLIC_URL"  # PUBLIC_BASE_URL is the older alias
OVERRIDES_ENV = "GHOST_CALL_BRIDGE_OVERRIDES"
CREDENTIALS_ENV = "CREDENTIALS_DIRECTORY"  # systemd LoadCredential= style: one file per secret
DEFAULT_HINT_BASE_URL = "https://openrouter.ai/api/v1"  # any OpenAI-compatible chat API
DEFAULT_HINT_MODEL = "openai/gpt-4.1-mini"  # gateway id; a workspace override can change it
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class Tenant:
    """One workspace on the shared bridge, as resolved from its token plus any override. Its hint
    model key never lives here: the plugin sends it per session, held in memory only."""
    workspace: str
    hint_base_url: str = ""
    hint_model: str = ""
    daily_session_limit: int = 20
    disabled: bool = False


def parse_overrides(raw: str) -> dict[str, Tenant]:
    """``{"<workspace>": {"hint_base_url": ..., "hint_model": ..., "daily_session_limit": ...,
    "disabled": true}}``. Every key is optional. Anything malformed raises ValueError."""
    if not (raw or "").strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{OVERRIDES_ENV} must be a JSON object")
    out: dict[str, Tenant] = {}
    for ws, conf in data.items():
        if not isinstance(ws, str) or not WORKSPACE_RE.match(ws) or not isinstance(conf, dict):
            raise ValueError(f"{OVERRIDES_ENV}: keys are workspace names, values objects")
        base = str(conf.get("hint_base_url") or "").strip().rstrip("/")
        if base and urlsplit(base).scheme != "https":
            raise ValueError(f"{OVERRIDES_ENV}: hint_base_url must be https")
        out[ws] = Tenant(workspace=ws, hint_base_url=base,
                         hint_model=str(conf.get("hint_model") or "").strip()[:120],
                         daily_session_limit=int(max(1, min(500, int(
                             conf.get("daily_session_limit") or 20)))),
                         disabled=conf.get("disabled") is True)
    return out


def credential(name: str, environ: Mapping[str, str] | None = None) -> str:
    """Secret ``name``. With ``CREDENTIALS_DIRECTORY`` set (production) it is read only from the
    file of that name there, never from the environment; without it, the profile-scoped lookup
    (co-located bridge, local dev)."""
    env = os.environ if environ is None else environ
    cred_dir = str(env.get(CREDENTIALS_ENV) or "").strip()
    if not cred_dir:
        return secretsmod.get(name, environ)
    try:
        return (Path(cred_dir) / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
    signing_key: str = field(default="", repr=False)  # HMAC key for <workspace>.<sig> tokens
    auth_url: str = ""  # TeamShift auth base that verifies ts_usg_ usage tokens; '' = path off
    auth_timeout_s: float = 5.0
    overrides: dict[str, Tenant] = field(default_factory=dict)
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
            return credential(name, environ)

        raw_port = env.get("HERMES_GHOST_BRIDGE_PORT") or env.get("PORT") or str(DEFAULT_PORT)
        try:
            port = int(raw_port)
        except ValueError:
            port = DEFAULT_PORT
        return cls(
            public_base_url=(env.get(PUBLIC_URL_ENV) or env.get("PUBLIC_BASE_URL")
                             or "").strip().rstrip("/"),
            openai_api_key=secret("OPENAI_API_KEY"),
            twilio_account_sid=secret("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=secret("TWILIO_AUTH_TOKEN"),
            twilio_from_number=secret("TWILIO_FROM_NUMBER"),
            db_path=env.get("HERMES_GHOST_DB") or "",
            host=env.get("HERMES_GHOST_BRIDGE_HOST") or DEFAULT_HOST,  # PORT alone: loopback
            port=port,
            trust_proxy=(env.get("HERMES_GHOST_TRUST_PROXY") or "").strip() == "1",
            signing_key=secret("GHOST_CALL_BRIDGE_SIGNING_KEY"),
            auth_url=(env.get("GHOST_CALL_BRIDGE_AUTH_URL") or "").strip().rstrip("/"),
            overrides=parse_overrides(env.get(OVERRIDES_ENV) or ""),
            telnyx_api_key=secret("GHOST_CALL_TELNYX_API_KEY"),
            telnyx_connection_id=(env.get("GHOST_CALL_TELNYX_CONNECTION_ID") or "").strip(),
            telnyx_public_key=secret("GHOST_CALL_TELNYX_PUBLIC_KEY"),
            caller_number=(env.get("GHOST_CALL_CALLER_NUMBER") or "").strip(),
            environ={**env, **{n: v for n in secretsmod.SECRET_NAMES if (v := secret(n))}},
        )

    def missing_env(self) -> list[str]:
        """Names (never values) of what web mode needs and is unset."""
        out = []
        if not self.openai_api_key:
            out.append("OPENAI_API_KEY")
        if not self.public_base_url:
            out.append(PUBLIC_URL_ENV)
        return out

    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token
                    and self.twilio_from_number and self.public_base_url)

    def phone_ready(self) -> bool:
        return bool(self.telnyx_api_key and self.telnyx_connection_id and self.caller_number
                    and self.telnyx_public_key and self.public_base_url.startswith("https://"))

    def base_path(self) -> str:
        """The proxy path prefix (``/bridge/ghost``), taken from the public URL."""
        return urlsplit(self.public_base_url).path.rstrip("/")

    def ws_base_url(self) -> str:
        base = self.public_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base
