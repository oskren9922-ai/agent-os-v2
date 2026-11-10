"""One SQLite connection, shared safely.

The daemon serves HTTP requests on a thread pool and runs the agent worker on
its own thread, so every statement goes through one re-entrant lock. WAL mode
lets the CLI (a separate process) read and write the same file meanwhile.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string, e.g. 2026-11-10T16:46:30.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def from_json(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        # isolation_level=None: we issue BEGIN/COMMIT ourselves in transaction().
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # -- statements ---------------------------------------------------------
    def execute(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | list | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing block. Nested use joins the outer transaction."""
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.execute("COMMIT")

    # -- schema -------------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply every migrations/NNNN_*.sql file that has not run yet."""
        applied_now: list[str] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {row["name"] for row in self._conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                script = path.read_text(encoding="utf-8")
                stamp = f"INSERT INTO schema_migrations (name, applied_at) VALUES ('{path.name}', '{utcnow()}');"
                try:
                    self._conn.executescript(f"BEGIN;\n{script}\n{stamp}\nCOMMIT;")
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
                applied_now.append(path.name)
        return applied_now

    def close(self) -> None:
        with self._lock:
            self._conn.close()
