"""The webhook pipeline: verify -> store -> publish -> queue.

    1. paypal_adapter/webhooks.py verifies the signature with PayPal.
       A fake is rejected and leaves an audit row.
    2. The event is stored in `events`. PayPal's event id is UNIQUE, so a retry
       of the same event is recognised and dropped.
    3. It is published on the event bus (dashboard live feed).
    4. A job is queued for the worker, which hands it to the agents.

The HTTP route in daemon/api/webhooks.py is a thin wrapper around `ingest`.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from daemon.config import Settings
from daemon.events import EventBus
from daemon.governance.audit import AuditLog
from daemon.storage.stores import EventStore

log = logging.getLogger("agentos.ingest")


class Ingestor:
    def __init__(self, settings: Settings, events: EventStore, bus: EventBus, audit: AuditLog,
                 queue=None, client=None):
        self.settings = settings
        self.events = events
        self.bus = bus
        self.audit = audit
        self.queue = queue    # worker.queue.JobQueue
        self.client = client  # paypal_adapter.client.PayPalClient

    def ingest(self, headers: Mapping[str, str], raw_body: bytes, *,
               trusted_simulation: bool = False) -> dict[str, Any]:
        """Returns {"status": accepted|duplicate|rejected, "http_status": int, ...}."""
        from paypal_adapter.webhooks import parse_event, verify_signature

        # -- 1. verify ---------------------------------------------------
        if trusted_simulation:
            # Only the sandbox replay helper uses this, and never in live.
            if not self.settings.is_sandbox_like:
                return self._reject("simulated events are not allowed when PAYPAL_ENV=live", raw_body)
            verified = False
        else:
            if self.client is None:
                return self._reject("PayPal is not configured, so the signature cannot be verified", raw_body)
            result = verify_signature(self.client, self.settings.effective_webhook_id, headers, raw_body)
            if not result.ok:
                return self._reject(result.reason, raw_body)
            verified = True

        # -- 2. parse and store -----------------------------------------
        try:
            event = parse_event(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            return self._reject(f"unreadable event: {exc}", raw_body, http_status=400)

        event_row_id, inserted = self.events.insert(
            paypal_event_id=event.event_id, event_type=event.event_type, topic=event.topic,
            resource_type=event.resource_type, resource_id=event.resource_id, summary=event.summary,
            payload=event.payload, verified=verified, simulated=trusted_simulation,
        )
        if not inserted:
            self.audit.write("paypal", "webhook.duplicate", status="duplicate",
                             detail={"paypal_event_id": event.event_id, "event_type": event.event_type})
            return {"status": "duplicate", "http_status": 200, "event_id": event_row_id}

        self.audit.write("paypal", "webhook.accepted", status="accepted",
                         detail={"event_id": event_row_id, "paypal_event_id": event.event_id,
                                 "event_type": event.event_type, "resource_id": event.resource_id,
                                 "verified": verified, "simulated": trusted_simulation})

        # -- 3. publish, 4. queue ----------------------------------------
        self.bus.publish(event.topic, {
            "event_id": event_row_id, "event_type": event.event_type, "resource_id": event.resource_id,
            "summary": event.summary, "verified": verified, "simulated": trusted_simulation,
        })
        job_id = None
        if self.queue is not None:
            job_id = self.queue.enqueue("event", event_id=event_row_id,
                                        payload={"topic": event.topic, "event_type": event.event_type})
        return {"status": "accepted", "http_status": 200, "event_id": event_row_id, "job_id": job_id,
                "topic": event.topic}

    def _reject(self, reason: str, raw_body: bytes, http_status: int = 401) -> dict[str, Any]:
        log.warning("webhook rejected: %s", reason)
        self.audit.write("unknown", "webhook.rejected", args={"body_bytes": len(raw_body)},
                         status="rejected", detail={"reason": reason})
        self.bus.publish("webhook.rejected", {"reason": reason})
        return {"status": "rejected", "http_status": http_status, "reason": reason}
