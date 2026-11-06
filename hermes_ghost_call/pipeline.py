"""Bridge-side hint pipeline: completed utterance -> cheap gate -> (rarely) hint model -> hint.

Latency budget: the gate is pure Python; the hint model is called only when the gate says yes,
with a hard timeout (default 2.5 s). On timeout the hint is skipped, never queued. Inside the
rate-limit window a gated utterance does not stack a second call: it marks the engine pending and
one deferred attempt runs on the latest context when the window opens (coalescing).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import hints as hintsmod
from .llm import LLMError, OpenAIClient
from .session import Store

log = logging.getLogger(__name__)

HintFn = Callable[[list[dict], dict], Awaitable[str]]


class HintEngine:
    def __init__(self, *, store: Store, session: dict, client: OpenAIClient | None,
                 on_hint: Callable[[dict], Any] | None = None,
                 hint_fn: HintFn | None = None, clock: Callable[[], float] | None = None,
                 monotonic: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[Any]] | None = None) -> None:
        cfg = session.get("config") or {}
        self.store = store
        self.sid = session["id"]
        self.goal = session.get("goal", "")
        self.callee_label = session.get("callee_label", "")
        self.cfg = cfg
        self.client = client
        self.on_hint = on_hint
        self.clock = clock or store.clock
        self.mono = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self.live_at = float(session.get("live_at") or self.clock())
        self.keep_turns = bool(cfg.get("store_transcripts", True))
        self.tap_only = cfg.get("hint_mode") == "tap"
        self.timeout_s = float(cfg.get("hint_timeout_ms", 2500)) / 1000.0
        self.limiter = hintsmod.RateLimiter(cfg.get("min_hint_interval_s", 4),
                                            cfg.get("max_hints_per_session", 40),
                                            count=store.hint_count(self.sid))
        self.turns: list[dict] = []  # always in memory (the summary needs it); disk is optional
        self.hint_lines: list[str] = [h["text"] for h in store.hints(self.sid)]
        self._hint_fn = hint_fn or self._model_hint
        self._busy = False
        self._pending = False
        self._pending_explicit = False
        self._deferred: asyncio.Task | None = None
        self._closed = False
        self.decisions: list[str] = []  # gate/attempt outcomes, for tests and /ghost log

    # -- inputs --
    def t_now(self) -> float:
        return max(0.0, self.clock() - self.live_at)

    async def on_utterance(self, speaker: str, text: str) -> str:
        text = " ".join(str(text or "").split())
        if not text or self._closed:
            return "empty"
        turn = {"speaker": speaker, "text": text, "t": self.t_now()}
        recent = list(self.turns)
        self.turns.append(turn)
        if self.keep_turns:
            self.store.add_turn(self.sid, speaker, text, turn["t"])
        if self.tap_only:
            return self._note("tap_mode")
        verdict = hintsmod.gate(speaker, text, recent)
        if not verdict.ok:
            return self._note(f"silent:{verdict.reason}")
        return await self._attempt(explicit=False)

    async def request_hint(self) -> str:
        """The user tapped Hint (their explicit ask): skip the gate, keep the rate limit."""
        if self._closed:
            return "closed"
        return await self._attempt(explicit=True)

    # -- core --
    def _note(self, what: str) -> str:
        self.decisions.append(what)
        return what

    async def _attempt(self, *, explicit: bool) -> str:
        if self.limiter.exhausted():
            return self._note("cap")
        if self._busy:
            self._pending = True
            self._pending_explicit |= explicit
            return self._note("coalesced")
        wait = self.limiter.wait_s(self.mono())
        if wait > 0:
            self._pending = True
            self._pending_explicit |= explicit
            self._schedule(wait)
            return self._note("coalesced")
        self._busy = True
        try:
            messages = hintsmod.build_messages(goal=self.goal, callee_label=self.callee_label,
                                               turns=self.turns, previous_hints=self.hint_lines,
                                               explicit=explicit)
            raw = await asyncio.wait_for(self._hint_fn(messages, self.cfg), self.timeout_s)
        except TimeoutError:
            return self._note("timeout")
        except LLMError as exc:
            return self._note(f"error:{exc.code}")
        except Exception:  # noqa: BLE001 - a hint must never break the call
            log.exception("ghost hint failed")
            return self._note("error")
        finally:
            self._busy = False
            if self._pending and not self._closed:
                self._schedule(self.limiter.wait_s(self.mono()))
        line = hintsmod.clean_hint(raw, self.hint_lines)
        if line is None:
            return self._note("silent:model")
        hint = self.store.add_hint(self.sid, self.t_now(), line)
        self.limiter.mark(self.mono())
        self.hint_lines.append(line)
        if self.on_hint is not None:
            try:
                out = self.on_hint(hint)
                if inspect.isawaitable(out):
                    await out
            except Exception:  # noqa: BLE001
                log.exception("ghost on_hint callback failed")
        return self._note("hint")

    def _schedule(self, delay: float) -> None:
        if self._deferred is not None and not self._deferred.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._deferred = loop.create_task(self._run_deferred(max(0.0, delay)))

    async def _run_deferred(self, delay: float) -> None:
        await self._sleep(delay + 0.01)
        if self._closed or not self._pending:
            return
        explicit = self._pending_explicit
        self._pending = False
        self._pending_explicit = False
        self._deferred = None
        await self._attempt(explicit=explicit)

    async def _model_hint(self, messages: list[dict], cfg: dict) -> str:
        if self.client is None:
            raise LLMError("no_api_key")
        return await self.client.chat(messages, model=cfg.get("model", ""), max_tokens=60,
                                      temperature=0.2, timeout=self.timeout_s)

    async def close(self) -> None:
        self._closed = True
        task = self._deferred
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
