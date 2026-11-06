"""Shared fixtures: fake Hermes ctx, tmp store, fake clocks, fake OpenAI/STT. No network."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hermes_ghost_call import tools  # noqa: E402
from hermes_ghost_call.session import Store  # noqa: E402

MARKER_KEY = "sk-MARKER-0123456789abcdef-DO-NOT-LEAK"
MARKER_TOKEN = "twilio-MARKER-auth-token-zz"
BASE = "https://ghost.example.test"


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []
        self.ok = True

    def edit(self, text: str) -> dict:
        self.edits.append(text)
        self.text = text
        return {"ok": True}


class FakeCtx:
    """Records register_* calls like Hermes' PluginContext. ``ask_choice`` must never be used."""

    def __init__(self, config: dict | None = None) -> None:
        self.tools: dict[str, dict] = {}
        self.hooks: list[tuple[str, object]] = []
        self.commands: dict[str, dict] = {}
        self.skills: list[dict] = []
        self.cli: dict[str, dict] = {}
        self.messages: list[FakeMessage] = []
        self.asked: list = []
        self.config = dict(config or {})
        self.cron = None
        self.surface = None

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.hooks.append((hook_name, callback))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description,
                               "args_hint": args_hint}

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append({"name": name, "path": Path(path), "description": description})

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = {"setup": setup_fn, "handler": handler_fn}

    def post_message(self, text):
        msg = FakeMessage(text)
        self.messages.append(msg)
        return msg

    def ask_choice(self, question, choices):  # pragma: no cover - asserting it is never called
        self.asked.append((question, choices))
        raise AssertionError("hermes-ghost-call must never ask the user a question")

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def current_cron_execution(self):
        return self.cron

    def session_surface(self):
        return self.surface

    def hook(self, name):
        return next(cb for n, cb in self.hooks if n == name)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeLLM:
    """Stands in for OpenAIClient: scripted chat replies, fake TTS bytes."""

    def __init__(self, replies: list[str] | None = None, *, tts_bytes: bytes = b"\x00\x01" * 2400
                 ) -> None:
        self.replies = list(replies or [])
        self.calls: list[list[dict]] = []
        self.tts_calls: list[str] = []
        self.tts_bytes = tts_bytes
        self.tts_error: Exception | None = None

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "SILENT"

    async def tts(self, text, **kwargs):
        self.tts_calls.append(text)
        if self.tts_error is not None:
            raise self.tts_error
        return self.tts_bytes

    async def aclose(self):
        return None


class FakeSttConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)

    async def recv(self):
        item = await self.inbox.get()
        return item

    async def close(self):
        self.closed = True
        self.inbox.put_nowait(None)


class FakeStt:
    def __init__(self) -> None:
        self.conns: dict[str, FakeSttConn] = {}

    async def __call__(self, role):
        conn = FakeSttConn()
        self.conns[role] = conn
        return conn


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> Store:
    s = Store(path=tmp_path / "ghost.db", clock=clock)
    yield s
    s.close()


@pytest.fixture
def ctx() -> FakeCtx:
    return FakeCtx(config={"user_name": "Winch"})


@pytest.fixture
def env() -> dict:
    return {"PUBLIC_BASE_URL": BASE, "OPENAI_API_KEY": MARKER_KEY}


@pytest.fixture
def svc(store, ctx, env, clock):
    service = tools.configure(ctx=ctx, store=store, environ=env, clock=clock,
                              start_watchers=False)
    yield service
    for w in list(service.watchers.values()):
        w.stop.set()
    tools._service = None


def start(svc, goal="book a table Friday 8pm for 4, budget $80, name Winch", **kw) -> dict:
    return svc.start(goal=goal, callee_label=kw.pop("callee_label", "Bella Napoli"), **kw)
