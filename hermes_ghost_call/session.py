"""SQLite state shared by the plugin process and the bridge process.

Tables: ``sessions`` (one ghost call), ``tokens`` (hashed one-time link and media tokens),
``turns`` (text transcript, only when ``store_transcripts``), ``hints``, ``commands`` (plugin to
bridge queue: speak, intervene, hangup) and ``events`` (a small audit log).

No audio is ever written here. The summary is write-once: ``set_summary_once`` only fills an
empty column, and ``claim_summary_post`` lets exactly one poster deliver it.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

PLUGIN_NAME = "hermes-ghost-call"
DB_ENV = "HERMES_GHOST_DB"
DB_FILE = "ghost.db"

CREATED = "created"  # links handed out, nobody joined yet
LIVE = "live"  # the user's page (web) or leg (Twilio) is connected
ENDED = "ended"
ACTIVE_STATES = (CREATED, LIVE)

ROLES = ("you", "them")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  callee_label TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  origin_session TEXT NOT NULL DEFAULT '',
  config TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  live_at REAL,
  ended_at REAL,
  end_reason TEXT,
  intervene_allowed INTEGER NOT NULL DEFAULT 0,
  intervene_by TEXT,
  disclosed INTEGER NOT NULL DEFAULT 0,
  hangup_requested REAL,
  summary TEXT,
  summary_posted INTEGER NOT NULL DEFAULT 0,
  provider TEXT NOT NULL DEFAULT '{}',
  workspace TEXT NOT NULL DEFAULT '',
  client_ref TEXT NOT NULL DEFAULT '',
  consent_at REAL,
  usage TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS tokens (
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL,
  PRIMARY KEY (session_id, role, kind, hash)
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  speaker TEXT NOT NULL,
  text TEXT NOT NULL,
  t REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  n INTEGER NOT NULL,
  t REAL NOT NULL,
  text TEXT NOT NULL,
  created_at REAL NOT NULL,
  posted INTEGER NOT NULL DEFAULT 0,
  spoken_at REAL,
  UNIQUE (session_id, n)
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  taken_at REAL,
  done_at REAL,
  result TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hints_by_session ON hints (session_id, n);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns (session_id, id);
CREATE INDEX IF NOT EXISTS commands_open ON commands (session_id, taken_at);
"""

COMMAND_KINDS = ("speak", "intervene", "hangup")
# Columns added after the first release; older ghost.db files get them on open.
_ADDED_COLUMNS = {
    "workspace": "TEXT NOT NULL DEFAULT ''",  # shared bridge tenant; '' for a co-located bridge
    "client_ref": "TEXT NOT NULL DEFAULT ''",  # the plugin's own session id (shared bridge)
    "consent_at": "REAL",  # transcription consent: page confirm or the spoken phone disclosure
    "usage": "TEXT NOT NULL DEFAULT '{}'",  # {"stt_seconds", "tts_chars"}
}


def default_db_path() -> Path:
    """``HERMES_GHOST_DB`` wins; else Hermes' plugin data dir; else ``$HERMES_HOME`` by hand.

    The plugin and the bridge must resolve the same file, so both use this function.
    """
    explicit = (os.environ.get(DB_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir(PLUGIN_NAME)) / DB_FILE
    except Exception:  # noqa: BLE001 - no Hermes on this interpreter
        home = (os.environ.get("HERMES_HOME") or "").strip()
        base = Path(home) if home else Path.home() / ".hermes"
        return base / "plugin-data" / PLUGIN_NAME / DB_FILE


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_id() -> str:
    return "g" + secrets.token_hex(3)


class Store:
    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] | None = None
                 ) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        have = {r[1] for r in self._db.execute("PRAGMA table_info(sessions)").fetchall()}
        if have:
            for name, decl in _ADDED_COLUMNS.items():
                if name not in have:
                    self._db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
        self._db.executescript(SCHEMA)
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_by_ref ON sessions"
                         " (workspace, client_ref)")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- helpers --
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._exec(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, params).fetchall()]

    @staticmethod
    def _session_row(row: dict | None) -> dict | None:
        if row is None:
            return None
        row["config"] = json.loads(row.get("config") or "{}")
        row["provider"] = json.loads(row.get("provider") or "{}")
        row["usage"] = json.loads(row.get("usage") or "{}")
        row["summary"] = json.loads(row["summary"]) if row.get("summary") else None
        row["intervene_allowed"] = bool(row.get("intervene_allowed"))
        row["disclosed"] = bool(row.get("disclosed"))
        return row

    # -- sessions --
    def create_session(self, *, goal: str, callee_label: str, mode: str, config: dict,
                       origin_session: str = "", provider: dict | None = None,
                       session_id: str | None = None, workspace: str = "",
                       client_ref: str = "") -> dict:
        sid = session_id or new_session_id()
        self._exec(
            "INSERT INTO sessions (id, goal, callee_label, mode, state, origin_session, config,"
            " created_at, provider, workspace, client_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, goal, callee_label, mode, CREATED, origin_session,
             json.dumps(config, sort_keys=True), self.clock(), json.dumps(provider or {}),
             workspace, client_ref))
        self.add_event(sid, "created", {"mode": mode})
        return self.get_session(sid) or {}

    def get_session(self, sid: str) -> dict | None:
        return self._session_row(self._one("SELECT * FROM sessions WHERE id=?", (sid,)))

    def tenant_session(self, workspace: str, client_ref: str) -> dict | None:
        """A shared-bridge session, only ever looked up inside its own workspace."""
        if not workspace or not client_ref:
            return None
        return self._session_row(self._one(
            "SELECT * FROM sessions WHERE workspace=? AND client_ref=?", (workspace, client_ref)))

    def active_sessions(self, workspace: str | None = None) -> list[dict]:
        if workspace is None:
            rows = self._all("SELECT * FROM sessions WHERE state IN (?,?) ORDER BY created_at",
                             ACTIVE_STATES)
        else:
            rows = self._all("SELECT * FROM sessions WHERE state IN (?,?) AND workspace=?"
                             " ORDER BY created_at", (*ACTIVE_STATES, workspace))
        return [r for r in (self._session_row(x) for x in rows) if r]

    def latest_session(self) -> dict | None:
        return self._session_row(self._one(
            "SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC LIMIT 1"))

    def recent_sessions(self, n: int) -> list[dict]:
        rows = self._all("SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC LIMIT ?",
                         (max(1, min(int(n), 50)),))
        return [r for r in (self._session_row(x) for x in rows) if r]

    def sessions_since(self, since: float, workspace: str | None = None) -> int:
        if workspace is None:
            row = self._one("SELECT COUNT(*) AS n FROM sessions WHERE created_at >= ?", (since,))
        else:
            row = self._one("SELECT COUNT(*) AS n FROM sessions WHERE created_at >= ? AND"
                            " workspace=?", (since, workspace))
        return int((row or {}).get("n") or 0)

    def mark_live(self, sid: str) -> bool:
        cur = self._exec("UPDATE sessions SET state=?, live_at=? WHERE id=? AND state=?",
                         (LIVE, self.clock(), sid, CREATED))
        if cur.rowcount:
            self.add_event(sid, "live", {})
        return bool(cur.rowcount)

    def end_once(self, sid: str, reason: str) -> bool:
        """Move to ``ended`` exactly once; True for the caller that did it."""
        cur = self._exec(
            "UPDATE sessions SET state=?, ended_at=?, end_reason=? WHERE id=? AND state IN (?,?)",
            (ENDED, self.clock(), reason, sid, *ACTIVE_STATES))
        if cur.rowcount:
            self.add_event(sid, "ended", {"reason": reason})
        return bool(cur.rowcount)

    def request_hangup(self, sid: str) -> bool:
        cur = self._exec(
            "UPDATE sessions SET hangup_requested=? WHERE id=? AND state IN (?,?)"
            " AND hangup_requested IS NULL", (self.clock(), sid, *ACTIVE_STATES))
        return bool(cur.rowcount)

    def set_intervene(self, sid: str, allowed: bool, by: str = "") -> bool:
        cur = self._exec("UPDATE sessions SET intervene_allowed=?, intervene_by=? WHERE id=?",
                         (1 if allowed else 0, by or None, sid))
        if cur.rowcount:
            self.add_event(sid, "intervene_" + ("allow" if allowed else "deny"), {"by": by})
        return bool(cur.rowcount)

    def mark_disclosed(self, sid: str) -> bool:
        """True the first time only: the caller must speak the AI disclosure."""
        cur = self._exec("UPDATE sessions SET disclosed=1 WHERE id=? AND disclosed=0", (sid,))
        return bool(cur.rowcount)

    def mark_consent(self, sid: str) -> bool:
        """True the first time only. Nothing is transcribed for ``sid`` before this."""
        cur = self._exec("UPDATE sessions SET consent_at=? WHERE id=? AND consent_at IS NULL",
                         (self.clock(), sid))
        if cur.rowcount:
            self.add_event(sid, "consent", {})
        return bool(cur.rowcount)

    def set_usage(self, sid: str, usage: dict) -> None:
        self._exec("UPDATE sessions SET usage=? WHERE id=?", (json.dumps(usage), sid))

    def set_provider(self, sid: str, provider: dict) -> None:
        cur = self.get_session(sid)
        merged = {**((cur or {}).get("provider") or {}), **provider}
        self._exec("UPDATE sessions SET provider=? WHERE id=?", (json.dumps(merged), sid))

    def set_summary_once(self, sid: str, summary: dict) -> bool:
        cur = self._exec("UPDATE sessions SET summary=? WHERE id=? AND summary IS NULL",
                         (json.dumps(summary, ensure_ascii=False, sort_keys=True), sid))
        return bool(cur.rowcount)

    def claim_summary_post(self, sid: str) -> bool:
        cur = self._exec("UPDATE sessions SET summary_posted=1 WHERE id=? AND summary_posted=0"
                         " AND summary IS NOT NULL", (sid,))
        return bool(cur.rowcount)

    def release_summary_post(self, sid: str) -> None:
        self._exec("UPDATE sessions SET summary_posted=0 WHERE id=?", (sid,))

    # -- tokens --
    def mint_token(self, sid: str, role: str, kind: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(24)
        now = self.clock()
        self._exec("INSERT INTO tokens (session_id, role, kind, hash, created_at, expires_at)"
                   " VALUES (?,?,?,?,?,?)", (sid, role, kind, hash_token(token), now, now + ttl_s))
        return token

    def peek_token(self, sid: str, role: str, kind: str, token: str) -> bool:
        if not token:
            return False
        row = self._one("SELECT used_at, expires_at FROM tokens WHERE session_id=? AND role=?"
                        " AND kind=? AND hash=?", (sid, role, kind, hash_token(token)))
        return bool(row and row["used_at"] is None and row["expires_at"] > self.clock())

    def consume_token(self, sid: str, role: str, kind: str, token: str) -> bool:
        if not token:
            return False
        cur = self._exec("UPDATE tokens SET used_at=? WHERE session_id=? AND role=? AND kind=?"
                         " AND hash=? AND used_at IS NULL AND expires_at > ?",
                         (self.clock(), sid, role, kind, hash_token(token), self.clock()))
        return bool(cur.rowcount)

    def token_used(self, sid: str, role: str, kind: str) -> bool:
        row = self._one("SELECT COUNT(*) AS n FROM tokens WHERE session_id=? AND role=? AND"
                        " kind=? AND used_at IS NOT NULL", (sid, role, kind))
        return bool(row and row["n"])

    # -- turns --
    def add_turn(self, sid: str, speaker: str, text: str, t: float) -> None:
        self._exec("INSERT INTO turns (session_id, speaker, text, t, created_at) VALUES"
                   " (?,?,?,?,?)", (sid, speaker, text, t, self.clock()))

    def turns(self, sid: str, limit: int | None = None) -> list[dict]:
        if limit:
            rows = self._all("SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?",
                             (sid, int(limit)))
            return list(reversed(rows))
        return self._all("SELECT * FROM turns WHERE session_id=? ORDER BY id", (sid,))

    # -- hints --
    def add_hint(self, sid: str, t: float, text: str) -> dict:
        with self._lock:
            row = self._one("SELECT COALESCE(MAX(n), 0) AS n FROM hints WHERE session_id=?",
                            (sid,))
            n = int((row or {}).get("n") or 0) + 1
            self._exec("INSERT INTO hints (session_id, n, t, text, created_at) VALUES"
                       " (?,?,?,?,?)", (sid, n, t, text, self.clock()))
        return self.get_hint(sid, n) or {}

    def put_hint(self, sid: str, n: int, t: float, text: str) -> bool:
        """Mirror a hint numbered elsewhere (the shared bridge). Idempotent by ``n``."""
        cur = self._exec("INSERT OR IGNORE INTO hints (session_id, n, t, text, created_at)"
                         " VALUES (?,?,?,?,?)", (sid, int(n), float(t), text, self.clock()))
        return bool(cur.rowcount)

    def get_hint(self, sid: str, n: int) -> dict | None:
        return self._one("SELECT * FROM hints WHERE session_id=? AND n=?", (sid, int(n)))

    def hints(self, sid: str, limit: int | None = None) -> list[dict]:
        if limit:
            rows = self._all("SELECT * FROM hints WHERE session_id=? ORDER BY n DESC LIMIT ?",
                             (sid, int(limit)))
            return list(reversed(rows))
        return self._all("SELECT * FROM hints WHERE session_id=? ORDER BY n", (sid,))

    def hint_count(self, sid: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM hints WHERE session_id=?", (sid,))
        return int((row or {}).get("n") or 0)

    def unposted_hints(self, sid: str) -> list[dict]:
        return self._all("SELECT * FROM hints WHERE session_id=? AND posted=0 ORDER BY n", (sid,))

    def mark_hints_posted(self, sid: str, upto_n: int) -> None:
        self._exec("UPDATE hints SET posted=1 WHERE session_id=? AND n<=?", (sid, int(upto_n)))

    def mark_spoken(self, sid: str, n: int) -> None:
        self._exec("UPDATE hints SET spoken_at=? WHERE session_id=? AND n=?",
                   (self.clock(), sid, int(n)))

    # -- commands (plugin -> bridge) --
    def enqueue(self, sid: str, kind: str, payload: dict | None = None) -> int:
        if kind not in COMMAND_KINDS:
            raise ValueError(f"unknown command kind {kind!r}")
        cur = self._exec("INSERT INTO commands (session_id, kind, payload, created_at) VALUES"
                         " (?,?,?,?)", (sid, kind, json.dumps(payload or {}), self.clock()))
        return int(cur.lastrowid or 0)

    def take_commands(self, sid: str) -> list[dict]:
        """Claim every untaken command for ``sid`` (one taker wins each row)."""
        out = []
        for row in self._all("SELECT * FROM commands WHERE session_id=? AND taken_at IS NULL"
                             " ORDER BY id", (sid,)):
            cur = self._exec("UPDATE commands SET taken_at=? WHERE id=? AND taken_at IS NULL",
                             (self.clock(), row["id"]))
            if cur.rowcount:
                row["payload"] = json.loads(row.get("payload") or "{}")
                out.append(row)
        return out

    def finish_command(self, cid: int, result: str) -> None:
        self._exec("UPDATE commands SET done_at=?, result=? WHERE id=?",
                   (self.clock(), result[:200], cid))

    def get_command(self, cid: int) -> dict | None:
        return self._one("SELECT * FROM commands WHERE id=?", (cid,))

    # -- events --
    def add_event(self, sid: str, kind: str, data: dict | None = None) -> None:
        self._exec("INSERT INTO events (session_id, kind, data, created_at) VALUES (?,?,?,?)",
                   (sid, kind, json.dumps(data or {}, ensure_ascii=False), self.clock()))

    def events(self, sid: str) -> list[dict]:
        return self._all("SELECT * FROM events WHERE session_id=? ORDER BY id", (sid,))


def elapsed(session: dict, now: float) -> float:
    start = session.get("live_at") or session.get("created_at") or now
    end = session.get("ended_at") or now
    return max(0.0, float(end) - float(start))

