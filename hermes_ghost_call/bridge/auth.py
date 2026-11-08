"""Who is calling the workspace API. Two bearer shapes, no tenant list to keep in sync:

* ``<workspace>.<sig>``: sig is base64url (no padding) of HMAC-SHA256(signing key,
  ``ghost-bridge-v1|<workspace>``). Minted by ``hermes ghost bridge-token`` (the owner).
* ``ts_usg_<64 hex>`` plus an ``X-TeamShift-Workspace`` header: the workspace's existing usage
  token, checked against the TeamShift auth service. Only successes are cached, for 10 minutes.

Anything else, or any doubt (auth service down, slow, odd status), is refused. Tokens are never
logged; the cache keys on a hash.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from .settings import WORKSPACE_RE, BridgeSettings, Tenant

TOKEN_PREFIX = "ghost-bridge-v1|"
USAGE_PREFIX = "ts_usg_"
USAGE_TOKEN_RE = re.compile(r"^ts_usg_[0-9a-f]{64}$")
USAGE_NAME_RE = re.compile(r"^t-[0-9a-f]{10}$")
WORKSPACE_HEADER = "x-teamshift-workspace"
USAGE_PATH = "/api/workspace/usage"
CACHE_S = 600.0
DENY_CACHE_S = 60.0  # a 401 is remembered this long, so a flood of guesses stays local
MAX_CHECKS = 8  # concurrent auth-service calls; the rest wait their turn
CHECK_BUDGET = 120  # auth-service calls per minute across all workspaces
CHECK_WINDOW_S = 60.0
KNOWN_GOOD_S = 86400.0  # a key that verified this recently re-verifies outside the budget
KNOWN_GOOD_MAX = 10000
CACHE_MAX = 4096
_SIG_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # 32 bytes, base64url, unpadded
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def sign(key: str, workspace: str) -> str:
    mac = hmac.new(key.encode("utf-8"), (TOKEN_PREFIX + workspace).encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def mint(key: str, workspace: str) -> str:
    if not key:
        raise ValueError("GHOST_CALL_BRIDGE_SIGNING_KEY is not set")
    if not WORKSPACE_RE.match(workspace):
        raise ValueError("workspace must match ^[a-z0-9][a-z0-9-]{0,62}$")
    return f"{workspace}.{sign(key, workspace)}"


def signed_workspace(key: str, token: str) -> str | None:
    """The workspace a ``<workspace>.<sig>`` token names, or None if it is not genuine."""
    workspace, dot, sig = token.rpartition(".")
    if not key or not dot or not WORKSPACE_RE.match(workspace) or not _SIG_RE.match(sig):
        return None
    return workspace if hmac.compare_digest(sig, sign(key, workspace)) else None


class AuthBusy(Exception):
    """The auth-service budget is spent: answer 429, never call it."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # never resend the token
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _usage_status(url: str, token: str, name: str, timeout: float) -> int:
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}", WORKSPACE_HEADER: name,
        "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001  timeout, refused, TLS: unavailable, fail closed
        return 0


class Authenticator:
    def __init__(self, settings: BridgeSettings, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self._cache: dict[str, tuple[str, float]] = {}  # sha256(name+token) -> (name, expiry)
        self._denied: dict[str, tuple[str, float]] = {}  # same key, 401 answers only
        self._checks = asyncio.Semaphore(MAX_CHECKS)
        self._spent: deque[float] = deque()  # times of recent auth-service calls
        self._known_good: dict[str, float] = {}  # key -> last success, oldest first
        url = settings.auth_url
        parts = urllib.parse.urlsplit(url)
        ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS)
        self.usage_url = url + USAGE_PATH if url and ok else ""

    def tenant(self, workspace: str) -> Tenant:
        return self.settings.overrides.get(workspace) or Tenant(workspace=workspace)

    async def resolve(self, headers: Mapping[str, str], *,
                      admit: Callable[[], bool] | None = None) -> Tenant | None:
        """The calling workspace (possibly ``disabled``), or None: refuse with 401. Before an
        auth-service lookup, ``admit()`` must allow it (the caller's per-IP limit). Raises
        AuthBusy when admit refuses or the global lookup budget is spent."""
        auth = headers.get("authorization") or ""
        if not auth.startswith("Bearer ") or len(auth) > 512:
            return None
        token = auth[7:].strip()
        if token.startswith(USAGE_PREFIX):
            name = await self._usage(token, (headers.get(WORKSPACE_HEADER) or "").strip(), admit)
        else:
            name = signed_workspace(self.settings.signing_key, token)
        return self.tenant(name) if name else None

    async def _usage(self, token: str, name: str,
                     admit: Callable[[], bool] | None = None) -> str | None:
        if not self.usage_url or not USAGE_TOKEN_RE.match(token) or not USAGE_NAME_RE.match(name):
            return None
        key = hashlib.sha256((name + token).encode("utf-8")).hexdigest()
        now = self.clock()
        hit = self._cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        denied = self._denied.get(key)
        if denied and denied[1] > now:
            return None
        # A recent success skips the per-IP count and the global budget, so junk floods
        # cannot lock out a workspace whose 10-minute cache just expired.
        if now - self._known_good.get(key, -KNOWN_GOOD_S) >= KNOWN_GOOD_S:
            if admit is not None and not admit():
                raise AuthBusy
            while self._spent and now - self._spent[0] > CHECK_WINDOW_S:
                self._spent.popleft()
            if len(self._spent) >= CHECK_BUDGET:
                raise AuthBusy
            self._spent.append(now)
        async with self._checks:
            status = await asyncio.to_thread(_usage_status, self.usage_url, token, name,
                                             self.settings.auth_timeout_s)
        if status == 401:
            self._known_good.pop(key, None)
            _remember(self._denied, key, name, now + DENY_CACHE_S, now)
        if status != 200:
            return None
        _remember(self._cache, key, name, now + CACHE_S, now)
        self._known_good.pop(key, None)  # re-insert at the end: oldest first
        self._known_good[key] = now
        if len(self._known_good) > KNOWN_GOOD_MAX:
            self._known_good.pop(next(iter(self._known_good)))
        return name


def _remember(cache: dict[str, tuple[str, float]], key: str, name: str, until: float,
              now: float) -> None:
    if len(cache) >= CACHE_MAX:
        for k in [k for k, v in cache.items() if v[1] <= now]:
            cache.pop(k)
        if len(cache) >= CACHE_MAX:
            cache.pop(next(iter(cache)))
    cache[key] = (name, until)
