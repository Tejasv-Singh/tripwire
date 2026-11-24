"""Configuration (pydantic-settings, read from the repo-root `.env`) and the testnet guard.

Every variable is documented in `/.env.example`. Secrets are `SecretStr` so they never show up in
reprs or logs by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

from hyperliquid.utils import constants
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]  # .../backend
REPO_ROOT = BACKEND_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

# The only Hyperliquid endpoint this backend will ever talk to.
TESTNET_API_URL: str = constants.TESTNET_API_URL

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRIVATE_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
MIN_PAIRING_CODE_LEN = 8


class TestnetGuardError(RuntimeError):
    """Raised when anything tries to point Tripwire at a non-testnet Hyperliquid URL."""

    __test__ = False  # not a pytest test class, despite the name


def assert_testnet(url: str) -> None:
    """Hard testnet guard. Exact string match against the SDK constant -- no normalisation, so even
    a trailing slash is refused. Called at app creation and again inside the Hyperliquid feed."""
    if url != TESTNET_API_URL:
        raise TestnetGuardError(
            f"Refusing to start: HL_API_URL={url!r} is not the Hyperliquid TESTNET URL "
            f"{TESTNET_API_URL!r}. Tripwire is testnet-only."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- user-provided -------------------------------------------------------------------------
    hl_account_address: str = ""  # public 0x address; empty -> HUD shows only the reference price
    # Any public testnet address to follow read-only when HL_ACCOUNT_ADDRESS is empty ("WATCH").
    watch_address: str = ""
    hl_agent_key: SecretStr = SecretStr("")  # API/agent wallet key; empty -> trading off
    telegram_bot_token: SecretStr = SecretStr("")  # empty -> bot skipped
    anthropic_api_key: SecretStr = SecretStr("")  # unused on Day 1
    pairing_code: SecretStr  # required: the plugin must present it as /ws?code=...

    # --- operational ---------------------------------------------------------------------------
    hl_api_url: str = TESTNET_API_URL  # checked by assert_testnet(); do not change
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    db_path: Path = Path("data/tripwire.sqlite3")
    log_level: str = "INFO"

    @field_validator("hl_account_address", "watch_address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        v = v.strip()
        if v and not _ADDRESS_RE.match(v):
            raise ValueError("must be empty or a 0x-prefixed 40-hex-char address")
        return v.lower()

    @field_validator("hl_agent_key")
    @classmethod
    def _check_agent_key(cls, v: SecretStr) -> SecretStr:
        key = v.get_secret_value().strip()
        if key and not _PRIVATE_KEY_RE.match(key):
            # never echo the value: it is a private key
            raise ValueError(
                "HL_AGENT_KEY must be empty or a 64-hex-char private key (0x optional)"
            )
        return SecretStr(key)

    @field_validator("pairing_code")
    @classmethod
    def _check_pairing_code(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value().strip()) < MIN_PAIRING_CODE_LEN:
            raise ValueError(f"PAIRING_CODE must be at least {MIN_PAIRING_CODE_LEN} characters")
        return SecretStr(v.get_secret_value().strip())

    @property
    def watch_mode(self) -> bool:
        """WATCH = follow someone else's public positions; never trade. The own account wins."""
        return bool(self.watch_address and not self.hl_account_address)

    @property
    def stream_address(self) -> str:
        """Whose clearinghouseState the feed subscribes to ("" = reference price only)."""
        return self.hl_account_address or self.watch_address

    @property
    def trading_enabled(self) -> bool:
        """Orders need both: the agent key signs, the account address says whose positions."""
        return bool(self.hl_account_address and self.hl_agent_key.get_secret_value())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else BACKEND_DIR / self.db_path
