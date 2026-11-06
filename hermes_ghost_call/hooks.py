"""The one lifecycle hook. Callbacks accept ``**kwargs`` (Plugin Doctor requires it).

``pre_tool_call`` vetoes ``ghost_intervene`` unless the session has the sticky allow the user
typed (``/ghost intervene allow``), and always under cron or other unattended runs. It never
returns ``approve`` (no approval prompt) and never touches the hint tools. Fails closed.
There is deliberately no ``pre_llm_call`` hook: Ghost has no question-card pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from . import safety, tools

log = logging.getLogger(__name__)

GATED_TOOLS = frozenset({"ghost_intervene"})


def pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any) -> dict | None:
    if tool_name not in GATED_TOOLS:
        return None
    try:
        svc = tools.get_service()
        sid = args.get("session_id") if isinstance(args, dict) else None
        session = svc.find(sid if isinstance(sid, str) else None)
        refusal = safety.intervene_refusal(
            session, unattended=safety.is_unattended(svc.ctx, kwargs))
        if refusal:
            return _block(refusal)
        return None
    except Exception:  # noqa: BLE001
        log.exception("hermes-ghost-call pre_tool_call failed; blocking")
        return _block("Intervene could not be verified.")


def _block(message: str) -> dict:
    return {"action": "block", "message": f"hermes-ghost-call: {message}"}
