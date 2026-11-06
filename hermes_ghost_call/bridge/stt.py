"""Streaming speech-to-text, one OpenAI Realtime transcription session per labelled channel.

GA transcription sessions: ``wss://api.openai.com/v1/realtime?intent=transcription`` and a
``session.update`` with ``session.type = "transcription"``. Audio goes in as PCM16 24 kHz
(``input_audio_buffer.append``); the server emits
``conversation.item.input_audio_transcription.delta`` (partials) and ``.completed`` (one
utterance). With server VAD the server commits after ~500 ms of silence; models without server VAD
(``gpt-live-*``) get a crude energy VAD here that sends ``input_audio_buffer.commit``.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from . import audio

log = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
SILENCE_MS = 500
SEND_EVERY_FRAMES = 5  # 100 ms of 20 ms frames per append event
MAX_QUEUE = 250  # ~5 s of audio; beyond that the oldest is dropped (a stalled socket)
VAD_ENERGY = 600.0  # mean |sample| above this counts as speech (client VAD only)


class SttConnection(Protocol):
    async def send(self, event: dict) -> None: ...
    async def recv(self) -> dict | None: ...
    async def close(self) -> None: ...


SttConnect = Callable[[str], Awaitable[SttConnection]]


class _WsConnection:
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def recv(self) -> dict | None:
        try:
            raw = await self._ws.recv()
        except Exception:  # noqa: BLE001 - closed
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"type": "unparseable"}
        return data if isinstance(data, dict) else {"type": "unparseable"}

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()


def make_openai_connect(api_key: str) -> SttConnect:
    async def connect(_role: str) -> SttConnection:
        from websockets.asyncio.client import connect as ws_connect

        ws = await ws_connect(REALTIME_URL, additional_headers={
            "Authorization": f"Bearer {api_key}"}, max_size=None, open_timeout=10)
        return _WsConnection(ws)

    return connect


def uses_server_vad(model: str) -> bool:
    return not str(model or "").lower().startswith("gpt-live")


def session_update(model: str) -> dict:
    inp: dict[str, Any] = {
        "format": {"type": "audio/pcm", "rate": 24000},
        "noise_reduction": {"type": "near_field"},
        "transcription": {"model": model},
    }
    inp["turn_detection"] = ({"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
                              "silence_duration_ms": SILENCE_MS}
                             if uses_server_vad(model) else None)
    return {"type": "session.update", "session": {"type": "transcription",
                                                  "audio": {"input": inp}}}


class Transcriber:
    """Feed mu-law frames with :meth:`feed`; utterances arrive through ``on_final``."""

    def __init__(self, role: str, *, connect: SttConnect, model: str,
                 on_final: Callable[[str, str], Awaitable[None]],
                 on_partial: Callable[[str, str], Awaitable[None]] | None = None) -> None:
        self.role = role
        self._connect = connect
        self.model = model
        self.on_final = on_final
        self.on_partial = on_partial
        self._conn: SttConnection | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._partials: dict[str, str] = {}
        self.failed = ""
        self._client_vad = not uses_server_vad(model)
        self._speech_ms = 0
        self._quiet_ms = 0

    async def start(self) -> bool:
        try:
            self._conn = await self._connect(self.role)
            await self._conn.send(session_update(self.model))
        except Exception as exc:  # noqa: BLE001 - never leak details (headers carry the key)
            self.failed = type(exc).__name__
            log.warning("ghost stt %s could not connect (%s)", self.role, self.failed)
            return False
        self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._sender())]
        return True

    def feed(self, ulaw: bytes) -> None:
        if self._conn is None or self.failed:
            return
        if self._queue.qsize() >= MAX_QUEUE:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(ulaw)

    async def _sender(self) -> None:
        assert self._conn is not None
        buf = bytearray()
        frames = 0
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            buf += chunk
            frames += 1
            commit = self._client_vad and self._vad(chunk)
            if frames >= SEND_EVERY_FRAMES or commit:
                pcm = audio.ulaw_to_pcm24k(bytes(buf))
                buf.clear()
                frames = 0
                try:
                    await self._conn.send({"type": "input_audio_buffer.append",
                                           "audio": base64.b64encode(pcm).decode("ascii")})
                    if commit:
                        await self._conn.send({"type": "input_audio_buffer.commit"})
                except Exception:  # noqa: BLE001
                    self.failed = "send_failed"
                    return

    def _vad(self, chunk: bytes) -> bool:
        """Client VAD: True once per utterance, after SILENCE_MS of quiet following speech."""
        ms = len(chunk) // 8
        if audio.ulaw_energy(chunk) >= VAD_ENERGY:
            self._speech_ms += ms
            self._quiet_ms = 0
            return False
        if self._speech_ms < 200:
            self._speech_ms = 0
            return False
        self._quiet_ms += ms
        if self._quiet_ms >= SILENCE_MS:
            self._speech_ms = 0
            self._quiet_ms = 0
            return True
        return False

    async def _reader(self) -> None:
        assert self._conn is not None
        while True:
            event = await self._conn.recv()
            if event is None:
                if not self.failed:
                    self.failed = "closed"
                return
            kind = event.get("type", "")
            if kind == "conversation.item.input_audio_transcription.delta":
                item = str(event.get("item_id") or "")
                text = self._partials.get(item, "") + str(event.get("delta") or "")
                self._partials[item] = text
                if self.on_partial is not None:
                    await self._safe(self.on_partial, text)
            elif kind == "conversation.item.input_audio_transcription.completed":
                self._partials.pop(str(event.get("item_id") or ""), None)
                text = " ".join(str(event.get("transcript") or "").split())
                if text:
                    await self._safe(self.on_final, text)
            elif kind == "error":
                err = event.get("error") or {}
                log.warning("ghost stt %s error: %s", self.role,
                            str(err.get("code") or err.get("type") or "unknown")[:60])

    async def _safe(self, fn: Callable[[str, str], Awaitable[None]], text: str) -> None:
        try:
            await fn(self.role, text)
        except Exception:  # noqa: BLE001
            log.exception("ghost stt callback failed")

    async def close(self) -> None:
        self._queue.put_nowait(None)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._conn is not None:
            await self._conn.close()
