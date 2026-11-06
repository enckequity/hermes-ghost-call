"""Twilio mode, without the Twilio SDK: TwiML builders, webhook signatures, three REST calls.

Shape of a Twilio ghost session: Twilio rings the user's own phone (leg ``you``); when they pick
up, their TwiML forks the leg's inbound audio to the bridge (``<Start><Stream>``, receive-only)
and joins a conference. The bridge then rings the callee (leg ``them``) into the same conference
with its own receive-only stream. Ghost is never a conference participant, so it cannot speak on
the line. Earpiece whispers are a conference *announcement to the user's participant only*;
Intervene is an announcement to the callee's participant, gated like everywhere else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode
from xml.sax.saxutils import escape, quoteattr

from .. import secrets as secretsmod

log = logging.getLogger(__name__)

API = "https://api.twilio.com/2010-04-01"
SAY_VOICE = "Polly.Joanna"


def parse_form(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def valid_signature(auth_token: str, url: str, params: Mapping[str, str], sig: str | None
                    ) -> bool:
    if not auth_token or not sig:
        return False
    return hmac.compare_digest(signature(auth_token, url, params), sig)


def public_url(base: str, path: str, query: str = "") -> str:
    return base.rstrip("/") + path + (f"?{query}" if query else "")


def conference_name(sid: str) -> str:
    return f"ghost-{sid}"


def leg_twiml(*, sid: str, role: str, ws_url: str, token: str, status_url: str) -> str:
    stream = (f"<Start><Stream url={quoteattr(ws_url)} track=\"inbound_track\">"
              f"<Parameter name=\"token\" value={quoteattr(token)}/></Stream></Start>")
    intro = ""
    if role == "you":
        intro = (f"<Say voice=\"{SAY_VOICE}\">Ghost is listening, muted to the other party. "
                 "Connecting your call.</Say>")
    conf = (f"<Dial><Conference startConferenceOnEnter=\"true\" endConferenceOnExit=\"true\" "
            f"beep=\"false\" statusCallback={quoteattr(status_url)} "
            f"statusCallbackEvent=\"end\">{escape(conference_name(sid))}</Conference></Dial>")
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>{stream}{intro}{conf}</Response>"


def say_twiml(text: str) -> str:
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say voice=\"{SAY_VOICE}\">"
            f"{escape(text)}</Say></Response>")


def empty_twiml() -> str:
    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>"


class TwilioRest:
    """Minimal async REST client. Credentials are held privately and never logged."""

    def __init__(self, account_sid: str, auth_token: str, *, http: Any = None) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._http = http

    def __repr__(self) -> str:
        return "TwilioRest(<credentials hidden>)"

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"{API}/Accounts/{self._sid}{path}"
        try:
            resp = await self._client().request(
                method, url, auth=(self._sid, self._token),
                content=urlencode(data or {}) if data is not None else None,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        except Exception as exc:  # noqa: BLE001
            log.warning("twilio %s failed: %s", path.split("/")[1] if "/" in path else path,
                        secretsmod.scrub(type(exc).__name__))
            raise RuntimeError("twilio_network") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"twilio_http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            return {}

    async def create_call(self, *, to: str, from_: str, url: str, status_url: str) -> str:
        data = await self._request("POST", "/Calls.json", {
            "To": to, "From": from_, "Url": url, "Method": "POST",
            "StatusCallback": status_url, "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "completed", "Timeout": "30"})
        return str(data.get("sid") or "")

    async def hangup(self, call_sid: str) -> None:
        await self._request("POST", f"/Calls/{call_sid}.json", {"Status": "completed"})

    async def conference_sid(self, friendly_name: str) -> str:
        path = "/Conferences.json?" + urlencode({"FriendlyName": friendly_name,
                                                 "Status": "in-progress"})
        data = await self._request("GET", path)
        confs = data.get("conferences") or []
        return str(confs[0].get("sid") or "") if confs else ""

    async def announce(self, conference_sid: str, call_sid: str, url: str) -> None:
        """Play ``url``'s TwiML to one participant only (the other leg does not hear it)."""
        await self._request("POST", f"/Conferences/{conference_sid}/Participants/"
                                    f"{call_sid}.json",
                            {"AnnounceUrl": url, "AnnounceMethod": "POST"})
