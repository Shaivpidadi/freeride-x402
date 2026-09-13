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
    bundled_demo_payer,
    demo_wallet_in_use,
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
    if cfg.force_paid:
        print("  FORCE PAID  : ON - free providers are skipped; every request pays")
    demo = demo_wallet_in_use()
    suffix = "  [BUNDLED DEMO WALLET - public key, testnet only]" if demo else ""
    print(f"  payer       : {(account if account else '(not set)')}{suffix}")
    print(f"  payer key   : {'set' if key else 'missing'} (never printed)")
    print(f"  auto-pay    : {'yes' if (cfg.ready and payer_set) else 'no'}")
    paid = bool(cfg.paid_openrouter_api_key)
    print(f"  paid upstream key : {'set' if paid else 'missing'}")
    if demo:
        print()
        print("  This is the demo payer that ships with FreeRide. Anyone can spend it.")
        print("  Your own wallet:  freeride wallet setup")
        print("  Refuse the demo:  FREERIDE_X402_DEMO_WALLET=0")
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

    if getattr(args, "demo", False):
        demo_account, demo_key = bundled_demo_payer()
        if not demo_account or not demo_key:
            print("No demo wallet is bundled with this build. Run `freeride wallet setup`.")
            return 1
        existing["FREERIDE_X402_PAYER_ACCOUNT"] = demo_account
        existing["HEDERA_ACCOUNT_ID"] = demo_account
        existing["FREERIDE_X402_PAYER_KEY"] = demo_key
        existing["HEDERA_PRIVATE_KEY"] = demo_key
        existing.setdefault("FREERIDE_X402_PAY_TO", demo_account)
        existing["FREERIDE_X402_ENABLED"] = "1"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_env(out_path, existing)
        print(f"  payer  : {demo_account}  (bundled demo wallet, testnet)")
        print(f"  pay_to : {existing['FREERIDE_X402_PAY_TO']}")
        print()
        print("  This key ships in the package and is public. Demos only.")
        print(f"wrote {out_path}")
        print("next: `freeride restart` (or `freeride reload`)")
        return 0

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


TINYBARS_PER_HBAR = 100_000_000
DEFAULT_AGENT_MAX = "1000000"  # 0.01 HBAR


def parse_amount(raw: str) -> int:
    """Accept tinybars, or a friendlier ``0.01hbar`` / ``0.01 HBAR``.

    Money typed by hand is easy to get wrong by three orders of magnitude,
    so the unit is allowed to be explicit.
    """
    text = raw.strip().lower().replace("_", "").replace(" ", "")
    if text.endswith("hbar"):
        return int(round(float(text[:-4]) * TINYBARS_PER_HBAR))
    if text.endswith("tinybars"):
        text = text[:-8]
    elif text.endswith("tb"):
        text = text[:-2]
    value = int(text)
    if value <= 0:
        raise ValueError("amount must be positive")
    return value


def _fmt_hbar(tinybars: int) -> str:
    return f"{tinybars / TINYBARS_PER_HBAR:.8f}".rstrip("0").rstrip(".") + " HBAR"


def cmd_wallet_budget(args) -> int:
    """Show or change the ceiling agents may spend, without touching keys."""
    out_path = Path(args.out) if getattr(args, "out", None) else _DEFAULT_OUT
    load_dotenv_into_environ()
    existing = _read_existing(out_path)
    changed: list[str] = []

    if getattr(args, "max_amount", None) is not None:
        try:
            amount = parse_amount(args.max_amount)
        except (ValueError, TypeError):
            print(f"  not an amount: {args.max_amount!r} (try 1000000 or 0.01hbar)")
            return 2
        existing["FREERIDE_X402_MAX_AMOUNT"] = str(amount)
        changed.append(f"per-payment ceiling -> {amount} tinybars ({_fmt_hbar(amount)})")

    if getattr(args, "any_payee", False):
        existing.pop("FREERIDE_X402_ALLOWED_PAYEES", None)
        changed.append("payee allowlist cleared (any account may be paid)")
    elif getattr(args, "allow_payee", None):
        payees = sorted({p.strip() for p in args.allow_payee if p.strip()})
        existing["FREERIDE_X402_ALLOWED_PAYEES"] = ",".join(payees)
        changed.append(f"payees restricted to {', '.join(payees)}")

    agent_pay = getattr(args, "agent_pay", None)
    if agent_pay is not None:
        if agent_pay == "on":
            existing["FREERIDE_X402_AGENT_PAY_ENABLED"] = "1"
            changed.append("agent payments ON — local agents may spend from this wallet")
        else:
            existing.pop("FREERIDE_X402_AGENT_PAY_ENABLED", None)
            changed.append("agent payments OFF")

    if changed:
        existing.setdefault("FREERIDE_X402_MAX_AMOUNT", DEFAULT_AGENT_MAX)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_env(out_path, existing)
        for line in changed:
            print(f"  {line}")
        print()
        print(f"wrote {out_path}")
        print("next: `freeride restart` (or `freeride reload`) to apply")
        # Re-read so the summary below reflects what was just written.
        for key, value in existing.items():
            os.environ[key] = value

    enabled = os.environ.get("FREERIDE_X402_AGENT_PAY_ENABLED", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )
    raw_max = os.environ.get("FREERIDE_X402_MAX_AMOUNT", "").strip() or DEFAULT_AGENT_MAX
    try:
        ceiling = int(raw_max)
    except ValueError:
        ceiling = int(DEFAULT_AGENT_MAX)
    payees = [p.strip() for p in os.environ.get("FREERIDE_X402_ALLOWED_PAYEES", "").split(",") if p.strip()]

    print("Agent spending budget")
    print(f"  agent payments : {'on' if enabled else 'off'}")
    print(f"  per payment    : {ceiling} tinybars ({_fmt_hbar(ceiling)})")
    print(f"  payees         : {', '.join(payees) if payees else 'any'}")
    if not changed:
        print()
        print("  change it:  freeride wallet budget --max-amount 0.05hbar --agent-pay on")
    return 0


def cmd_wallet(args, *, _input: Callable = input) -> int:
    action = getattr(args, "wallet_command", None) or getattr(args, "action", None)
    if action == "status":
        return cmd_wallet_status(args)
    if action == "setup":
        return cmd_wallet_setup(args, _input=_input)
    if action == "budget":
        return cmd_wallet_budget(args)
    print("usage: freeride wallet status|setup|budget")
    return 1
