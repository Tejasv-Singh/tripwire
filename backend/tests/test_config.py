"""Testnet guard + settings validation."""

from __future__ import annotations

import pytest
from hyperliquid.utils import constants
from pydantic import ValidationError

from tripwire.config import Settings, TestnetGuardError, assert_testnet

GOOD_CODE = "pair-code-123"


def make(**kw: object) -> Settings:
    # _env_file=None: never read the developer's real .env during tests
    return Settings(_env_file=None, pairing_code=GOOD_CODE, **kw)  # type: ignore[arg-type,call-arg]


def test_guard_accepts_exact_testnet_url() -> None:
    assert_testnet(constants.TESTNET_API_URL)


@pytest.mark.parametrize(
    "url",
    [
        constants.MAINNET_API_URL,
        constants.LOCAL_API_URL,
        constants.TESTNET_API_URL + "/",  # even a trailing slash is refused
        "https://api.hyperliquid-testnet.xyz.evil.example",
        "",
    ],
)
def test_guard_refuses_everything_else(url: str) -> None:
    with pytest.raises(TestnetGuardError):
        assert_testnet(url)


def test_default_settings_point_at_testnet() -> None:
    s = make()
    assert s.hl_api_url == constants.TESTNET_API_URL
    assert s.hl_account_address == ""
    assert s.telegram_bot_token.get_secret_value() == ""


def test_pairing_code_required_and_min_length() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, pairing_code="short")  # type: ignore[arg-type,call-arg]


def test_pairing_code_is_secret_in_repr() -> None:
    assert GOOD_CODE not in repr(make())


def test_address_validation_and_lowercasing() -> None:
    addr = "0xABCDEF0123456789abcdef0123456789ABCDEF01"
    assert make(hl_account_address=addr).hl_account_address == addr.lower()
    with pytest.raises(ValidationError):
        make(hl_account_address="0x123")


def test_cors_origin_list_splits_and_trims() -> None:
    s = make(cors_origins=" http://a:1 , http://b:2,, ")
    assert s.cors_origin_list == ["http://a:1", "http://b:2"]
