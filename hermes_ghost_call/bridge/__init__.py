"""The ghost media bridge (FastAPI). Imported only by ``hermes ghost serve`` and the tests."""
from __future__ import annotations

from .app import create_app
from .settings import BridgeSettings

__all__ = ["BridgeSettings", "create_app"]
