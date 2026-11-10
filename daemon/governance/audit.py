"""Append-only audit log.

Every governed step writes one row: who did what, to which function, with a
hash of the arguments, and how it ended. Rows are never updated or deleted;
SQLite triggers (migration 0002) refuse both.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from daemon.storage.db import Database, from_json, to_json, utcnow


def args_hash(args: Any) -> str:
    """Stable SHA-256 of the arguments, so a row proves WHAT was sent without storing it."""
    canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def write(
        self,
        actor: str,
        action: str,
        *,
        function: str | None = None,
        args: Any = None,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO audit_log (ts, actor, action, function, args_hash, status, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                utcnow(), actor, action, function,
                args_hash(args) if args is not None else None,
                status, to_json(detail or {}),
            ),
        )
        return int(cur.lastrowid)

    def list(
        self,
        *,
        function: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        status: str | None = None,
        decision_id: int | None = None,
        run_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        sql, params = "SELECT * FROM audit_log WHERE 1=1", []
        for column, value in (("function", function), ("actor", actor), ("status", status)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        if action:
            sql += " AND action LIKE ?"
            params.append(action.replace("*", "%"))
        if decision_id is not None and run_id:
            sql += " AND (json_extract(detail, '$.decision_id') = ? OR json_extract(detail, '$.run_id') = ?)"
            params += [decision_id, run_id]
        elif decision_id is not None:
            sql += " AND json_extract(detail, '$.decision_id') = ?"
            params.append(decision_id)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [max(1, min(limit, 1000)), max(0, offset)]
        rows = self.db.query(sql, params)
        for row in rows:
            row["detail"] = from_json(row["detail"], {})
        return rows

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS n FROM audit_log")["n"])
