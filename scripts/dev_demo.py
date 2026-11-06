"""Local web demo without Hermes: the bridge plus a terminal "chat" that shows the ticker.

    python scripts/dev_demo.py 'book a table Friday 8pm for 4, budget $80, name Winch'
    python scripts/dev_demo.py isp      # ready-made scenario: haggle with the internet provider

It starts the bridge on PUBLIC_BASE_URL's port (default http://localhost:8766), creates one ghost
session through the same service the plugin uses, prints the You/Them links, and prints every
chat message the plugin would post (ticker edits and the one summary). Type a command at the
prompt: ``speak``, ``hangup``, ``status``, ``allow`` / ``deny`` (Intervene), ``say <text>``
(Intervene, needs allow) or ``quit``. OPENAI_API_KEY enables transcription and hints.
"""
from __future__ import annotations

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
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host=settings.host,
                                           port=settings.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    tools.configure(ctx=TerminalCtx({"user_name": os.environ.get("GHOST_USER_NAME", "Winch")}))
    print(commands.ghost_command(f"start {goal}"), flush=True)
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
        alias = {"allow": "intervene allow", "deny": "intervene deny"}.get(line, line)
        print(commands.ghost_command(alias))


if __name__ == "__main__":
    raise SystemExit(main())
