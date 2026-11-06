"""Local web demo without Hermes: the bridge plus a terminal "chat" that shows the ticker.

    python scripts/dev_demo.py "book a table Friday 8pm for 4, budget $80, name Winch"

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
    goal = " ".join(sys.argv[1:]) or "book a table Friday 8pm for 4, budget $80, name Winch"
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8766")
    os.environ.setdefault("HERMES_GHOST_DB", str(Path(tempfile.gettempdir()) /
                                                 "hermes-ghost-demo.db"))
    os.environ.setdefault("HERMES_GHOST_BRIDGE_PORT",
                          os.environ["PUBLIC_BASE_URL"].rsplit(":", 1)[-1].strip("/") or "8766")
    if not os.environ.get("OPENAI_API_KEY", "").strip() and sys.stdin.isatty():
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
