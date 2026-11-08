"""One live ghost session inside the bridge process.

Web mode: two browser pages (``you`` and ``them``) send 20 ms mu-law frames; the bridge relays
each side's audio to the other page and feeds a copy to that side's transcriber. Twilio mode: the
two conference legs fork their inbound audio here (receive-only); Twilio mixes the call itself.

What reaches the other party is fixed by construction: ``_relay`` forwards only the user's own
microphone frames to ``them``. Hints, captions and whispers are sent to ``you`` only. The single
path that can put Hermes' voice on the far leg is :meth:`GhostRuntime.intervene`, which re-checks
the session's sticky ``intervene_allowed`` flag in SQLite and prepends the AI disclosure.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .. import safety
from .. import summary as summarymod
from ..llm import FAR_LEG_STYLE, WHISPER_STYLE, LLMError, OpenAIClient
from ..pipeline import HintEngine
from ..session import ENDED, LIVE, Store, elapsed
from . import audio, telnyx_io, twilio_io
from .settings import BridgeSettings
from .stt import SttConnect, Transcriber
from .stt import vocabulary as stt_vocabulary

log = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]
ENDING_REASONS = ("hangup", "you_hung_up", "them_hung_up", "duration_cap", "bridge_shutdown",
                  "call_completed", "you_left", "them_left")


class GhostRuntime:
    def __init__(self, sid: str, *, store: Store, settings: BridgeSettings,
                 client: OpenAIClient | None, stt_connect: SttConnect | None,
                 twilio: twilio_io.TwilioRest | None = None,
                 clock: Callable[[], float] | None = None,
                 on_finished: Callable[[str], None] | None = None,
                 hint_client: OpenAIClient | None | bool = True,
                 telnyx: telnyx_io.TelnyxRest | None = None) -> None:
        self.sid = sid
        self.store = store
        self.settings = settings
        self.client = client  # speech (whisper TTS), held by the bridge
        # Hints and the summary; a shared-bridge session brings its own (True: same as client).
        self.hint_client = client if hint_client is True else (hint_client or None)
        self.telnyx = telnyx
        self.stt_connect = stt_connect
        self.twilio = twilio
        self.clock = clock or store.clock
        self.on_finished = on_finished
        session = store.get_session(sid) or {}
        self.mode = session.get("mode", "web")
        self.cfg = session.get("config") or {}
        # Nothing is transcribed before consent: the confirm on the user's page, or the
        # disclosure finished playing to the other party (phone). Legacy Twilio mode predates it.
        self.consented = self.mode == "twilio" or session.get("consent_at") is not None
        self.usage = {"stt_seconds": 0.0, "tts_chars": 0, **(session.get("usage") or {})}
        self._usage_saved = dict(self.usage)
        self.sockets: dict[str, Send] = {}
        self.transcribers: dict[str, Transcriber] = {}
        self.engine: HintEngine | None = None
        self.ended = asyncio.Event()
        self.summary_task: asyncio.Task | None = None
        self._control: asyncio.Task | None = None
        self._ending = False
        self._whisper_lock = asyncio.Lock()
        self._intervene_lock = asyncio.Lock()
        self.far_leg_log: list[str] = []  # every text spoken to "them" (tests, audit)
        self._announce_texts: dict[str, str] = {}  # one-time announce token -> text (memory)

    # -- lifecycle ---------------------------------------------------------------------------
    def session(self) -> dict:
        return self.store.get_session(self.sid) or {}

    async def attach(self, role: str, send: Send | None) -> None:
        if send is not None:
            self.sockets[role] = send
        if role == "you" and self.store.mark_live(self.sid):
            self.store.add_event(self.sid, "you_joined", {})
        elif role == "them":
            self.store.add_event(self.sid, "them_joined", {})
        if self.engine is None and self.session().get("state") == LIVE:
            self.engine = HintEngine(store=self.store, session=self.session(),
                                     client=self.hint_client, on_hint=self._push_hint,
                                     clock=self.clock)
        await self._start_transcriber(role)
        if self._control is None:
            self._control = asyncio.create_task(self._control_loop())
        other = "them" if role == "you" else "you"
        await self._send(other, {"event": "peer", "joined": True})
        if other in self.sockets:
            await self._send(role, {"event": "peer", "joined": True})

    async def detach(self, role: str) -> None:
        self.sockets.pop(role, None)
        if not self._ending:
            await self.end(f"{role}_left")

    async def _start_transcriber(self, role: str) -> None:
        if role in self.transcribers or self.stt_connect is None:
            if self.stt_connect is None and role == "you":
                await self._send("you", {"event": "notice", "text":
                                         "Transcription is off on this call service."})
            return
        tr = Transcriber(role, connect=self.stt_connect,
                         model=str(self.cfg.get("stt_model") or "gpt-4o-mini-transcribe"),
                         on_final=self._on_final, on_partial=self._on_partial,
                         far_field=self.mode == "room", prompt=self._stt_prompt())
        self.transcribers[role] = tr
        if not await tr.start():
            await self._send("you", {"event": "notice",
                                     "text": f"Transcription for '{role}' is unavailable."})

    # -- inputs ------------------------------------------------------------------------------
    async def on_audio(self, role: str, ulaw: bytes) -> None:
        if self._ending or not ulaw:
            return
        if not self.consented:
            self.consented = self.session().get("consent_at") is not None
        tr = self.transcribers.get(role)
        if tr is not None and self.consented:
            tr.feed(ulaw)
            self.usage["stt_seconds"] += len(ulaw) / 8000.0  # G.711: 8000 bytes a second
        if self.mode == "web":  # room mode relays nothing: the call itself runs on the phone
            await self._relay(role, ulaw)

    async def _relay(self, role: str, ulaw: bytes) -> None:
        """The only path from one page's microphone to the other page's speaker."""
        target = "them" if role == "you" else "you"
        await self._send(target, {"event": "media",
                                  "payload": base64.b64encode(ulaw).decode("ascii")})

    async def on_client_event(self, role: str, msg: dict) -> None:
        kind = msg.get("event")
        if kind == "stop":
            await self.end(f"{role}_hung_up")
        elif role == "you" and kind == "consent" and self.mode in ("web", "room"):
            self.store.mark_consent(self.sid)
            self.consented = True
            await self._send("you", {"event": "consented"})
        elif role == "you" and kind == "speak":
            await self.speak(msg.get("n"))
        elif role == "you" and kind == "hint" and self.engine is not None:
            outcome = await self.engine.request_hint()
            if outcome not in ("hint", "coalesced"):
                await self._send("you", {"event": "notice", "text": _NO_HINT.get(
                    outcome.split(":")[0], "Nothing useful to add right now.")})

    def _stt_prompt(self) -> str:
        """Vocabulary for the transcriber: names and amounts from the goal and the callee, as a
        bare list (never the goal's sentences, which noise can turn into invented lines)."""
        session = self.session()
        return stt_vocabulary(session.get("callee_label") or "", session.get("goal") or "")

    def _speaker(self, role: str) -> str:
        """Room mode: one mic hears both people, so nobody is labelled; the model infers."""
        return "call" if self.mode == "room" else role

    async def _on_final(self, role: str, text: str) -> None:
        speaker = self._speaker(role)
        await self._send("you", {"event": "caption", "role": speaker, "text": text,
                                 "final": True})
        if self.engine is not None:
            await self.engine.on_utterance(speaker, text)

    async def _on_partial(self, role: str, text: str) -> None:
        if role == "them" or self.mode == "room":
            role = self._speaker(role)
            await self._send("you", {"event": "caption", "role": role, "text": text,
                                     "final": False})

    async def _push_hint(self, hint: dict) -> None:
        from ..hints import format_line

        await self._send("you", {"event": "hint", "n": hint["n"], "text": hint["text"],
                                 "line": format_line(hint["t"], hint["text"])})

    # -- earpiece whisper (user only) --------------------------------------------------------
    async def speak(self, n: Any = None) -> str:
        hint = None
        try:
            if n not in (None, "", "latest"):
                hint = self.store.get_hint(self.sid, int(n))
        except (TypeError, ValueError):
            hint = None
        if hint is None:
            rows = self.store.hints(self.sid, limit=1)
            hint = rows[-1] if rows else None
        if hint is None:
            return "no_hint"
        async with self._whisper_lock:
            if self.mode == "twilio":
                ok = await self._announce("you", hint["text"])
                result = "spoken" if ok else "announce_failed"
            elif self.mode == "phone":
                result = await self._phone_whisper(hint["text"])
            else:
                payload: dict[str, Any] = {"event": "whisper", "n": hint["n"],
                                           "text": hint["text"]}
                if self.client is not None:
                    try:
                        pcm = await self.client.tts(hint["text"],
                                                    voice=str(self.cfg.get("voice") or ""),
                                                    style=WHISPER_STYLE, timeout=5.0)
                        payload["pcm24"] = base64.b64encode(pcm).decode("ascii")
                        self.usage["tts_chars"] += len(hint["text"])
                    except LLMError as exc:
                        payload["fallback"] = exc.code  # the page uses local speech synthesis
                await self._send("you", payload)
                result = "spoken"
        self.store.mark_spoken(self.sid, hint["n"])
        return result

    async def _phone_whisper(self, text: str) -> str:
        ccid = (self.session().get("provider") or {}).get("you_ccid") or ""
        if self.telnyx is None or not ccid:
            return "speak_failed"
        try:
            await self.telnyx.speak(ccid, text, self_only=True,
                                    state=telnyx_io.client_state(self.sid, "you", "whisper"))
        except Exception:  # noqa: BLE001
            log.warning("ghost phone whisper failed")
            return "speak_failed"
        self.usage["tts_chars"] += len(text)
        return "spoken"

    def _save_usage(self) -> None:
        if self.usage != self._usage_saved:
            self.usage["stt_seconds"] = round(self.usage["stt_seconds"], 2)
            self.store.set_usage(self.sid, dict(self.usage))
            self._usage_saved = dict(self.usage)

    # -- intervene (far leg; the one gated path) ---------------------------------------------
    async def intervene(self, text: str, *, unattended: bool = False) -> str:
        async with self._intervene_lock:
            session = self.session()
            refusal = safety.intervene_refusal(session, unattended=unattended)
            if refusal:
                self.store.add_event(self.sid, "intervene_refused", {"reason": refusal[:80]})
                return "refused: " + refusal
            body = " ".join(str(text or "").split())[:safety.MAX_INTERVENE_CHARS]
            if not body:
                return "refused: empty text"
            first = self.store.mark_disclosed(self.sid)
            spoken = safety.intervene_text(body, disclosed=not first,
                                           user_name=str(self.cfg.get("user_name") or ""))
            self.far_leg_log.append(spoken)
            self.store.add_event(self.sid, "intervene", {"disclosed_now": first})
            if self.engine is not None:
                self.engine.turns.append({"speaker": "ghost", "text": spoken,
                                          "t": self.engine.t_now()})
            if self.mode == "twilio":
                return "spoken" if await self._announce("them", spoken) else "announce_failed"
            if "them" not in self.sockets:
                return "failed: the other party is not connected"
            if self.client is None:
                return "failed: no speech service on the bridge"
            try:
                pcm = await self.client.tts(spoken, voice=str(self.cfg.get("voice") or ""),
                                            style=FAR_LEG_STYLE, timeout=8.0)
            except LLMError as exc:
                return f"failed: tts {exc.code}"
            ulaw = audio.pcm24k_to_ulaw(pcm)
            for chunk in audio.frames(ulaw, 160 * 20):
                await self._send("them", {"event": "media",
                                          "payload": base64.b64encode(chunk).decode("ascii")})
            await self._send("you", {"event": "notice", "text": f"Ghost said: {spoken}"})
            return "spoken"

    async def _announce(self, role: str, text: str) -> bool:
        """Twilio: conference announcement to one participant. ``role`` picks the leg."""
        if self.twilio is None:
            return False
        provider = self.session().get("provider") or {}
        call_sid = provider.get(f"{role}_call_sid") or ""
        if not call_sid:
            return False
        try:
            conf = provider.get("conference_sid") or await self.twilio.conference_sid(
                twilio_io.conference_name(self.sid))
            if not conf:
                return False
            self.store.set_provider(self.sid, {"conference_sid": conf})
            token = self.store.mint_token(self.sid, role, "announce", 120)
            self.store.add_event(self.sid, "announce_text", {"role": role, "token_for": role})
            self._announce_texts[token] = text
            url = (f"{self.settings.public_base_url}/twilio/{self.sid}/announce/{role}"
                   f"?t={token}")
            await self.twilio.announce(conf, call_sid, url)
            return True
        except Exception:  # noqa: BLE001
            log.warning("ghost announce failed for %s", role)
            return False

    def take_announce_text(self, role: str, token: str) -> str | None:
        if not self.store.consume_token(self.sid, role, "announce", token):
            return None
        return self._announce_texts.pop(token, None)

    # -- control loop ------------------------------------------------------------------------
    async def _control_loop(self) -> None:
        try:
            while not self.ended.is_set():
                session = self.session()
                if not session or session.get("state") == ENDED:
                    await self.end(session.get("end_reason") or "ended")
                    return
                if session.get("hangup_requested"):
                    await self.end("hangup")
                    return
                cap = float(self.cfg.get("max_duration_s") or 900)
                if session.get("live_at") and elapsed(session, self.clock()) >= cap:
                    await self._send("you", {"event": "notice",
                                             "text": "Duration cap reached. Ghost stops now."})
                    await self.end("duration_cap")
                    return
                for cmd in self.store.take_commands(self.sid):
                    await self._run_command(cmd)
                self._save_usage()
                await asyncio.sleep(self.settings.tick_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ghost control loop crashed")
            await self.end("bridge_error")

    async def _run_command(self, cmd: dict) -> None:
        kind, payload = cmd["kind"], cmd.get("payload") or {}
        try:
            if kind == "speak":
                result = await self.speak(payload.get("n"))
            elif kind == "intervene":
                result = await self.intervene(str(payload.get("text") or ""),
                                              unattended=bool(payload.get("unattended")))
            elif kind == "hangup":
                result = "ok"
                self.store.finish_command(cmd["id"], result)
                await self.end("hangup")
                return
            else:
                result = "unknown"
        except Exception:  # noqa: BLE001
            log.exception("ghost command %s failed", kind)
            result = "error"
        self.store.finish_command(cmd["id"], result)

    # -- ending ------------------------------------------------------------------------------
    async def end(self, reason: str) -> None:
        if self._ending:
            return
        self._ending = True
        self.store.end_once(self.sid, reason)
        self._save_usage()
        for role in list(self.sockets):
            await self._send(role, {"event": "ended", "reason": reason})
        for tr in list(self.transcribers.values()):
            with contextlib.suppress(Exception):
                await tr.close()
        if self.engine is not None:
            await self.engine.close()
        if self.mode == "twilio" and self.twilio is not None:
            provider = self.session().get("provider") or {}
            for key in ("you_call_sid", "them_call_sid"):
                if provider.get(key):
                    with contextlib.suppress(Exception):
                        await self.twilio.hangup(provider[key])
        if self.mode == "phone" and self.telnyx is not None:
            provider = self.session().get("provider") or {}
            for key in ("you_ccid", "them_ccid"):
                if provider.get(key):
                    with contextlib.suppress(Exception):
                        await self.telnyx.hangup(provider[key])
        self.ended.set()
        self.summary_task = asyncio.create_task(self._summarise())
        if self._control is not None and self._control is not asyncio.current_task():
            self._control.cancel()

    async def _summarise(self) -> None:
        try:
            session = self.session()
            if session.get("summary") is not None:
                return
            turns = self.engine.turns if self.engine is not None else []
            hint_rows = self.store.hints(self.sid)
            stats = {"duration_s": round(elapsed(session, self.clock()), 1),
                     "hint_count": len(hint_rows), "turn_count": len(turns),
                     "end_reason": session.get("end_reason") or ""}
            result = await summarymod.build(
                self.hint_client, goal=session.get("goal", ""),
                callee_label=session.get("callee_label", ""),
                user_name=str(self.cfg.get("user_name") or ""), turns=turns,
                hint_lines=[h["text"] for h in hint_rows],
                model=str(self.cfg.get("model") or ""), stats=stats)
            self.store.set_summary_once(self.sid, result)
        except Exception:  # noqa: BLE001
            log.exception("ghost summary failed")
            self.store.set_summary_once(self.sid, {**summarymod.fallback("summary failed"),
                                                   "end_reason": "error"})
        finally:
            if self.hint_client is not None and self.hint_client is not self.client:
                with contextlib.suppress(Exception):
                    await self.hint_client.aclose()
            if self.on_finished is not None:
                self.on_finished(self.sid)

    async def _send(self, role: str, msg: dict) -> None:
        send = self.sockets.get(role)
        if send is None:
            return
        try:
            await send(msg)
        except Exception:  # noqa: BLE001 - a dead socket is handled by its reader
            self.sockets.pop(role, None)


_NO_HINT = {
    "silent": "Nothing useful to add right now.",
    "timeout": "The hint model was too slow; skipped.",
    "cap": "Hint limit for this call reached.",
    "error": "Hints are unavailable right now.",
}
