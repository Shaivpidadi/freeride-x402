"""Tests for the agent spending budget and the bundled demo wallet.

Both touch money: the budget decides how much an agent may spend, and the
bundled wallet decides *whose* money it is by default. The invariants worth
pinning are that a real payer always beats the bundled one, that the bundled
one can be refused outright, and that an amount typed by hand cannot silently
mean a thousand times what was intended.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from freeride.cli.cmd_wallet import parse_amount
from freeride.core import x402_hedera


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("FREERIDE_X402") or key.startswith("HEDERA_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ─── amount parsing ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100000", 100_000),
        ("1_000_000", 1_000_000),
        ("0.01hbar", 1_000_000),
        ("0.5 HBAR", 50_000_000),
        ("1hbar", 100_000_000),
        ("250tb", 250),
        ("  42  ", 42),
    ],
)
def test_parse_amount_accepts_tinybars_and_hbar(text: str, expected: int):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["0", "-5", "abc", "", "hbar", "1.2.3"])
def test_parse_amount_rejects_nonsense(text: str):
    with pytest.raises((ValueError, TypeError)):
        parse_amount(text)


def test_hbar_and_tinybars_do_not_collide():
    """The unit has to change the number, or the suffix is decoration."""
    assert parse_amount("1hbar") == 100_000_000 * parse_amount("1")


# ─── bundled demo wallet ─────────────────────────────────────────────


def _write_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **fields) -> None:
    payload = {
        "account_id": "0.0.999",
        "private_key_hex": "0xdemo",
        "network": "hedera:testnet",
        **fields,
    }
    target = tmp_path / "demo-wallet.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(x402_hedera, "_demo_wallet_path", lambda: target)


def test_bundled_wallet_is_used_when_nothing_is_configured(tmp_path, monkeypatch):
    """The whole point: a fresh install can pay without setup."""
    _write_demo(tmp_path, monkeypatch)
    account, key = x402_hedera.load_payer_credentials()
    assert (account, key) == ("0.0.999", "0xdemo")
    assert x402_hedera.demo_wallet_in_use() is True
    assert x402_hedera.has_payer_credentials() is True


def test_configured_payer_beats_the_bundled_one(tmp_path, monkeypatch):
    """A real wallet must never be silently shadowed by the shipped one."""
    _write_demo(tmp_path, monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_PAYER_ACCOUNT", "0.0.111")
    monkeypatch.setenv("FREERIDE_X402_PAYER_KEY", "0xreal")
    assert x402_hedera.load_payer_credentials() == ("0.0.111", "0xreal")
    assert x402_hedera.demo_wallet_in_use() is False


def test_hedera_aliases_also_beat_the_bundled_one(tmp_path, monkeypatch):
    _write_demo(tmp_path, monkeypatch)
    monkeypatch.setenv("HEDERA_ACCOUNT_ID", "0.0.222")
    monkeypatch.setenv("HEDERA_PRIVATE_KEY", "0xalias")
    assert x402_hedera.load_payer_credentials() == ("0.0.222", "0xalias")
    assert x402_hedera.demo_wallet_in_use() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_bundled_wallet_can_be_refused(tmp_path, monkeypatch, value):
    _write_demo(tmp_path, monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_DEMO_WALLET", value)
    assert x402_hedera.load_payer_credentials() == (None, None)
    assert x402_hedera.has_payer_credentials() is False
    assert x402_hedera.demo_wallet_in_use() is False


def test_half_a_wallet_is_not_a_wallet(tmp_path, monkeypatch):
    """An account with no key must not read as a configured payer."""
    _write_demo(tmp_path, monkeypatch, private_key_hex="")
    assert x402_hedera.load_payer_credentials() == (None, None)


def test_missing_bundle_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(x402_hedera, "_demo_wallet_path", lambda: tmp_path / "nope.json")
    assert x402_hedera.bundled_demo_payer() == (None, None)
    assert x402_hedera.load_payer_credentials() == (None, None)


def test_corrupt_bundle_is_not_an_error(tmp_path, monkeypatch):
    """A broken shipped file must not take the gateway down at import time."""
    target = tmp_path / "demo-wallet.json"
    target.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(x402_hedera, "_demo_wallet_path", lambda: target)
    assert x402_hedera.bundled_demo_payer() == (None, None)


def test_shipped_demo_wallet_is_testnet_and_warns():
    """Guards the real file in the repo, not a fixture."""
    account, key = x402_hedera.bundled_demo_payer()
    if account is None:
        pytest.skip("no demo wallet bundled in this build")
    data = json.loads(x402_hedera._demo_wallet_path().read_text(encoding="utf-8"))
    assert data["network"] == "hedera:testnet", "a mainnet key must never ship"
    assert "warning" in data, "the shipped key must carry its own warning"
    assert key is not None and key.startswith("0x")
