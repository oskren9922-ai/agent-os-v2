"""OAuth2 for PayPal.

PayPal's REST API uses the client-credentials grant: exchange the app's
client id and secret for a short-lived access token, then send that token as
`Authorization: Bearer ...`. The token is cached and refreshed shortly before
it expires, so one process makes one token call every few hours, not one per
request.
"""
from __future__ import annotations

import threading
import time

import httpx

from daemon.invocation import AdapterError

TOKEN_PATH = "/v1/oauth2/token"


class PayPalAuthError(AdapterError):
    """PayPal refused the credentials, or the token endpoint failed."""


class TokenProvider:
    def __init__(self, http: httpx.Client, client_id: str, client_secret: str, *, skew_seconds: int = 60):
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._skew = skew_seconds
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.fetch_count = 0  # how many times we really called PayPal (handy in tests)

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - self._skew:
                return self._token
            return self._fetch()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> str:
        if not self._client_id or not self._client_secret:
            raise PayPalAuthError(
                "PayPal credentials are missing: set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env",
                code="CREDENTIALS_MISSING",
            )
        try:
            response = self._http.post(
                TOKEN_PATH,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise PayPalAuthError(f"could not reach PayPal for a token: {exc}", code="NETWORK") from exc
        self.fetch_count += 1
        if response.status_code != 200:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PayPalAuthError(
                body.get("error_description") or f"token request failed with HTTP {response.status_code}",
                code=body.get("error") or "AUTH_FAILED",
                status=response.status_code,
            )
        data = response.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + float(data.get("expires_in", 300))
        return self._token
