"""Small OpenAI HTTP client: fast chat completion (hints, summary) and TTS (earpiece whisper).

``httpx`` is imported lazily. Errors never carry the key: callers get :class:`LLMError` with a
short code, and the transport exception text is scrubbed before anything is logged.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import secrets as secretsmod

log = logging.getLogger(__name__)

API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "coral"
TTS_RATE = 24000  # response_format "pcm": 16-bit little-endian mono at 24 kHz
WHISPER_STYLE = "Speak quickly and softly, like a coach whispering in an earpiece."
FAR_LEG_STYLE = "Speak clearly and politely, at a normal phone pace."


def _is_reasoning(model: str) -> bool:
    low = model.lower()
    return low.startswith(("o1", "o3", "o4", "gpt-5")) and "chat" not in low


class LLMError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpenAIClient:
    """One shared ``httpx.AsyncClient`` per bridge; ``api_key`` is held, never printed."""

    def __init__(self, api_key: str, *, base_url: str = API_BASE, http: Any = None) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http

    def __repr__(self) -> str:  # never show the key
        return "OpenAIClient(<key hidden>)"

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and hasattr(self._http, "aclose"):
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict, timeout: float) -> Any:
        if not self._key:
            raise LLMError("no_api_key")
        try:
            resp = await self._client().post(f"{self._base}{path}", headers=self._headers(),
                                             content=json.dumps(body), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS ...
            name = type(exc).__name__
            log.warning("openai %s failed: %s", path, secretsmod.scrub(name))
            raise LLMError("timeout" if "Timeout" in name else "network") from None
        if resp.status_code >= 400:
            raise LLMError(f"http_{resp.status_code}")
        return resp

    async def chat(self, messages: list[dict], *, model: str = DEFAULT_MODEL,
                   max_tokens: int = 60, temperature: float = 0.2, timeout: float = 0.9,
                   json_mode: bool = False) -> str:
        model = model or DEFAULT_MODEL
        body: dict[str, Any] = {"model": model, "messages": messages,
                                "max_completion_tokens": max_tokens}
        if not _is_reasoning(model):  # reasoning models reject a custom temperature
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = await self._post("/chat/completions", body, timeout)
        try:
            data = resp.json()
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception:  # noqa: BLE001
            raise LLMError("bad_response") from None

    async def tts(self, text: str, *, voice: str = DEFAULT_VOICE, style: str = WHISPER_STYLE,
                  model: str = DEFAULT_TTS_MODEL, timeout: float = 6.0) -> bytes:
        """PCM16 mono 24 kHz bytes."""
        body = {"model": model, "input": text[:400], "voice": voice or DEFAULT_VOICE,
                "response_format": "pcm", "instructions": style}
        resp = await self._post("/audio/speech", body, timeout)
        data = resp.content
        if not isinstance(data, bytes | bytearray) or not data:
            raise LLMError("bad_response")
        return bytes(data)
