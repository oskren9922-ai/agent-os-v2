"""The PayPal HTTP client.

One method, `request()`, used by everything in this adapter. It adds:

  * the right base URL (sandbox or live);
  * the OAuth bearer token (auth.py), refreshed once on a 401;
  * `PayPal-Request-Id` on POSTs, so a repeated request cannot repeat the action;
  * retries with backoff on 429/5xx/network errors, but ONLY when repeating is
    safe (a GET/PUT/PATCH/DELETE, or a POST that carries an idempotency key);
  * a clean `PayPalError` instead of raw HTTP failures.

In mock mode the same client runs against an in-process fake PayPal
(mock_server.py) by swapping the transport. No other code changes.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from daemon.config import Settings
from daemon.invocation import AdapterError
from paypal_adapter.auth import TokenProvider

RETRY_STATUSES = {429, 500, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD", "PUT", "PATCH", "DELETE"}


class PayPalError(AdapterError):
    """PayPal answered with an error. `code` is PayPal's error name."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: Any = None, debug_id: str | None = None):
        super().__init__(message, code=code, status=status, details=details)
        self.debug_id = debug_id

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["debug_id"] = self.debug_id
        return data


class PayPalClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, *,
                 transport: httpx.BaseTransport | None = None, timeout: float = 20.0,
                 max_retries: int = 3, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)
        self.tokens = TokenProvider(self._http, client_id, client_secret)
        self.max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                files: Any = None, content: bytes | str | None = None, headers: dict | None = None,
                idempotency_key: str | None = None) -> Any:
        method = method.upper()
        send_headers = {"Accept": "application/json"}
        if json is not None or content is not None:
            send_headers["Content-Type"] = "application/json"
        if idempotency_key and method == "POST":
            send_headers["PayPal-Request-Id"] = idempotency_key
        send_headers.update(headers or {})
        if params:
            params = {k: _param(v) for k, v in params.items() if v is not None}

        may_retry = method in SAFE_METHODS or bool(idempotency_key)
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            send_headers["Authorization"] = f"Bearer {self.tokens.token()}"
            try:
                response = self._http.request(method, path, params=params, json=json, files=files,
                                              content=content, headers=send_headers)
            except httpx.TransportError as exc:
                if may_retry and attempt <= self.max_retries:
                    self._sleep(_backoff(attempt))
                    continue
                raise PayPalError(f"could not reach PayPal: {exc}", code="NETWORK") from exc

            if response.status_code == 401 and not refreshed:
                refreshed = True  # the token may have been revoked early: get a new one, once
                self.tokens.invalidate()
                continue
            if response.status_code in RETRY_STATUSES and may_retry and attempt <= self.max_retries:
                self._sleep(_retry_after(response) or _backoff(attempt))
                continue
            if response.status_code >= 400:
                raise _to_error(response)
            return _parse(response)


def _param(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _backoff(attempt: int) -> float:
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers.get("Retry-After", "")), 10.0)
    except ValueError:
        return None


def _parse(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return {"ok": True, "status_code": response.status_code}
    try:
        return response.json()
    except ValueError:
        return {"ok": True, "status_code": response.status_code, "text": response.text[:2000]}


def _to_error(response: httpx.Response) -> PayPalError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = body.get("name") or body.get("error") or f"HTTP_{response.status_code}"
    message = body.get("message") or body.get("error_description") or response.text[:300] or name
    details = body.get("details")
    if isinstance(details, list) and details:
        issues = "; ".join(
            str(d.get("issue") or d.get("description") or d) for d in details[:3] if isinstance(d, dict)
        )
        if issues:
            message = f"{message} ({issues})"
    return PayPalError(message, code=name, status=response.status_code, details=details,
                       debug_id=body.get("debug_id") or response.headers.get("Paypal-Debug-Id"))


def build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> PayPalClient:
    """Create the client for the configured environment."""
    if settings.paypal_env == "mock" and transport is None:
        from paypal_adapter.mock_server import MockPayPal

        state_file = settings.db_path.parent / f"{settings.db_path.stem}.mock-paypal.json"
        transport = MockPayPal(state_file=state_file).transport()
    if settings.paypal_env == "mock":
        return PayPalClient(settings.paypal_base_url, "mock-client-id", "mock-client-secret",
                            transport=transport, sleep=lambda _s: None)
    return PayPalClient(settings.paypal_base_url, settings.paypal_client_id,
                        settings.paypal_client_secret, transport=transport)
