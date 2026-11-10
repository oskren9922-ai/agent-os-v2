"""Configuration.

Every setting the daemon needs is read once, from the process environment and
from a `.env` file in the project root. Real environment variables win over
the file, so a deploy platform can override anything without editing files.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = Path.home() / ".agentos" / "agentos.db"

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
LIVE_URL = "https://api-m.paypal.com"
MOCK_WEBHOOK_ID = "MOCK-WEBHOOK-ID"


class ConfigError(RuntimeError):
    """A setting is missing or invalid."""


class Settings(BaseModel):
    # PayPal
    paypal_env: str = "mock"  # mock | sandbox | live
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_auto_register: bool = True

    # Claude
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_max_tokens: int = 2000

    # Governance
    auto_approve_cap: float = 0.0
    auto_approve_currency: str = "USD"
    communicate_rate_limit: int = 20

    # Daemon
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = DEFAULT_DB_PATH
    dashboard_token: str = ""
    worker_enabled: bool = True
    collections_sweep_minutes: int = 0

    @field_validator("paypal_env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        value = (value or "mock").strip().lower()
        if value not in {"mock", "sandbox", "live"}:
            raise ValueError("PAYPAL_ENV must be mock, sandbox or live")
        return value

    @property
    def paypal_base_url(self) -> str:
        return LIVE_URL if self.paypal_env == "live" else SANDBOX_URL

    @property
    def is_sandbox_like(self) -> bool:
        """True when sandbox_only functions may run (mock or sandbox, never live)."""
        return self.paypal_env in {"mock", "sandbox"}

    @property
    def paypal_credentials_present(self) -> bool:
        return self.paypal_env == "mock" or bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def effective_webhook_id(self) -> str:
        if self.paypal_webhook_id:
            return self.paypal_webhook_id
        return MOCK_WEBHOOK_ID if self.paypal_env == "mock" else ""

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


# Environment variable name -> Settings field.
_ENV_MAP = {
    "PAYPAL_ENV": "paypal_env",
    "PAYPAL_CLIENT_ID": "paypal_client_id",
    "PAYPAL_CLIENT_SECRET": "paypal_client_secret",
    "PAYPAL_WEBHOOK_ID": "paypal_webhook_id",
    "PAYPAL_AUTO_REGISTER": "paypal_auto_register",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "ANTHROPIC_MODEL": "anthropic_model",
    "ANTHROPIC_MAX_TOKENS": "anthropic_max_tokens",
    "AUTO_APPROVE_CAP": "auto_approve_cap",
    "AUTO_APPROVE_CURRENCY": "auto_approve_currency",
    "COMMUNICATE_RATE_LIMIT": "communicate_rate_limit",
    "AGENTOS_HOST": "host",
    "AGENTOS_PORT": "port",
    "AGENTOS_DB_PATH": "db_path",
    "DASHBOARD_TOKEN": "dashboard_token",
    "WORKER_ENABLED": "worker_enabled",
    "COLLECTIONS_SWEEP_MINUTES": "collections_sweep_minutes",
}


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. Blank lines and # comments are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(env_file: Path | None = None, **overrides) -> Settings:
    """Build Settings from .env + environment, then apply explicit overrides."""
    file_values = read_env_file(env_file if env_file is not None else PROJECT_ROOT / ".env")
    merged = {**file_values, **os.environ}
    data: dict = {}
    for env_name, field in _ENV_MAP.items():
        value = merged.get(env_name)
        if value is None or value == "":
            continue  # empty means "use the default"
        data[field] = value
    data.update(overrides)
    try:
        return Settings(**data)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
