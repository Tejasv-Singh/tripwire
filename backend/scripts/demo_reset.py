"""Clean slate between demo takes: `npm run demo:reset`.

With the backend running (default http://127.0.0.1:8000), it POSTs /admin/demo-reset with the
pairing code from .env. The backend then:
  - closes every open alert episode and clears the Away-mode DM cooldowns
  - re-arms and unsnoozes every position in the risk engine
  - sets the alert threshold back to ALERT_LIQ_PCT from .env
  - starts a fresh session (the current one is NOT stored in the journal or the history)
  - sends every connected glasses client back to the plain HUD
  - drops a waiting forwarded draft, the inbox line and the latency samples

It never touches .env (only reads it), exchange positions or orders, the kill switch, or the
stored session history.

With the backend NOT running, everything above is in memory and starts fresh on the next start
anyway. Only the saved /threshold survives restarts, so it is reset in SQLite instead.

  npm run demo:reset
  uv --directory backend run python scripts/demo_reset.py --url http://127.0.0.1:8010
(the second form targets the demo backend on :8010. In PowerShell, `npm run demo:reset --
--url ...` loses the `--`, so use the uv form.)
"""

from __future__ import annotations

import argparse

import httpx

from tripwire.config import Settings
from tripwire.db import Store
from tripwire.risk import THRESHOLD_KEY


def reset_offline(settings: Settings) -> str:
    """Backend down: reset the only persisted demo state, the saved alert threshold."""
    store = Store(settings.resolved_db_path)
    store.init()
    before = store.kv_get(THRESHOLD_KEY)
    store.kv_set(THRESHOLD_KEY, f"{settings.alert_liq_pct:g}")
    return f"saved alert threshold {before or 'unset'} -> {settings.alert_liq_pct:g}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="the running backend")
    args = ap.parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]  # reads .env, never writes it
    url = args.url.rstrip("/")
    try:
        resp = httpx.post(
            f"{url}/admin/demo-reset",
            headers={"X-Pairing-Code": settings.pairing_code.get_secret_value()},
            timeout=10,
        )
    except httpx.ConnectError:
        print(f"No backend at {url}: alerts, cooldowns and the session start fresh on its next")
        print(f"start anyway. {reset_offline(settings)} (SQLite only; .env untouched).")
        return 0
    if resp.status_code != 200:
        print(f"Reset refused: HTTP {resp.status_code} {resp.text[:200]}")
        return 1
    body = resp.json()
    r = body["reset"]
    a = r["alerts"]
    print(f"Demo reset done at {url}:")
    print(f"  alerts: {a['episodes']} open episode(s) closed, {a['cooldowns']} DM cooldown(s) "
          f"cleared, threshold back to {a['threshold_pct']:g}%")  # fmt: skip
    print(f"  session: {r['session']}")
    print(f"  glasses: {r['clients_back_to_hud']} client(s) back to the HUD; draft, inbox and "
          f"{r['latency_samples']} latency sample(s) cleared")  # fmt: skip
    print(f"  untouched: {', '.join(body['untouched'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
