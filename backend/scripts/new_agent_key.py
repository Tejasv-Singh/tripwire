"""Create a Hyperliquid TESTNET agent (API) wallet locally; its key goes into .env only.

Run: `npm run agent:new`

The private key goes straight into HL_AGENT_KEY in the repo-root .env. It is never printed,
logged or shown, so it can't end up in a terminal scrollback, a screenshot or a chat. Only the
agent's public address is printed: that is what you paste into the testnet site to authorize it.

  npm run agent:new             # refuses if HL_AGENT_KEY already has a value
  uv --directory backend run python scripts/new_agent_key.py --force   # replace an existing key
                                # (revoke the old agent on the testnet API page afterwards)

An agent wallet can place orders for your account but can't withdraw. Tripwire signs with it
only for TESTNET (config.py guard). DRY_RUN in .env is left as it is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eth_account import Account

from tripwire.config import ENV_FILE

KEY = "HL_AGENT_KEY"


def set_agent_key(env_text: str, key_hex: str, *, force: bool = False) -> str:
    """Return env_text with HL_AGENT_KEY set to key_hex. Keeps every other line (and its line
    ending) untouched; raises ValueError if a key is already set and force is False."""
    lines = env_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(f"{KEY}="):
            body = line.rstrip("\r\n")
            if body[len(KEY) + 1 :].strip() and not force:
                raise ValueError(
                    f"{KEY} already has a value in .env; not overwriting it. "
                    "Use --force to replace it (then revoke the old agent on the API page)."
                )
            lines[i] = f"{KEY}={key_hex}{line[len(body) :]}"
            return "".join(lines)
    newline = "\r\n" if "\r\n" in env_text else "\n"
    sep = "" if not env_text or env_text.endswith(("\n", "\r")) else newline
    return f"{env_text}{sep}{KEY}={key_hex}{newline}"


def main(argv: list[str] | None = None, env_file: Path = ENV_FILE) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--force", action="store_true", help="replace an existing HL_AGENT_KEY")
    args = ap.parse_args(argv)
    if not env_file.exists():
        print(f"No {env_file.name} yet: copy .env.example to .env first (README, Setup).")
        return 1
    text = env_file.read_text(encoding="utf-8")
    agent = Account.create()
    try:
        updated = set_agent_key(text, agent.key.hex(), force=args.force)
    except ValueError as exc:
        print(exc)
        return 1
    env_file.write_text(updated, encoding="utf-8", newline="")
    print("Agent wallet created. Its private key is now in .env as HL_AGENT_KEY (not shown).")
    print(f"Agent address (public, safe to paste): {agent.address}")
    print("Next:")
    print(
        "  1. app.hyperliquid-testnet.xyz with your MAIN wallet -> API -> put this address in the"
    )
    print("     API wallet address field (instead of 'Generate') -> Authorize, and sign.")
    print("  2. In .env: HL_ACCOUNT_ADDRESS=<your MAIN wallet address> (not the agent's).")
    print("     Leave DRY_RUN=true for now (docs/GO_LIVE.md).")
    print("  3. npm run golive:check   (check 2 confirms the agent is authorized for your account)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
