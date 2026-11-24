"""DRY_RUN: real testnet payloads, really signed (throwaway or agent key), logged, never sent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hyperliquid.utils import constants
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from tripwire.config import TESTNET_API_URL, Settings, TestnetGuardError
from tripwire.exchanges.hyperliquid import HyperliquidAdapter
from tripwire.orders import CloseOrder, OpenOrder, StopOrder
from tripwire.state import MarketState

from .test_trading import ACCOUNT, AGENT_KEY, FakeInfo, Poster, live_state


def dry(
    tmp_path: Path, *, agent_key: str = "", account: str = ACCOUNT, state: MarketState | None = None
) -> tuple[HyperliquidAdapter, Poster, Path]:  # noqa: E501
    poster = Poster()
    log = tmp_path / "dry_run_orders.jsonl"
    a = HyperliquidAdapter(
        state or live_state(),
        base_url=TESTNET_API_URL,
        account_address=account,
        agent_key=agent_key,
        dry_run=True,
        dry_run_log=log,
        info_factory=FakeInfo,
        poster=poster,
    )
    return a, poster, log


def lines(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def signer_of(record: dict[str, Any], *, mainnet: bool) -> str:
    p = record["payload"]
    return str(
        recover_agent_or_user_from_l1_action(
            p["action"], p["signature"], p["vaultAddress"], p["nonce"], p["expiresAfter"], mainnet
        )
    )


def test_settings_default_to_dry_run(tmp_path: Path) -> None:
    s = Settings(_env_file=None, pairing_code="x" * 8, db_path=tmp_path / "s.db")  # type: ignore[call-arg]
    assert s.dry_run is True
    assert not s.trading_enabled  # no account
    with_account = Settings(  # type: ignore[call-arg]
        _env_file=None, pairing_code="x" * 8, hl_account_address=ACCOUNT, db_path=tmp_path / "s.db"
    )
    assert with_account.trading_enabled  # dry run needs no agent key
    live_no_key = Settings(  # type: ignore[call-arg]
        _env_file=None, pairing_code="x" * 8, hl_account_address=ACCOUNT, dry_run=False,
        db_path=tmp_path / "s.db",
    )  # fmt: skip
    assert not live_no_key.trading_enabled


def test_dry_run_refuses_mainnet() -> None:
    with pytest.raises(TestnetGuardError):
        HyperliquidAdapter(
            MarketState(), base_url=constants.MAINNET_API_URL, account_address=ACCOUNT, dry_run=True
        )


async def test_close_is_signed_with_a_throwaway_key_logged_and_not_sent(tmp_path: Path) -> None:
    a, poster, log = dry(tmp_path)
    assert a.can_trade and a.signer_kind == "throwaway"
    await a.start()
    assert a.status == "ready" and a.detail.startswith("DRY RUN: signed with throwaway key")
    r = await a.close_reduce_only(CloseOrder("ETH", "LONG", 0.5, 0.25))
    assert (r.ok, r.status) == (True, "dry_run")
    assert r.message == "DRY RUN: sell 0.25 ETH IOC @ 3319.7 signed, not sent"
    assert poster.payloads == []  # NOTHING was POSTed
    (rec,) = lines(log)
    assert rec["network"] == "testnet" and rec["what"] == "close ETH 0.25"
    (o,) = rec["payload"]["action"]["orders"]
    assert o == {
        "a": 4, "b": False, "p": "3319.7", "s": "0.25", "r": True, "t": {"limit": {"tif": "Ioc"}},
    }  # fmt: skip  # identical to the live wire (test_trading)
    # the signature is real: it recovers to our signer -- for TESTNET, not for mainnet
    assert signer_of(rec, mainnet=False).lower() == a.agent_address.lower()
    assert signer_of(rec, mainnet=True).lower() != a.agent_address.lower()
    assert AGENT_KEY not in log.read_text(encoding="utf-8")


async def test_dry_run_with_an_agent_key_signs_with_it(tmp_path: Path) -> None:
    a, poster, log = dry(tmp_path, agent_key=AGENT_KEY)
    assert a.signer_kind == "agent"
    await a.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    (rec,) = lines(log)
    assert (
        signer_of(rec, mainnet=False).lower() == a.agent_address.lower() and poster.payloads == []
    )


async def test_dry_run_set_sl_signs_place_and_cancel(tmp_path: Path) -> None:
    a, poster, log = dry(tmp_path)
    r = await a.set_sl(StopOrder("ETH", "LONG", 3412.0, 3300.0, 88, "SL to breakeven"))
    assert (r.status, r.message) == (
        "dry_run",
        "DRY RUN: SL 3412 signed + cancel of old SL, not sent",
    )
    place, cancel = lines(log)
    assert place["payload"]["action"]["grouping"] == "positionTpsl"
    assert cancel["payload"]["action"] == {"type": "cancel", "cancels": [{"a": 4, "o": 88}]}
    assert poster.payloads == []


async def test_dry_run_open_without_any_account(tmp_path: Path) -> None:
    a, poster, log = dry(tmp_path, account="")  # price-only mode can still sign drafts
    assert a.can_trade
    r = await a.place_order(OpenOrder("ETH", "LONG", 100.0, 3.0, 3400.0, 3300.0, 3600.0))
    assert r.status == "dry_run" and poster.payloads == []
    lev, order = lines(log)
    assert lev["payload"]["action"]["type"] == "updateLeverage"
    legs = order["payload"]["action"]["orders"]
    assert [o["t"].get("trigger", {}).get("tpsl") for o in legs] == [None, "sl", "tp"]


def test_watch_mode_never_trades_even_in_dry_run() -> None:
    a = HyperliquidAdapter(
        MarketState(), base_url=TESTNET_API_URL, account_address="", watch=True, dry_run=True
    )
    assert not a.can_trade and "watch only" in a.detail
