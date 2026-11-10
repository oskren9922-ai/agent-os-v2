"""Small table stores: events, decisions and client profiles."""
from __future__ import annotations

from typing import Any

from daemon.events import EventBus
from daemon.storage.db import Database, from_json, to_json, utcnow


class EventStore:
    """Verified webhook events. The UNIQUE PayPal event id drops duplicates."""

    def __init__(self, db: Database):
        self.db = db

    def insert(self, *, paypal_event_id: str, event_type: str, topic: str, resource_type: str | None,
               resource_id: str | None, summary: str | None, payload: dict, verified: bool,
               simulated: bool) -> tuple[int, bool]:
        """Returns (row id, inserted). inserted is False for a duplicate."""
        with self.db.transaction():
            existing = self.db.query_one("SELECT id FROM events WHERE paypal_event_id = ?", (paypal_event_id,))
            if existing:
                return int(existing["id"]), False
            cur = self.db.execute(
                "INSERT INTO events (paypal_event_id, event_type, topic, resource_type, resource_id, "
                "summary, payload, verified, simulated, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (paypal_event_id, event_type, topic, resource_type, resource_id, summary,
                 to_json(payload), int(verified), int(simulated), utcnow()),
            )
            return int(cur.lastrowid), True

    def get(self, event_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        if row:
            row["payload"] = from_json(row["payload"], {})
        return row

    def list(self, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            "SELECT id, paypal_event_id, event_type, topic, resource_type, resource_id, summary, "
            "verified, simulated, received_at FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return rows


_DECISION_JSON = ("decision", "tool_calls", "result")
_DECISION_LIST_COLUMNS = (
    "id, agent, event_id, subject_type, subject_id, client_id, client_name, input_summary, action, "
    "summary, confidence, model, status, proposed_function, pending_action_id, amount, currency, "
    "created_at, updated_at"
)


class DecisionStore:
    def __init__(self, db: Database, bus: EventBus | None = None):
        self.db = db
        self.bus = bus

    def create(self, **fields: Any) -> int:
        now = utcnow()
        fields = {**fields, "created_at": now, "updated_at": now}
        for key in _DECISION_JSON:
            if key in fields and not isinstance(fields[key], str):
                fields[key] = to_json(fields[key])
        columns = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = self.db.execute(f"INSERT INTO decisions ({columns}) VALUES ({marks})", tuple(fields.values()))
        decision_id = int(cur.lastrowid)
        if self.bus:
            self.bus.publish("decision.created", {
                "id": decision_id, "agent": fields.get("agent"), "action": fields.get("action"),
                "subject_type": fields.get("subject_type"), "subject_id": fields.get("subject_id"),
                "summary": fields.get("summary"),
            })
        return decision_id

    def update(self, decision_id: int, **fields: Any) -> None:
        fields = {**fields, "updated_at": utcnow()}
        for key in _DECISION_JSON:
            if key in fields and not isinstance(fields[key], str) and fields[key] is not None:
                fields[key] = to_json(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE decisions SET {sets} WHERE id = ?", (*fields.values(), decision_id))
        if self.bus:
            self.bus.publish("decision.updated", {"id": decision_id, "status": fields.get("status")})

    def get(self, decision_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        if not row:
            return None
        for key in _DECISION_JSON:
            row[key] = from_json(row[key], [] if key == "tool_calls" else None)
        return row

    def list(self, *, limit: int = 100, agent: str | None = None, status: str | None = None,
             client_id: str | None = None) -> list[dict]:
        sql, params = f"SELECT {_DECISION_LIST_COLUMNS} FROM decisions WHERE 1=1", []
        for column, value in (("agent", agent), ("status", status), ("client_id", client_id)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return self.db.query(sql, params)

    def recent_for_subject(self, subject_type: str, subject_id: str, since_iso: str) -> dict | None:
        return self.db.query_one(
            "SELECT id, action, status, created_at FROM decisions WHERE subject_type = ? AND subject_id = ? "
            "AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (subject_type, subject_id, since_iso),
        )


class ProfileStore:
    """Per-client payment profile: how reliably this payer has paid before."""

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, profile: dict) -> dict:
        profile = {**profile, "last_updated": utcnow()}
        self.db.execute(
            "INSERT INTO client_profiles (client_id, name, invoices_count, paid_count, avg_days_late, "
            "on_time_share, outstanding_amount, currency, last_updated) "
            "VALUES (:client_id, :name, :invoices_count, :paid_count, :avg_days_late, :on_time_share, "
            ":outstanding_amount, :currency, :last_updated) "
            "ON CONFLICT(client_id) DO UPDATE SET name=excluded.name, "
            "invoices_count=excluded.invoices_count, paid_count=excluded.paid_count, "
            "avg_days_late=excluded.avg_days_late, on_time_share=excluded.on_time_share, "
            "outstanding_amount=excluded.outstanding_amount, currency=excluded.currency, "
            "last_updated=excluded.last_updated",
            profile,
        )
        return profile

    def get(self, client_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM client_profiles WHERE client_id = ?", (client_id,))

    def list(self) -> list[dict]:
        return self.db.query("SELECT * FROM client_profiles ORDER BY name, client_id")
