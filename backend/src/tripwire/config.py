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
    anthropic_api_key: SecretStr = SecretStr("")  # not used (no Anthropic key: see BUILD_LOG §19)
    # Reply drafts after a fill (replies.py). Empty key -> templates only.
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "google/gemma-4-31b-it:free"
    pairing_code: SecretStr  # required: the plugin must present it as /ws?code=...

    # Your own numeric Telegram user id: only this user may use /kill, /status, forwarded order
    # drafts, and receives alert DMs. The bot's /start reply tells you the number.
    telegram_owner_id: int | None = None
    # Your own journal channel (or group) for end-of-session summaries; the bot must be an admin
    # there. Add the bot to the channel and it DMs you this id. Empty = no journal posts.
    journal_chat_id: int | None = None

    # DRY_RUN (default ON): orders are built and SIGNED exactly like live ones (with HL_AGENT_KEY,
    # or a throwaway key generated at startup if it is empty) and logged, never POSTed.
    # Set DRY_RUN=false (with an agent key) to really trade on testnet.
    dry_run: bool = True

    # --- liquidation alerts (risk.py) ------------------------------------------------------------
    alert_liq_pct: float = 5.0  # alert when liq is this % or closer to mark (Telegram /threshold)
    alert_hysteresis_pct: float = 1.0  # re-arm only once it is threshold + this % away again
    alert_snooze_s: float = 300.0  # "Snooze 5m" on the glasses

    # --- push-to-talk voice commands (voice.py) --------------------------------------------------
    voice_enabled: bool = True  # local faster-whisper; false = the long-press does nothing
    voice_model: str = "base"  # faster-whisper size; "base" ~145 MB, ~1.8 s per phrase on CPU

    # --- forwarded-message drafts (drafts.py) ---------------------------------------------------
    draft_default_notional_usd: float = 100.0  # when the message names no size
    draft_default_leverage: float = 1.0  # when the message names no "Nx"

    # --- policy guard (policy.py) ---------------------------------------------------------------
    kill_switch: bool = False  # true -> no order that adds exposure (closes / tighter SLs still ok)
    max_open_notional_usd: float = 250.0  # per opening order
    max_leverage: float = 5.0  # per opening order
    daily_loss_limit_usd: float = 100.0  # realized loss today (UTC) that locks opening orders

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

    @field_validator("telegram_owner_id", "journal_chat_id", mode="before")
    @classmethod
    def _empty_owner_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

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
        """Live orders need an account and the agent key; DRY_RUN signs with a throwaway key."""
        has_signer = bool(self.hl_agent_key.get_secret_value()) or self.dry_run
        return bool(self.hl_account_address and has_signer and not self.watch_mode)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else BACKEND_DIR / self.db_path
