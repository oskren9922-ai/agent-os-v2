"""POST /webhooks/paypal: where PayPal calls us.

The route does almost nothing itself. It hands the raw bytes and headers to
the pipeline in daemon/ingest.py (verify -> store -> publish -> queue) and
answers quickly, so PayPal does not time out and retry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from daemon.api.deps import get_runtime
from daemon.runtime import Runtime

router = APIRouter(tags=["webhooks"])

MAX_BODY_BYTES = 1_000_000


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, rt: Runtime = Depends(get_runtime)) -> JSONResponse:
    raw_body = await request.body()  # the exact bytes: the signature covers these, not parsed JSON
    if len(raw_body) > MAX_BODY_BYTES:
        return JSONResponse({"status": "rejected", "reason": "body too large"}, status_code=413)
    headers = {key.lower(): value for key, value in request.headers.items()}
    # ingest() makes a blocking HTTP call to PayPal, so keep it off the event loop.
    from starlette.concurrency import run_in_threadpool

    outcome = await run_in_threadpool(rt.ingestor.ingest, headers, raw_body)
    body = {key: value for key, value in outcome.items() if key != "http_status"}
    return JSONResponse(body, status_code=outcome["http_status"])
