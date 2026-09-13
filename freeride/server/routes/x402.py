"""Agent-facing Hedera x402 payment endpoint.

FreeRide already pays for its *own* inference when free providers die. This
route exposes that same machinery to any local agent that hits a 402 on some
other resource: the agent sends the challenge's payment requirements, the
daemon signs and settles with its payer key, and the agent gets back a
ready-to-send ``PAYMENT-SIGNATURE`` value.

The point is that the payer key never leaves this process. An agent (eve-bot,
a script, anything on localhost) needs no Hedera dependencies and never sees
the key -- it asks the daemon to pay, the same way it asks it for inference.

Because this signs real value, it enforces its own ceiling. ``/v1/chat`` can
only ever spend ``FREERIDE_X402_AMOUNT`` per request against a payee this
operator configured; an arbitrary caller here supplies both, so a cap and a
network check are the only things standing between a buggy agent loop and an
empty wallet.

Policy env:
- ``FREERIDE_X402_AGENT_PAY_ENABLED``  off by default; must be set to opt in
- ``FREERIDE_X402_MAX_AMOUNT``         per-payment ceiling in tinybars
- ``FREERIDE_X402_ALLOWED_PAYEES``     optional comma-separated allowlist
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from freeride.core.x402_hedera import (
    HEADER_PAYMENT_REQUIRED,
    X402Error,
    create_signed_payment_payload,
    decode_payment_payload,
    encode_settlement_response,
    has_payer_credentials,
    load_x402_config,
    payment_requirements_from_required,
    verify_and_settle,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 1 HBAR. Well above the 0.001 demo price and far below anything that would
# quietly drain a funded testnet payer in a runaway tool loop.
DEFAULT_MAX_AMOUNT = 100_000_000


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _agent_pay_enabled() -> bool:
    return _truthy(os.environ.get("FREERIDE_X402_AGENT_PAY_ENABLED", ""))


def _max_amount() -> int:
    raw = os.environ.get("FREERIDE_X402_MAX_AMOUNT", "").strip()
    if not raw:
        return DEFAULT_MAX_AMOUNT
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("FREERIDE_X402_MAX_AMOUNT=%r is not an integer; using default", raw)
        return DEFAULT_MAX_AMOUNT
    return parsed if parsed > 0 else DEFAULT_MAX_AMOUNT


def _allowed_payees() -> set[str]:
    raw = os.environ.get("FREERIDE_X402_ALLOWED_PAYEES", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"type": code, "message": message, **extra}},
    )


def _requirements_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Accept either one accepts[] entry or a whole PaymentRequired challenge.

    Agents copy the 402's ``PAYMENT-REQUIRED`` header around verbatim, so
    taking the full challenge (base64 or decoded) saves every caller from
    reimplementing the same unwrap.
    """
    reqs = body.get("paymentRequirements")
    if isinstance(reqs, dict):
        return reqs

    required = body.get("paymentRequired")
    if isinstance(required, str):
        required = decode_payment_payload(required)
    if isinstance(required, dict):
        return payment_requirements_from_required(required)

    raise X402Error(
        "Send paymentRequirements (one accepts[] entry), or paymentRequired "
        "(the decoded challenge or its base64 PAYMENT-REQUIRED header value)."
    )


@router.post("/v1/_freeride/x402/pay")
async def x402_pay(request: Request) -> JSONResponse:
    """Sign + settle one x402 payment on the caller's behalf.

    Returns the base64 ``PAYMENT-SIGNATURE`` value to retry the original
    request with, plus the settlement for the caller's audit trail.
    """
    cfg = load_x402_config()

    if not _agent_pay_enabled():
        return _error(
            403,
            "agent_pay_disabled",
            "Agent payments are off. Set FREERIDE_X402_AGENT_PAY_ENABLED=1 to let "
            "local agents spend from this wallet.",
        )
    if not cfg.enabled:
        return _error(
            409, "x402_disabled", "Set FREERIDE_X402_ENABLED=1 to enable the cash lane."
        )
    if not has_payer_credentials():
        return _error(
            409,
            "no_payer",
            "No payer configured. Run `freeride wallet setup` so the daemon can sign.",
        )

    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid_json", "Request body must be a JSON object.")
    if not isinstance(body, dict):
        return _error(400, "invalid_json", "Request body must be a JSON object.")

    try:
        requirements = _requirements_from_body(body)
    except X402Error as e:
        return _error(400, "invalid_requirements", e.message)

    # ---- policy: the caller chose these, so check them before signing ----
    network = str(requirements.get("network") or "")
    if network != cfg.network:
        return _error(
            403,
            "network_not_allowed",
            f"This wallet pays on {cfg.network}, but the challenge asks for "
            f"{network or '(unset)'}.",
        )

    pay_to = str(requirements.get("payTo") or "")
    if not pay_to:
        return _error(400, "invalid_requirements", "Payment requirements have no payTo.")
    allowed = _allowed_payees()
    if allowed and pay_to not in allowed:
        return _error(
            403,
            "payee_not_allowed",
            f"{pay_to} is not in FREERIDE_X402_ALLOWED_PAYEES.",
            pay_to=pay_to,
        )

    raw_amount = requirements.get("amount")
    try:
        amount = int(str(raw_amount))
    except (TypeError, ValueError):
        return _error(
            400, "invalid_requirements", f"Payment amount {raw_amount!r} is not an integer."
        )
    if amount <= 0:
        return _error(400, "invalid_requirements", "Payment amount must be positive.")
    ceiling = _max_amount()
    if amount > ceiling:
        return _error(
            403,
            "amount_above_cap",
            f"Payment of {amount} tinybars exceeds FREERIDE_X402_MAX_AMOUNT ({ceiling}).",
            amount=amount,
            max_amount=ceiling,
        )

    resource_url = str(body.get("resourceUrl") or "") or None
    logger.info(
        "x402 agent payment: %s tinybars -> %s on %s (resource=%s)",
        amount,
        pay_to,
        network,
        resource_url or "unspecified",
    )

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            payload = create_signed_payment_payload(requirements, cfg=cfg)
            settlement = await verify_and_settle(
                cfg,
                payment_payload=payload,
                payment_requirements=requirements,
                client=client,
            )
    except X402Error as e:
        logger.warning("x402 agent payment failed: %s", e.message)
        return _error(502, "payment_failed", e.message, detail=e.detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("x402 agent payment unexpected error: %s", e)
        return _error(502, "payment_failed", str(e)[:300])

    logger.info(
        "x402 agent payment settled: tx=%s payer=%s",
        settlement.get("transaction"),
        settlement.get("payer"),
    )
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            # Send this back as the PAYMENT-SIGNATURE header on the retry.
            "paymentSignature": encode_settlement_response(payload),
            "settlement": settlement,
            "transaction": settlement.get("transaction"),
            "payer": settlement.get("payer"),
            "amount": str(amount),
            "asset": str(requirements.get("asset") or cfg.asset),
            "network": network,
            "payTo": pay_to,
        },
    )


@router.get("/v1/_freeride/x402/policy")
async def x402_policy() -> dict[str, Any]:
    """What an agent is allowed to spend, without exposing the key.

    Lets a caller decide whether to even attempt a payment (and show the
    operator a budget) instead of discovering the ceiling via a 403.
    """
    cfg = load_x402_config()
    allowed = sorted(_allowed_payees())
    return {
        "agent_pay_enabled": _agent_pay_enabled(),
        "x402_enabled": cfg.enabled,
        "payer_configured": has_payer_credentials(),
        "network": cfg.network,
        "asset": cfg.asset,
        "max_amount": str(_max_amount()),
        "allowed_payees": allowed,
        "facilitator": cfg.facilitator,
        "dry_run": cfg.dry_run,
        "header_payment_required": HEADER_PAYMENT_REQUIRED,
    }
