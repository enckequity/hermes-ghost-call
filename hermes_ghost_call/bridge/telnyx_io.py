"""Phone mode on Telnyx Call Control, without an SDK: webhook signatures and five commands.

Shape of a phone session: the bridge rings the customer (leg ``you``); when they answer it rings
the other party (leg ``them``). When ``them`` answers, the disclosure is spoken to that leg
alone, and only after it has finished playing are the legs bridged and audio forked to the
bridge (one receive-only ``inbound_track`` stream per leg, so each stream is one speaker and the
whisper played to the customer can never be transcribed as the other party). Whispers are
``speak`` on the customer's leg with ``target_legs: self``.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from .. import secrets as secretsmod

log = logging.getLogger(__name__)

API = "https://api.telnyx.com/v2"
MAX_SKEW_S = 300
VOICE = "female"
LANGUAGE = "en-US"
# Spoken to the other party before any of their audio is transcribed. Not configurable.
DISCLOSURE = "This call is being transcribed by an assistant."


def verify(public_key_b64: str, body: bytes, headers: Mapping[str, str],
           now: float | None = None) -> bool:
    """Telnyx signs ``{timestamp}|{body}`` with Ed25519. False on anything missing, stale or
    forged, and when no key is configured (fail closed)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        stamp = headers["telnyx-timestamp"]
        if abs((time.time() if now is None else now) - int(stamp)) > MAX_SKEW_S:
            return False
        key.verify(base64.b64decode(headers["telnyx-signature-ed25519"], validate=True),
                   stamp.encode() + b"|" + body)
    except (KeyError, ValueError, TypeError, binascii.Error, InvalidSignature):
        return False
    return True


def client_state(sid: str, leg: str, kind: str = "") -> str:
    return base64.b64encode(json.dumps({"sid": sid, "leg": leg, "kind": kind}).encode()).decode()


def read_client_state(raw: Any) -> dict:
    try:
        data = json.loads(base64.b64decode(str(raw or ""), validate=True))
    except (ValueError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


class TelnyxRest:
    """Minimal async Call Control client. The key is held privately and never logged."""

    def __init__(self, api_key: str, connection_id: str, *, http: Any = None) -> None:
        self._key = api_key
        self.connection_id = connection_id
        self._http = http

    def __repr__(self) -> str:
        return "TelnyxRest(<credentials hidden>)"

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        return self._http

    async def _post(self, path: str, body: dict) -> dict:
        try:
            resp = await self._client().post(API + path, json=body,
                                             headers={"Authorization": f"Bearer {self._key}"})
        except Exception as exc:  # noqa: BLE001
            log.warning("phone command failed: %s", secretsmod.scrub(type(exc).__name__))
            raise RuntimeError("phone_network") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"phone_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data.get("data") or {} if isinstance(data, dict) else {}

    async def dial(self, *, to: str, from_: str, webhook_url: str, state: str) -> str:
        data = await self._post("/calls", {
            "connection_id": self.connection_id, "to": to, "from": from_,
            "webhook_url": webhook_url, "client_state": state, "timeout_secs": 30})
        return str(data.get("call_control_id") or "")

    async def speak(self, ccid: str, text: str, *, state: str = "", self_only: bool = False
                    ) -> None:
        body: dict[str, Any] = {"payload": text[:400], "voice": VOICE, "language": LANGUAGE}
        if state:
            body["client_state"] = state
        if self_only:
            # shortcut: the docs do not say whether target_legs=self on a bridged leg stays
            # one-sided; the live two-phone check in enckequity/teamshift#1090 slice 3 must
            # confirm the other party hears nothing before phone mode ships.
            body["target_legs"] = "self"
        await self._post(f"/calls/{ccid}/actions/speak", body)

    async def bridge(self, ccid: str, other_ccid: str) -> None:
        await self._post(f"/calls/{ccid}/actions/bridge", {"call_control_id": other_ccid})

    async def stream(self, ccid: str, url: str) -> None:
        await self._post(f"/calls/{ccid}/actions/streaming_start",
                         {"stream_url": url, "stream_track": "inbound_track"})

    async def hangup(self, ccid: str) -> None:
        await self._post(f"/calls/{ccid}/actions/hangup", {})
