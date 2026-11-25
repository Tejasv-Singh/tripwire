"""List active Hyperliquid TESTNET addresses that hold open positions, for WATCH_ADDRESS.

How: listen to the public `trades` stream for a few coins (every trade carries
`users: [buyer, seller]`), then ask the public `/info clearinghouseState` endpoint which of
those traders hold open perp positions. Read-only, no keys, testnet only.

Run (from the repo root):  npm run watch:find
                     or:   npm run watch:find -- --seconds 60 --coins ETH,BTC
Then put one address in .env:   WATCH_ADDRESS=0x...   (and leave HL_ACCOUNT_ADDRESS empty)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect

from tripwire.config import TESTNET_API_URL, assert_testnet
from tripwire.hl.feed import ws_url_for
from tripwire.hl.positions import normalize_positions, to_float


async def collect_traders(coins: list[str], seconds: float) -> dict[str, int]:
    """address -> number of trades seen."""
    seen: dict[str, int] = {}
    async with connect(ws_url_for(TESTNET_API_URL), max_size=8 * 1024 * 1024) as ws:
        for coin in coins:
            await ws.send(
                json.dumps(
                    {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
                )
            )
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(left, 5.0))
            except TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("channel") != "trades":
                continue
            for trade in msg.get("data", []):
                for user in trade.get("users", []):
                    seen[user.lower()] = seen.get(user.lower(), 0) + 1
    return seen


HTTP_TIMEOUT_S = 20.0  # testnet /info is sometimes slow; one 10 s timeout killed a fresh-clone run
RETRIES = 2


async def post_info(client: httpx.AsyncClient, body: dict[str, Any]) -> Any:
    """POST /info with a retry; raises httpx.HTTPError if every attempt failed."""
    for attempt in range(RETRIES):
        try:
            resp = await client.post("/info", json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            if attempt == RETRIES - 1:
                raise
            await asyncio.sleep(1.0)
    raise AssertionError("unreachable")


async def describe(
    client: httpx.AsyncClient, address: str, mids: dict[str, float]
) -> dict[str, Any] | None:
    try:
        state = await post_info(client, {"type": "clearinghouseState", "user": address})
    except httpx.HTTPError as exc:  # one slow address must not end the whole search
        print(f"  (skipped {address[:10]}...: {type(exc).__name__})")
        return None
    positions = normalize_positions(state.get("assetPositions", []), mids, {})
    if not positions:
        return None
    top = max(positions, key=lambda p: p.size * p.mid)
    return {
        "address": address,
        "count": len(positions),
        "top": top,
        "with_liq": sum(1 for p in positions if p.liq_px is not None),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seconds", type=float, default=20.0, help="how long to listen to trades")
    ap.add_argument("--coins", default="ETH,BTC,SOL", help="comma-separated coins to listen to")
    ap.add_argument("--limit", type=int, default=8, help="how many addresses to print")
    args = ap.parse_args()
    assert_testnet(TESTNET_API_URL)

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    print(f"Listening to testnet trades on {', '.join(coins)} for {args.seconds:.0f}s ...")
    traders = await collect_traders(coins, args.seconds)
    print(f"{len(traders)} distinct traders seen. Checking who holds open positions ...\n")

    async with httpx.AsyncClient(base_url=TESTNET_API_URL, timeout=HTTP_TIMEOUT_S) as client:
        try:
            raw_mids = await post_info(client, {"type": "allMids"})
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"Hyperliquid testnet didn't answer ({type(exc).__name__}). It is sometimes "
                "slow: run `npm run watch:find` again in a minute."
            ) from None
        mids = {k: v for k, raw in raw_mids.items() if (v := to_float(raw)) is not None}
        found = []
        for address, _n in sorted(traders.items(), key=lambda kv: -kv[1]):
            info = await describe(client, address, mids)
            if info:
                found.append(info)
            if len(found) >= args.limit * 2:
                break

    # Easiest to read on the glasses: a handful of positions, ideally with a liquidation price.
    found.sort(key=lambda i: (i["with_liq"] == 0, i["count"] > 20, i["count"]))
    if not found:
        print("No open positions among recent traders. Try again with --seconds 60.")
        return
    print(f"{'address':<44} {'pos':>4}  largest position")
    for info in found[: args.limit]:
        p = info["top"]
        liq = (
            f"liq {p.liq_px:,.2f} ({p.liq_dist_pct:.1f}% from mid)"
            if p.liq_px is not None and p.liq_dist_pct is not None
            else "no liq price"
        )
        where = f"{p.coin} {p.side} {p.size:g} @ {p.entry:,.2f}"
        print(f"{info['address']:<44} {info['count']:>4}  {where}  {liq}")
    print(
        "\nPick one and put it in .env:  WATCH_ADDRESS=<address>  (HL_ACCOUNT_ADDRESS stays empty)"
    )
    print("Positions change all the time on testnet; if the HUD shows none, pick another.")


if __name__ == "__main__":
    asyncio.run(main())
