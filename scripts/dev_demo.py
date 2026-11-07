"""Local web demo without Hermes: the bridge plus a terminal "chat" that shows the ticker.

    python scripts/dev_demo.py 'book a table Friday 8pm for 4, budget $80, name Winch'
    python scripts/dev_demo.py isp      # ready-made scenario: haggle with the internet provider

It starts the bridge on PUBLIC_BASE_URL's port (default http://localhost:8766), creates one ghost
session through the same service the plugin uses, prints the You/Them links, and prints every
chat message the plugin would post (ticker edits and the one summary). Type a command at the
prompt: ``speak``, ``hangup``, ``status``, ``allow`` / ``deny`` (Intervene), ``say <text>``
(Intervene, needs allow), ``them <line>`` / ``you <line>`` (rehearse by typing, no mic) or
``quit``. OPENAI_API_KEY enables transcription and hints.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ready-made demo calls: ``python scripts/dev_demo.py isp``. Each brief is the cheat sheet for
# the person playing the other party (see docs/DEMO.md for the full talking tracks).
SCENARIOS = {
    "table": {
        "goal": "book a table Friday 8pm for 4, budget $80, name Winch",
        "brief": "Scenario: book a table. The other party plays the restaurant: Friday is full, "
                 "only Saturday; the set menu is $95; ask for a name.",
    },
    "isp": {
        "goal": "get a lower price on my home internet. I pay $89/month for 500 Mbps, customer "
                "4 years, contract ended in October. FastNet offers 1 Gbps at $55/month for 12 "
                "months. Target $60/month or less, no new contract. Walk away: cancel and "
                "switch. Name Winch, account 4471 with Northwind Fiber retention",
        "brief": "Scenario: haggle with the internet provider. The other party plays Northwind "
                 "Fiber support: open at $79 on a 2-year contract, then $70 for 6 months, "
                 "move to retention, finally $60 for 12 months with no contract, plus a $10 "
                 "router fee if the caller does not ask.",
    },
}


DEMO_ENV_KEYS = ("OPENAI_API_KEY", "PUBLIC_BASE_URL", "GHOST_USER_NAME")


def load_demo_env(path: Path, environ=os.environ) -> None:
    """Fill a few demo settings from a local, git-ignored ``demo.env`` (KEY=value lines).

    Only this local demo reads it; the Hermes plugin itself never reads env files. Values already
    set in the process env win, unknown keys are ignored, and nothing is ever printed.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'").strip()
        if key in DEMO_ENV_KEYS and value and not environ.get(key, "").strip():
            environ[key] = value


def port_free(host: str, port: int) -> bool:
    """True when nothing listens on host:port (checked before uvicorn, which would hang us)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


async def rehearse(bridge, service, role: str, text: str) -> str:
    """Feed one typed line into the live session as if it had been transcribed from the call.

    Without an open You page this attaches a silent "you" side, so the call goes live and hints
    run; opening the You page later still works.
    """
    active = service.store.active_sessions()
    rt = bridge.runtime(active[0]["id"]) if active else None
    if rt is None:
        return "no active ghost session (start a new one with: start <goal>)"
    if rt.engine is None:
        await rt.attach("you", None)
    if rt.engine is None:
        return "the session is not live yet"
    await rt._on_final(role, text)
    return f"({role}: {text})"


class TerminalMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.ok = True
        _show(text, "post")

    def edit(self, text: str) -> dict:
        if text != self.text:
            self.text = text
            _show(text, "edit")
        return {"ok": True}


def _show(text: str, how: str) -> None:
    bar = "-" * 60
    print(f"\n{bar}\n[hermes chat {how}]\n{text}\n{bar}", flush=True)


class TerminalCtx:
    def __init__(self, config: dict) -> None:
        self.config = config

    def post_message(self, text: str) -> TerminalMessage:
        return TerminalMessage(text)

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    scenario = SCENARIOS.get(args[0].lower()) if len(args) == 1 else None
    goal = scenario["goal"] if scenario else " ".join(args) or SCENARIOS["table"]["goal"]
    if scenario:
        print(scenario["brief"], flush=True)
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8766")
    os.environ.setdefault("HERMES_GHOST_DB", str(Path(tempfile.gettempdir()) /
                                                 "hermes-ghost-demo.db"))
    os.environ.setdefault("HERMES_GHOST_BRIDGE_PORT",
                          os.environ["PUBLIC_BASE_URL"].rsplit(":", 1)[-1].strip("/") or "8766")
    load_demo_env(ROOT / "demo.env")
    # Both ends must be a terminal: on Windows a redirected NUL stdin still reports isatty().
    if not os.environ.get("OPENAI_API_KEY", "").strip() and sys.stdin.isatty() \
            and sys.stdout.isatty():
        import getpass  # hidden input: the key never shows on screen or in shell history

        key = getpass.getpass("OpenAI API key (paste, hidden; Enter to skip): ").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
    import uvicorn

    from hermes_ghost_call import commands, tools
    from hermes_ghost_call.bridge import BridgeSettings, create_app

    settings = BridgeSettings.from_env()
    if settings.missing_env():
        print("note: missing " + ", ".join(settings.missing_env()) +
              " (the call works; transcription and hints need OPENAI_API_KEY)")
    if not port_free(settings.host, settings.port):
        print(f"Port {settings.port} is already in use, most likely by an earlier demo that is "
              "still running. Close that terminal or stop it:\n"
              f"  Get-NetTCPConnection -LocalPort {settings.port} -State Listen | "
              "ForEach-Object { Stop-Process -Id $_.OwningProcess }\n"
              "or use another port: set PUBLIC_BASE_URL=http://localhost:8767 in demo.env.",
              flush=True)
        return 1
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                           log_level="warning"))
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    service = tools.configure(ctx=TerminalCtx({"user_name":
                                               os.environ.get("GHOST_USER_NAME", "Winch")}))
    print(commands.ghost_command(f"start {goal}"), flush=True)
    print("Rehearse without a mic or a friend: type `them <line>` for what the other party says "
          "and `you <line>` for your side; hints appear here.", flush=True)
    while True:
        try:
            line = input("\nghost> ").strip()
        except EOFError:  # no terminal attached (background run): just keep serving
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            line = "quit"
        if line in ("quit", "exit"):
            commands.ghost_command("hangup")
            time.sleep(1.0)
            return 0
        if line.startswith("say "):
            print(tools.HANDLERS["ghost_intervene"]({"text": line[4:]}))
            continue
        role, _, text = line.partition(" ")
        if role in ("them", "you") and text.strip():
            future = asyncio.run_coroutine_threadsafe(
                rehearse(app.state.bridge, service, role, text.strip()), loop)
            print(future.result(timeout=10))
            continue
        alias = {"allow": "intervene allow", "deny": "intervene deny"}.get(line, line)
        print(commands.ghost_command(alias))


if __name__ == "__main__":
    raise SystemExit(main())
