"""``freeride wallet`` — Hedera x402 cash-lane setup (terminal-first).

Keys live in ``~/.freeride/.env`` next to provider keys. Never prints
private keys.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from freeride.core.dotenv import DEFAULT_DOTENV_PATH, load_dotenv_into_environ, parse_dotenv
from freeride.core.x402_hedera import (
    DEFAULT_FEE_PAYER,
    has_payer_credentials,
    load_payer_credentials,
    load_x402_config,
)

_DEFAULT_OUT = DEFAULT_DOTENV_PATH


def _read_existing(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _write_env(path: Path, kvs: dict[str, str]) -> None:
    body = [
        "# Managed by FreeRide (`freeride init` / `freeride wallet setup`).",
        "# Re-running those commands merges new values without dropping others.",
        "",
    ]
    for k in sorted(kvs):
        body.append(f"{k}={kvs[k]}")
    body.append("")
    from freeride.core.state import atomic_write

    atomic_write(path, "\n".join(body))


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "(set)"
    return value[:4] + "…" + value[-4:]


def cmd_wallet_status(_args=None) -> int:
    load_dotenv_into_environ()
    cfg = load_x402_config()
    account, key = load_payer_credentials()
    payer_set = bool(account and key)

    print("FreeRide wallet (Hedera x402)")
    print(f"  enabled     : {'yes' if cfg.enabled else 'no'}")
    print(f"  ready       : {'yes' if cfg.ready else 'no'} (needs enabled + pay_to)")
    print(f"  pay_to      : {cfg.pay_to or '(not set)'}")
    print(f"  network     : {cfg.network}")
    print(f"  amount      : {cfg.amount} tinybars ({cfg.asset})")
    print(f"  facilitator : {cfg.facilitator}")
    print(f"  fee_payer   : {cfg.fee_payer or DEFAULT_FEE_PAYER + ' (default)'}")
    print(f"  dry_run     : {'yes' if cfg.dry_run else 'no'}")
    print(f"  payer       : {account if account else '(not set)'}")
    print(f"  payer key   : {'set' if key else 'missing'} (never printed)")
    print(f"  auto-pay    : {'yes' if (cfg.ready and payer_set) else 'no'}")
    paid = bool(cfg.paid_openrouter_api_key)
    print(f"  paid upstream key : {'set' if paid else 'missing'}")
    if cfg.enabled and cfg.ready and not payer_set:
        print()
        print("  tip: run `freeride wallet setup` so free→paid is seamless (daemon auto-pays).")
    return 0


def cmd_wallet_setup(args, *, _input: Callable = input) -> int:
    out_path = Path(args.out) if getattr(args, "out", None) else _DEFAULT_OUT
    print("FreeRide Hedera wallet setup")
    print(f"Writes to: {out_path}")
    print("Empty input keeps the current value. Ctrl-C aborts.")
    print()

    existing = _read_existing(out_path)
    # Seed process view from file so prompts show current.
    for k, v in existing.items():
        os.environ.setdefault(k, v)

    cfg = load_x402_config()
    account, key = load_payer_credentials()
    new_kvs = dict(existing)

    def _ask(label: str, current: str = "") -> str | None:
        shown = _mask(current) if current else ""
        prompt = f"  {label}"
        if shown:
            prompt += f" [current: {shown}, Enter to keep]"
        prompt += ": "
        try:
            value = _input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted — no file written.")
            raise SystemExit(1)
        if value:
            return value
        return None  # keep

    print("Payer (signs micropayments when free inference dies)")
    v = _ask("Hedera account id (0.0.x)", account or "")
    if v is not None:
        new_kvs["FREERIDE_X402_PAYER_ACCOUNT"] = v
        new_kvs["HEDERA_ACCOUNT_ID"] = v
    v = _ask("ECDSA private key", key or "")
    if v is not None:
        new_kvs["FREERIDE_X402_PAYER_KEY"] = v
        new_kvs["HEDERA_PRIVATE_KEY"] = v

    print()
    print("Merchant receive account (pay_to)")
    v = _ask("pay_to account id", cfg.pay_to or "")
    if v is not None:
        new_kvs["FREERIDE_X402_PAY_TO"] = v
    elif not (new_kvs.get("FREERIDE_X402_PAY_TO") or cfg.pay_to):
        print("  pay_to is required for the cash lane — set it now or later.")

    # Enable x402 if missing.
    if new_kvs.get("FREERIDE_X402_ENABLED", os.environ.get("FREERIDE_X402_ENABLED", "")).strip() not in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    ):
        new_kvs["FREERIDE_X402_ENABLED"] = "1"
        print("  FREERIDE_X402_ENABLED=1")

    if not new_kvs:
        print("nothing to save.")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_env(out_path, new_kvs)
    print()
    print(f"wrote wallet settings to {out_path}")
    print("next: restart/reload the gateway (`freeride reload` or `ridex restart`)")
    print("      then `freeride wallet status`")
    return 0


def cmd_wallet(args, *, _input: Callable = input) -> int:
    action = getattr(args, "wallet_command", None) or getattr(args, "action", None)
    if action == "status":
        return cmd_wallet_status(args)
    if action == "setup":
        return cmd_wallet_setup(args, _input=_input)
    print("usage: freeride wallet status|setup")
    return 1
