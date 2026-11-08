"""``python -m hermes_ghost_call.bridge``: serve the bridge with uvicorn. Config from env only.
Production runs the same app as ``uvicorn --factory hermes_ghost_call.bridge.app:factory``."""
from __future__ import annotations

import sys

from .app import create_app
from .settings import BridgeSettings


def main() -> int:
    import uvicorn

    settings = BridgeSettings.from_env()
    missing = settings.missing_env()
    if missing:
        # Fail safe, not fatal: /healthz and the call pages stay up, but no transcription or
        # hints run. Names only, never values.
        print("hermes-ghost-call bridge: WARNING missing env: " + ", ".join(missing),
              file=sys.stderr, flush=True)
    if not settings.signing_key and not settings.auth_url:
        print("hermes-ghost-call bridge: no GHOST_CALL_BRIDGE_SIGNING_KEY and no "
              "GHOST_CALL_BRIDGE_AUTH_URL; the /v1 workspace API refuses every token",
              file=sys.stderr, flush=True)
    print(f"hermes-ghost-call bridge on http://{settings.host}:{settings.port}", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
