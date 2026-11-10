"""GET /stream: server-sent events for the dashboard.

The browser opens one long-lived connection and the daemon pushes a line of
JSON whenever something happens (new decision, new approval, action executed).
That is how the dashboard updates without polling.
"""
from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from daemon.api.deps import get_runtime
from daemon.runtime import Runtime

router = APIRouter(tags=["stream"])

KEEPALIVE_SECONDS = 15.0
POLL_SECONDS = 1.0
# Set when the server is asked to stop, so open streams end at once instead of
# holding the shutdown until a timeout. `agentos serve` sets it on Ctrl+C.
shutdown_event = threading.Event()
# Internal chatter the dashboard does not need.
HIDDEN_TOPICS = ("job.queued", "job.done", "invocation.completed")


@router.get("/stream")
async def stream(request: Request, rt: Runtime = Depends(get_runtime)) -> StreamingResponse:
    subscription = rt.bus.open_stream("*")

    async def generate() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'topic': 'stream.connected', 'data': {}})}\n\n"
            idle = 0.0
            while not shutdown_event.is_set():
                if await request.is_disconnected():
                    break
                event = await subscription.get(POLL_SECONDS)
                if event is None:
                    idle += POLL_SECONDS
                    if idle >= KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield ": keepalive\n\n"
                    continue
                idle = 0.0
                if event.topic in HIDDEN_TOPICS:
                    continue
                yield f"id: {event.id}\ndata: {json.dumps(event.model_dump(), default=str)}\n\n"
        finally:
            subscription.close()

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
