"""The scheduler: queue a collections sweep every N minutes.

Disputes arrive as webhooks, but nobody sends a webhook to say "this invoice
is now a week overdue". So collections has to go and look, on a timer.

COLLECTIONS_SWEEP_MINUTES=0 turns the timer off; the sweep can still be
triggered from the dashboard or with `agentos demo invoices`.
"""
from __future__ import annotations

import logging
import threading

from worker.queue import JobQueue

log = logging.getLogger("agentos.scheduler")

SWEEP_KIND = "collections.sweep"


def enqueue_sweep(queue: JobQueue, source: str = "manual") -> int | None:
    """Queue one sweep unless one is already waiting or running."""
    if queue.has_open(SWEEP_KIND):
        return None
    return queue.enqueue(SWEEP_KIND, payload={"source": source})


class Scheduler:
    def __init__(self, queue: JobQueue, interval_minutes: int):
        self.queue = queue
        self.interval_seconds = max(0, interval_minutes) * 60
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agentos-scheduler", daemon=True)
        self._thread.start()
        log.info("collections sweep every %d minutes", self.interval_seconds // 60)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)

    def _loop(self) -> None:
        # Wait one full interval before the first sweep, so a restart does not chase clients at once.
        while not self._stop.wait(self.interval_seconds):
            try:
                enqueue_sweep(self.queue, source="scheduler")
            except Exception:
                log.exception("could not queue the collections sweep")
