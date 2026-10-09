"""SQLite storage: users, runs, search cache and waitlist. One file, zero ops."""

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    monthly_limit INTEGER,              -- NULL = use the default; ignored for admins
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idea TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,               -- running | done | failed
    created_at REAL NOT NULL,
    finished_at REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    title TEXT,
    score INTEGER,
    verdict TEXT,
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS runs_user_created ON runs(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS waitlist (
    email TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
"""


def _month_start_ts() -> float:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            os.chmod(path, 0o600)  # holds password hashes and API keys
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # Users -----------------------------------------------------------------

    def upsert_user(
        self, email: str, password_hash: str, *, is_admin: bool, monthly_limit: int | None = None
    ) -> None:
        self._exec(
            """INSERT INTO users (email, password_hash, is_admin, monthly_limit, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET password_hash = excluded.password_hash,
                   is_admin = excluded.is_admin, monthly_limit = excluded.monthly_limit""",
            (email, password_hash, int(is_admin), monthly_limit, time.time()),
        )

    def set_password(self, email: str, password_hash: str) -> bool:
        cur = self._exec(
            "UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, email)
        )
        return cur.rowcount > 0

    def get_user(self, email: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE email = ?", (email,))

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def list_users(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, email, is_admin, monthly_limit, created_at FROM users ORDER BY id"
        )

    def delete_user(self, email: str) -> bool:
        return self._exec("DELETE FROM users WHERE email = ?", (email,)).rowcount > 0

    # Runs ------------------------------------------------------------------

    def create_run(self, user_id: int, idea: str, mode: str) -> str:
        run_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO runs (id, user_id, idea, mode, status, created_at) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (run_id, user_id, idea, mode, time.time()),
        )
        return run_id

    def finish_run(self, run_id: str, result: dict[str, Any]) -> None:
        self._exec(
            """UPDATE runs SET status = 'done', finished_at = ?, cost_usd = ?, title = ?,
                   score = ?, verdict = ?, result_json = ? WHERE id = ?""",
            (
                time.time(),
                float(result.get("usage", {}).get("cost_usd", 0.0)),
                result.get("brief", {}).get("title"),
                result.get("score", {}).get("overall"),
                result.get("score", {}).get("verdict"),
                json.dumps(result),
                run_id,
            ),
        )

    def fail_run(self, run_id: str, error: str, cost_usd: float = 0.0) -> None:
        self._exec(
            "UPDATE runs SET status = 'failed', finished_at = ?, error = ?, cost_usd = ? "
            "WHERE id = ?",
            (time.time(), error[:2000], cost_usd, run_id),
        )

    def fail_stale_runs(self) -> int:
        """Runs left 'running' by a restart can never finish."""
        cur = self._exec(
            "UPDATE runs SET status = 'failed', error = 'interrupted by a server restart', "
            "finished_at = ? WHERE status = 'running'",
            (time.time(),),
        )
        return cur.rowcount

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row and row.get("result_json"):
            row["result"] = json.loads(row.pop("result_json"))
        elif row:
            row.pop("result_json", None)
            row["result"] = None
        return row

    def list_runs(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._all(
            """SELECT id, idea, mode, status, created_at, finished_at, cost_usd, title, score,
                      verdict FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        )

    def runs_this_month(self, user_id: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND created_at >= ?",
            (user_id, _month_start_ts()),
        )
        return int(row["n"]) if row else 0

    def cost_this_month(self) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE created_at >= ?",
            (_month_start_ts(),),
        )
        return float(row["c"]) if row else 0.0

    # Search cache ----------------------------------------------------------

    def cache_get(self, key: str, max_age_s: float) -> Any | None:
        row = self._one("SELECT response_json, created_at FROM search_cache WHERE key = ?", (key,))
        if not row or time.time() - row["created_at"] > max_age_s:
            return None
        return json.loads(row["response_json"])

    def cache_put(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO search_cache (key, response_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )

    # Settings (API keys saved from the UI) ---------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key = ?", (key,))

    # Waitlist --------------------------------------------------------------

    def add_to_waitlist(self, email: str) -> None:
        now = time.time()
        self._exec(
            """INSERT INTO waitlist (email, first_seen, last_seen) VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET attempts = attempts + 1,
                   last_seen = excluded.last_seen""",
            (email[:254], now, now),
        )

    def list_waitlist(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM waitlist ORDER BY last_seen DESC")
