"""Limits: the auto-approve cap and per-client rate limits."""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from daemon.config import Settings
from daemon.storage.db import Database

WINDOW_SECONDS = 3600


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def dig(data: Any, path: str | None) -> Any:
    """Follow a dotted path through nested dicts: dig(args, 'body.amount.value')."""
    if not path:
        return None
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class Limits:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    # -- auto-approve cap -----------------------------------------------------
    @property
    def cap(self) -> Decimal:
        return to_decimal(self.settings.auto_approve_cap) or Decimal("0")

    def within_cap(self, amount: Decimal | None, currency: str | None) -> bool:
        """True only when we KNOW the amount, in the cap's currency, at or under the cap."""
        if amount is None or self.cap <= 0:
            return False
        if (currency or "").upper() != self.settings.auto_approve_currency.upper():
            return False
        return Decimal("0") < amount <= self.cap

    # -- rate limit -----------------------------------------------------------
    def rate_key(self, function: str, client_id: str | None) -> str:
        return f"{client_id or 'unknown-client'}"

    def rate_allowed(self, key: str) -> bool:
        limit = self.settings.communicate_rate_limit
        if limit <= 0:
            return True
        since = time.time() - WINDOW_SECONDS
        row = self.db.query_one("SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND ts >= ?", (key, since))
        return int(row["n"]) < limit

    def rate_record(self, key: str) -> None:
        now = time.time()
        self.db.execute("INSERT INTO rate_events (key, ts) VALUES (?, ?)", (key, now))
        self.db.execute("DELETE FROM rate_events WHERE ts < ?", (now - 2 * WINDOW_SECONDS,))
