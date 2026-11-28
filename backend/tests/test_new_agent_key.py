"""npm run agent:new (go-live helper): the agent key goes into .env and is never printed."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "new_agent_key.py"
KEY = "ab" * 32


def load() -> Any:
    spec = importlib.util.spec_from_file_location("new_agent_key", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fills_the_empty_line_and_keeps_everything_else() -> None:
    mod = load()
    env = "# comment\r\nHL_ACCOUNT_ADDRESS=\r\nHL_AGENT_KEY=\r\nDRY_RUN=true\r\n"
    out = mod.set_agent_key(env, KEY)
    assert out == f"# comment\r\nHL_ACCOUNT_ADDRESS=\r\nHL_AGENT_KEY={KEY}\r\nDRY_RUN=true\r\n"


def test_refuses_to_overwrite_unless_forced() -> None:
    mod = load()
    env = f"HL_AGENT_KEY={'cd' * 32}\nDRY_RUN=true\n"
    with pytest.raises(ValueError, match="already has a value"):
        mod.set_agent_key(env, KEY)
    assert mod.set_agent_key(env, KEY, force=True) == f"HL_AGENT_KEY={KEY}\nDRY_RUN=true\n"


def test_appends_when_the_line_is_missing() -> None:
    mod = load()
    assert mod.set_agent_key("DRY_RUN=true", KEY) == f"DRY_RUN=true\nHL_AGENT_KEY={KEY}\n"
    assert mod.set_agent_key("A=1\r\n", KEY) == f"A=1\r\nHL_AGENT_KEY={KEY}\r\n"


def test_main_writes_a_valid_key_and_prints_only_the_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load()
    env = tmp_path / ".env"
    env.write_text(
        "PAIRING_CODE=test-pairing-code\nHL_AGENT_KEY=\nDRY_RUN=true\n", encoding="utf-8"
    )
    assert mod.main([], env_file=env) == 0
    written = re.search(r"^HL_AGENT_KEY=([0-9a-f]{64})$", env.read_text(encoding="utf-8"), re.M)
    assert written is not None
    key = written.group(1)
    out = capsys.readouterr().out
    address = Account.from_key(key).address
    assert f"Agent address (public, safe to paste): {address}" in out
    assert key not in out and key.upper() not in out  # the private key is never printed
    assert "DRY_RUN=true" in env.read_text(encoding="utf-8")  # untouched
    from tripwire.config import Settings

    s = Settings(_env_file=env, db_path=tmp_path / "t.sqlite3")  # type: ignore[call-arg]
    assert s.hl_agent_key.get_secret_value() == key  # the config's own validation accepts it
    assert mod.main([], env_file=env) == 1  # a second run refuses to overwrite
    assert "already has a value" in capsys.readouterr().out


def test_main_without_env_file_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load().main([], env_file=tmp_path / ".env") == 1
    assert "copy .env.example to .env first" in capsys.readouterr().out
