"""hermes-ghost-call: Ghost Call Copilot for Hermes Agent.

Hermes never joins the call. It listens to the user's own call and whispers one-line hints to
the user only (chat ticker plus an optional earpiece whisper).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

__version__ = "0.1.0"

SKILL_NAME = "ghost-call"
SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / "SKILL.md"


def register(ctx: Any) -> None:
    """Register tools, the one hook, the /ghost command, the CLI and the skill.

    Opens no sockets and imports no heavy dependencies (Plugin Doctor blocks the network while
    this runs). The SQLite store opens lazily on first use. No ``pre_llm_call`` hook: Ghost has
    no question cards.
    """
    from . import cli, commands, hooks, tools

    tools.configure(ctx=ctx)
    for name, handler in tools.HANDLERS.items():
        ctx.register_tool(name=name, toolset="ghost", schema=tools.SCHEMAS[name],
                          handler=handler, description=tools.SCHEMAS[name]["description"])
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_command("ghost", commands.ghost_command,
                         description="Ghost Call: silent call copilot (hints to you only)",
                         args_hint=commands.ARGS_HINT)
    ctx.register_cli_command(
        name="ghost", help="Run the ghost bridge and check the setup",
        setup_fn=cli.setup, handler_fn=cli.make_handler(ctx),
        description="hermes ghost serve | doctor")
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is not None and SKILL_PATH.exists():
        register_skill(SKILL_NAME, SKILL_PATH,
                       "Coach the user silently through their own phone call.")
