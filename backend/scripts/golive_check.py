"""`npm run golive:check`: is Tripwire ready to trade on Hyperliquid TESTNET?

Prints a PASS / FAIL / SKIP list and the exact next command; exit code 0 only if everything
passed. Reads .env, calls only public testnet /info endpoints, never sends an order.
See src/tripwire/golive.py for what each check does, and docs/GO_LIVE.md for what comes next.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import httpx

from tripwire.config import TESTNET_API_URL, Settings
from tripwire.golive import render, run_checks


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)  # keep the adapter's INFO lines out of the report
    settings = Settings()  # type: ignore[call-arg]
    async with httpx.AsyncClient(base_url=TESTNET_API_URL, timeout=15) as client:

        async def post(body: dict[str, Any]) -> Any:
            resp = await client.post("/info", json=body)
            resp.raise_for_status()
            return resp.json()

        results = await run_checks(settings, post)
    print(render(results, settings))
    return 0 if all(r.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
