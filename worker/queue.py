"""A small job queue stored in SQLite.

A webhook must be answered in a moment, but an agent may think for many
seconds. So the webhook only writes a job row; the runner picks it up on its
own thread. Because jobs live in the database, nothing is lost if the daemon
restarts between "event received" and "agent finished".

Job status: queued -> running -> done, or back to queued for a retry, or failed.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from daemon.events import EventBus
from daemon.storage.db import Database, from_json, to_json, utcnow

RETRY_DELAYS = (5, 30, 120)  # seconds before attempt 2, 3, 4


def _iso_in(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class JobQueue:
    def __init__(self, db: Database, bus: EventBus | None = None):
        self.db = db
        self.bus = bus
        self.wakeup = threading.Event()  # set whenever a job is added, so the runner need not poll hard

    def enqueue(self, kind: str, *, event_id: int | None = None, payload: dict | None = None,
                max_attempts: int = 3, delay_seconds: float = 0) -> int:
        now = utcnow()
        cur = self.db.execute(
            "INSERT INTO jobs (kind, event_id, payload, status, attempts, max_attempts, run_after, "
            "created_at, updated_at) VALUES (?,?,?,'queued',0,?,?,?,?)",
            (kind, event_id, to_json(payload or {}), max_attempts,
             _iso_in(delay_seconds) if delay_seconds else now, now, now),
        )
        job_id = int(cur.lastrowid)
        if self.bus:
            self.bus.publish("job.queued", {"job_id": job_id, "kind": kind, "event_id": event_id})
        self.wakeup.set()
        return job_id

    def claim(self) -> dict | None:
        """Atomically take the oldest job that is ready. Returns None when there is none."""
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY id LIMIT 1",
                (utcnow(),),
            )
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (utcnow(), row["id"]),
            )
        row["status"] = "running"
        row["attempts"] += 1
        row["payload"] = from_json(row["payload"], {})
        return row

    def complete(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?",
                        (utcnow(), job_id))
        if self.bus:
            self.bus.publish("job.done", {"job_id": job_id})

    def fail(self, job: dict, error: str) -> str:
        """Retry with a growing delay until max_attempts, then give up. Returns the new status."""
        if job["attempts"] < job["max_attempts"]:
            delay = RETRY_DELAYS[min(job["attempts"] - 1, len(RETRY_DELAYS) - 1)]
            self.db.execute(
                "UPDATE jobs SET status='queued', last_error=?, run_after=?, updated_at=? WHERE id=?",
                (error[:2000], _iso_in(delay), utcnow(), job["id"]),
            )
            status = "queued"
        else:
            self.db.execute("UPDATE jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                            (error[:2000], utcnow(), job["id"]))
            status = "failed"
        if self.bus:
            self.bus.publish("job.failed", {"job_id": job["id"], "kind": job["kind"], "error": error[:300],
                                            "will_retry": status == "queued"})
        return status

    def recover_stuck(self) -> int:
        """On startup: jobs left 'running' by a crash go back to the queue."""
        cur = self.db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (utcnow(),))
        if cur.rowcount:
            self.wakeup.set()
        return cur.rowcount

    def has_open(self, kind: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 AS x FROM jobs WHERE kind = ? AND status IN ('queued','running') LIMIT 1", (kind,))
        return row is not None

    def counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = from_json(row["payload"], {})
        return rows
