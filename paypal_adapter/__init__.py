"""The PayPal adapter: THE ONE PLACE PayPal attaches to Agent OS.

No other package talks to PayPal directly.

    auth.py        OAuth2 client-credentials token, cached and auto-refreshed
    client.py      HTTP client: base URL, idempotency, retries, clean errors
    specs/         official PayPal OpenAPI JSON, downloaded and pinned
    allowlist.py   which operations get registered, and each one's risk tier
    compiler.py    OpenAPI operation -> typed function
    overrides/     hand-written functions where generation is not enough
    register.py    atomic snapshot-replace into the registry (connect time)
    dispatch.py    registry call -> real HTTP request (ask time)
    webhooks.py    signature verification and event parsing
    sandbox/       demo helpers, tier sandbox_only
    mock_server.py a fake PayPal used when PAYPAL_ENV=mock and in tests
"""

ADAPTER_NAME = "paypal"
