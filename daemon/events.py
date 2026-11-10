"""In-process event bus.

Anything interesting that happens in the daemon is published here as
(topic, data). Two kinds of listener exist:

* callbacks: plain functions called inline, used by the worker;
* streams:   asyncio queues feeding the dashboard's /stream (server-sent events).

Topics are dotted names. Patterns use shell wildcards: `paypal.dispute.*`.

Topics in use:
    registry.changed
    invocation.completed
    paypal.dispute.*   paypal.invoice.*   paypal.subscription.*   paypal.payment.*
    webhook.rejected
    approval.created   approval.executed   approval.rejected   approval.failed
    decision.created   decision.updated
    job.queued   job.done   job.failed
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from daemon.storage.db import utcnow

log = logging.getLogger("agentos.events")


class Event(BaseModel):
    id: int
    topic: str
    data: dict[str, Any] = {}
    ts: str


class Stream:
    """A live subscription that an async consumer can await on."""

    def __init__(self, bus: EventBus, pattern: str, loop: asyncio.AbstractEventLoop):
        self._bus = bus
        self.pattern = pattern
        self.loop = loop
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)

    def _offer(self, event: Event) -> None:
        def put() -> None:
            if not self.queue.full():
                self.queue.put_nowait(event)

        try:
            self.loop.call_soon_threadsafe(put)
        except RuntimeError:  # the loop is closed: the client is gone
            self.close()

    async def get(self, timeout: float) -> Event | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._bus._drop_stream(self)


class EventBus:
    def __init__(self, history: int = 300):
        self._lock = threading.RLock()
        self._next_id = 1
        self._history: deque[Event] = deque(maxlen=history)
        self._callbacks: list[tuple[str, Callable[[Event], None]]] = []
        self._streams: list[Stream] = []

    def publish(self, topic: str, data: dict[str, Any] | None = None) -> Event:
        with self._lock:
            event = Event(id=self._next_id, topic=topic, data=data or {}, ts=utcnow())
            self._next_id += 1
            self._history.append(event)
            callbacks = [cb for pattern, cb in self._callbacks if fnmatch.fnmatchcase(topic, pattern)]
            streams = [s for s in self._streams if fnmatch.fnmatchcase(topic, s.pattern)]
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # a broken listener must never break the publisher
                log.exception("event callback failed for %s", topic)
        for stream in streams:
            stream._offer(event)
        return event

    def subscribe(self, pattern: str, callback: Callable[[Event], None]) -> Callable[[], None]:
        entry = (pattern, callback)
        with self._lock:
            self._callbacks.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                if entry in self._callbacks:
                    self._callbacks.remove(entry)

        return unsubscribe

    def open_stream(self, pattern: str = "*") -> Stream:
        """Must be called from inside the running event loop (an async route)."""
        stream = Stream(self, pattern, asyncio.get_running_loop())
        with self._lock:
            self._streams.append(stream)
        return stream

    def _drop_stream(self, stream: Stream) -> None:
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)

    def recent(self, limit: int = 50, pattern: str = "*") -> list[Event]:
        with self._lock:
            matched = [e for e in self._history if fnmatch.fnmatchcase(e.topic, pattern)]
        return matched[-limit:]
