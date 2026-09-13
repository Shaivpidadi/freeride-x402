"""Hedera x402 paid lane (Blocky402 facilitator).

FreeRide keeps free inference. When free providers/keys cannot serve,
this module builds an HTTP 402 PaymentRequired challenge and verifies
+ settles PAYMENT-SIGNATURE via the Blocky402 facilitator before
forwarding via a paid OpenRouter key.

Wire format (x402 v2):
- 402 response header ``PAYMENT-REQUIRED`` = base64(JSON PaymentRequired)
- Client retry header ``PAYMENT-SIGNATURE`` = base64(JSON PaymentPayload)
  (also accept legacy ``X-PAYMENT``)
- Success header ``PAYMENT-RESPONSE`` = base64(JSON SettlementResponse)
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_LEGACY = "X-PAYMENT"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"

DEFAULT_FACILITATOR = "https://api.testnet.blocky402.com"
DEFAULT_NETWORK = "hedera:testnet"
DEFAULT_AMOUNT = "100000"
DEFAULT_ASSET = "0.0.0"
DEFAULT_FEE_PAYER = "0.0.7162784"
DEFAULT_MAX_TIMEOUT = 300


class X402Error(Exception):
    """Payment verify/settle or config failure."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True, slots=True)
class X402Config:
    enabled: bool
    pay_to: str
    network: str
    amount: str
    asset: str
    facilitator: str
    fee_payer: str | None
    resource_url: str | None
    paid_openrouter_api_key: str | None
    dry_run: bool
    force_paid: bool

    @property
    def ready(self) -> bool:
        """Enabled and pay_to configured (required to challenge)."""
        return self.enabled and bool(self.pay_to)


def load_x402_config() -> X402Config:
    """Load x402 settings from process env."""
    enabled = os.environ.get("FREERIDE_X402_ENABLED", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )
    force_paid = os.environ.get("FREERIDE_X402_FORCE_PAID", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )
    dry_run = os.environ.get("FREERIDE_X402_DRY_RUN", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )
    paid_key = (
        os.environ.get("FREERIDE_X402_PAID_OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
        or None
    )
    fee_payer = os.environ.get("FREERIDE_X402_FEE_PAYER", "").strip() or None
    resource_url = os.environ.get("FREERIDE_X402_RESOURCE_URL", "").strip() or None
    return X402Config(
        enabled=enabled,
        pay_to=os.environ.get("FREERIDE_X402_PAY_TO", "").strip(),
        network=os.environ.get("FREERIDE_X402_NETWORK", DEFAULT_NETWORK).strip()
        or DEFAULT_NETWORK,
        amount=os.environ.get("FREERIDE_X402_AMOUNT", DEFAULT_AMOUNT).strip()
        or DEFAULT_AMOUNT,
        asset=os.environ.get("FREERIDE_X402_ASSET", DEFAULT_ASSET).strip()
        or DEFAULT_ASSET,
        facilitator=os.environ.get("FREERIDE_X402_FACILITATOR", DEFAULT_FACILITATOR).strip()
        or DEFAULT_FACILITATOR,
        fee_payer=fee_payer,
        resource_url=resource_url,
        paid_openrouter_api_key=paid_key,
        dry_run=dry_run,
        force_paid=force_paid,
    )


def _b64_json_encode(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _b64_json_decode(header_value: str) -> dict[str, Any]:
    try:
        padded = header_value.strip()
        raw = base64.b64decode(padded, validate=False)
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise X402Error(f"Invalid payment header encoding: {e}") from e
    if not isinstance(data, dict):
        raise X402Error("Payment header JSON must be an object")
    return data


def encode_payment_required(payment_required: dict[str, Any]) -> str:
    return _b64_json_encode(payment_required)


def decode_payment_payload(header_value: str) -> dict[str, Any]:
    return _b64_json_decode(header_value)


def encode_settlement_response(settlement: dict[str, Any]) -> str:
    return _b64_json_encode(settlement)


def extract_payment_header(headers: Any) -> str | None:
    """Return PAYMENT-SIGNATURE or legacy X-PAYMENT value if present."""
    # Starlette Headers is case-insensitive.
    for name in (HEADER_PAYMENT_SIGNATURE, HEADER_PAYMENT_LEGACY):
        val = headers.get(name) if hasattr(headers, "get") else None
        if val:
            return val
    return None


async def resolve_fee_payer(cfg: X402Config, *, client: httpx.AsyncClient | None = None) -> str:
    """Return feePayer from env override or facilitator GET /supported."""
    if cfg.fee_payer:
        return cfg.fee_payer
    own = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http.get(f"{cfg.facilitator.rstrip('/')}/supported")
        resp.raise_for_status()
        data = resp.json()
        kinds = data.get("kinds") or []
        for kind in kinds:
            if not isinstance(kind, dict):
                continue
            if kind.get("network") == cfg.network:
                extra = kind.get("extra") or {}
                if isinstance(extra, dict) and extra.get("feePayer"):
                    return str(extra["feePayer"])
        signers = data.get("signers") or {}
        # Prefer hedera:* then network-specific.
        for key in ("hedera:*", cfg.network):
            vals = signers.get(key)
            if isinstance(vals, list) and vals:
                return str(vals[0])
    except Exception as e:
        logger.warning("x402 /supported fetch failed, using default feePayer: %s", e)
    finally:
        if own:
            await http.aclose()
    return DEFAULT_FEE_PAYER


def build_accepts(
    cfg: X402Config,
    *,
    fee_payer: str,
) -> list[dict[str, Any]]:
    return [
        {
            "scheme": "exact",
            "network": cfg.network,
            "amount": cfg.amount,
            "payTo": cfg.pay_to,
            "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT,
            "asset": cfg.asset,
            "extra": {"feePayer": fee_payer},
        }
    ]


def build_payment_required(
    cfg: X402Config,
    *,
    resource_url: str,
    fee_payer: str,
    error: str = "Free providers exhausted; pay for premium inference",
) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": cfg.resource_url or resource_url,
            "description": "FreeRide premium inference when free tier cannot serve",
            "mimeType": "application/json",
        },
        "accepts": build_accepts(cfg, fee_payer=fee_payer),
        "extensions": {},
    }


def payment_requirements_from_required(payment_required: dict[str, Any]) -> dict[str, Any]:
    accepts = payment_required.get("accepts") or []
    if not accepts:
        raise X402Error("PaymentRequired has empty accepts")
    first = accepts[0]
    if not isinstance(first, dict):
        raise X402Error("Invalid accepts entry")
    return first


async def build_402_response(
    cfg: X402Config,
    *,
    resource_url: str,
    free_detail: dict[str, Any] | None = None,
    error: str = "Free providers exhausted; pay for premium inference",
    client: httpx.AsyncClient | None = None,
) -> JSONResponse:
    """Build a 402 JSONResponse with PAYMENT-REQUIRED header."""
    fee_payer = await resolve_fee_payer(cfg, client=client)
    payment_required = build_payment_required(
        cfg, resource_url=resource_url, fee_payer=fee_payer, error=error
    )
    body: dict[str, Any] = {
        "error": {
            "type": "payment_required",
            "message": error,
            "x402_version": 2,
            "network": cfg.network,
            "amount": cfg.amount,
            "asset": cfg.asset,
            "pay_to": cfg.pay_to,
        }
    }
    if free_detail is not None:
        body["free_exhaustion"] = free_detail
    return JSONResponse(
        status_code=402,
        content=body,
        headers={HEADER_PAYMENT_REQUIRED: encode_payment_required(payment_required)},
    )


def attach_payment_response(
    response: JSONResponse,
    settlement: dict[str, Any],
) -> JSONResponse:
    """Attach PAYMENT-RESPONSE header to a successful paid-lane response."""
    response.headers[HEADER_PAYMENT_RESPONSE] = encode_settlement_response(settlement)
    response.headers["X-FreeRide-Paid"] = "hedera-x402"
    response.headers["X-FreeRide-Lane"] = "paid"
    return response


async def verify_and_settle(
    cfg: X402Config,
    *,
    payment_payload: dict[str, Any],
    payment_requirements: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Verify then settle with the facilitator. Returns SettlementResponse.

    When ``FREERIDE_X402_DRY_RUN=1``, skips real HTTP and returns a fake
    settlement. Still requires a decoded payment payload object.
    """
    if cfg.dry_run:
        payer = None
        if isinstance(payment_payload, dict):
            # Best-effort: dry-run clients may put a marker payer.
            accepted = payment_payload.get("accepted") or {}
            payer = payment_payload.get("payer") or accepted.get("payTo")
        return {
            "success": True,
            "transaction": "dry-run.0.0.0@0.0",
            "network": cfg.network,
            "payer": payer or "dry-run",
            "dryRun": True,
        }

    body = {
        "x402Version": 2,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    }
    own = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    base = cfg.facilitator.rstrip("/")
    try:
        verify_resp = await http.post(f"{base}/verify", json=body)
        if verify_resp.status_code >= 400:
            raise X402Error(
                f"Facilitator verify HTTP {verify_resp.status_code}",
                detail={"body": _safe_json(verify_resp)},
            )
        verification = verify_resp.json()
        if not isinstance(verification, dict) or not verification.get("isValid"):
            if isinstance(verification, dict):
                reason = (
                    verification.get("invalidReason")
                    or verification.get("invalidMessage")
                    or "verification_failed"
                )
            else:
                reason = "verification_failed"
            raise X402Error(
                f"Payment verification failed: {reason}",
                detail={"verification": verification},
            )

        settle_resp = await http.post(f"{base}/settle", json=body)
        if settle_resp.status_code >= 400:
            raise X402Error(
                f"Facilitator settle HTTP {settle_resp.status_code}",
                detail={"body": _safe_json(settle_resp)},
            )
        settlement = settle_resp.json()
        if not isinstance(settlement, dict) or not settlement.get("success"):
            if isinstance(settlement, dict):
                reason = (
                    settlement.get("errorReason")
                    or settlement.get("errorMessage")
                    or "settlement_failed"
                )
            else:
                reason = "settlement_failed"
            raise X402Error(
                f"Payment settlement failed: {reason}",
                detail={"settlement": settlement},
            )
        return settlement
    finally:
        if own:
            await http.aclose()


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return (resp.text or "")[:500]


def paid_openrouter_key(cfg: X402Config) -> str | None:
    return cfg.paid_openrouter_api_key


# ---------------------------------------------------------------------------
# Daemon-side auto-pay (payer keys live in ~/.freeride/.env)
# ---------------------------------------------------------------------------


DEMO_WALLET_FILENAME = "demo-wallet.json"


def _demo_wallet_path() -> Path:
    return Path(__file__).resolve().parents[1] / "x402" / DEMO_WALLET_FILENAME


def bundled_demo_payer() -> tuple[str | None, str | None]:
    """The demo payer shipped with the package, if one is bundled.

    Ships so a judge can watch the cash lane work without creating a Hedera
    account first. It is testnet-only and its key is public by definition, so
    it is a fallback, never a preference: a real payer in the environment
    always wins, and `freeride wallet status` says which one is in use.
    """
    path = _demo_wallet_path()
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("bundled demo wallet is unreadable (%s): %s", path, e)
        return None, None
    if not isinstance(data, dict):
        return None, None
    account = str(data.get("account_id") or "").strip() or None
    key = str(data.get("private_key_hex") or data.get("private_key_der") or "").strip() or None
    return account, key


def demo_wallet_in_use() -> bool:
    """True when the payer being used is the bundled demo one."""
    if _env_payer() != (None, None):
        return False
    account, key = bundled_demo_payer()
    return bool(account and key) and _demo_wallet_allowed()


def _demo_wallet_allowed() -> bool:
    """Off only when explicitly disabled, so the demo works out of the box."""
    return os.environ.get("FREERIDE_X402_DEMO_WALLET", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _env_payer() -> tuple[str | None, str | None]:
    account = (
        os.environ.get("FREERIDE_X402_PAYER_ACCOUNT", "").strip()
        or os.environ.get("HEDERA_ACCOUNT_ID", "").strip()
        or None
    )
    key = (
        os.environ.get("FREERIDE_X402_PAYER_KEY", "").strip()
        or os.environ.get("HEDERA_PRIVATE_KEY", "").strip()
        or None
    )
    return (account, key) if (account and key) else (None, None)


def load_payer_credentials() -> tuple[str | None, str | None]:
    """Return (account_id, private_key) from env aliases.

    Preferred: ``FREERIDE_X402_PAYER_ACCOUNT`` / ``FREERIDE_X402_PAYER_KEY``.
    Also accepts ``HEDERA_ACCOUNT_ID`` / ``HEDERA_PRIVATE_KEY``.
    """
    account, key = _env_payer()
    if account and key:
        return account, key
    if not _demo_wallet_allowed():
        return None, None
    demo_account, demo_key = bundled_demo_payer()
    if demo_account and demo_key:
        logger.warning(
            "Using the BUNDLED DEMO payer %s (testnet). Its key ships in the package "
            "and is public. Run `freeride wallet setup` for your own, or set "
            "FREERIDE_X402_DEMO_WALLET=0 to refuse it.",
            demo_account,
        )
        return demo_account, demo_key
    return None, None


def has_payer_credentials() -> bool:
    account, key = load_payer_credentials()
    return bool(account and key)


# Where the Node signer may live. A wheel install has no repo `scripts/`
# next to the package, so a packaged copy inside `freeride/x402/` ships too.
#
# The signer imports `@x402/hedera` as ESM, and Node's ESM resolver walks
# up from the SCRIPT's own directory looking for node_modules — it ignores
# NODE_PATH entirely. So the script must run from a directory that has its
# deps adjacent; we prefer such a location and materialize the packaged
# copy into ~/.freeride/x402-signer when only that has node_modules.
SIGNER_BASENAME = "x402_hedera_sign.mjs"
SIGNER_HOME = Path.home() / ".freeride" / "x402-signer"


def _signer_candidates() -> list[Path]:
    out: list[Path] = []
    override = os.environ.get("FREERIDE_X402_SIGNER", "").strip()
    if override:
        out.append(Path(override).expanduser())
    pkg_root = Path(__file__).resolve().parents[1]  # freeride/
    out.append(pkg_root.parent / "scripts" / SIGNER_BASENAME)  # source checkout
    out.append(SIGNER_HOME / SIGNER_BASENAME)
    out.append(pkg_root / "x402" / SIGNER_BASENAME)  # packaged
    return out


def _has_deps(script: Path) -> bool:
    return (script.parent / "node_modules").is_dir()


def _signer_script_path() -> Path:
    """Pick a signer whose Node deps are reachable, materializing if needed."""
    cands = _signer_candidates()
    existing = [c for c in cands if c.is_file()]
    for c in existing:
        if _has_deps(c):
            return c
    # Nothing has adjacent deps. If the signer home does, copy the packaged
    # script beside them so ESM resolution succeeds.
    if existing and (SIGNER_HOME / "node_modules").is_dir():
        target = SIGNER_HOME / SIGNER_BASENAME
        try:
            SIGNER_HOME.mkdir(parents=True, exist_ok=True)
            target.write_text(existing[0].read_text(encoding="utf-8"), encoding="utf-8")
            return target
        except OSError as e:
            logger.warning("could not stage Hedera signer into %s: %s", SIGNER_HOME, e)
    return existing[0] if existing else cands[-1]


def create_signed_payment_payload(
    payment_requirements: dict[str, Any],
    *,
    cfg: X402Config,
) -> dict[str, Any]:
    """Build a PaymentPayload for ``payment_requirements``.

    ``FREERIDE_X402_DRY_RUN=1`` returns a stub without Node. Live mode
    shells out to ``scripts/x402_hedera_sign.mjs`` (stdin JSON → stdout JSON).
    """
    if cfg.dry_run:
        account, _ = load_payer_credentials()
        return {
            "x402Version": 2,
            "scheme": payment_requirements.get("scheme") or "exact",
            "network": payment_requirements.get("network") or cfg.network,
            "accepted": payment_requirements,
            "payload": {"transaction": "daemon-auto-pay-dry-run"},
            "payer": account or "0.0.dry-run",
        }

    account, key = load_payer_credentials()
    if not account or not key:
        raise X402Error(
            "Auto-pay needs FREERIDE_X402_PAYER_ACCOUNT + FREERIDE_X402_PAYER_KEY "
            "(or HEDERA_ACCOUNT_ID + HEDERA_PRIVATE_KEY)"
        )

    script = _signer_script_path()
    if not script.is_file():
        raise X402Error(
            "Missing Hedera signer helper. Looked in: "
            + ", ".join(str(c) for c in _signer_candidates())
            + ". Set FREERIDE_X402_SIGNER to its path."
        )

    import subprocess

    env = os.environ.copy()
    env["HEDERA_ACCOUNT_ID"] = account
    env["HEDERA_PRIVATE_KEY"] = key
    # Prefer FREERIDE aliases too for the Node helper.
    env["FREERIDE_X402_PAYER_ACCOUNT"] = account
    env["FREERIDE_X402_PAYER_KEY"] = key

    try:
        proc = subprocess.run(
            ["node", str(script)],
            input=json.dumps(payment_requirements).encode("utf-8"),
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        raise X402Error(
            "Node.js is required for live Hedera auto-pay. "
            "Install Node 18+ or set FREERIDE_X402_DRY_RUN=1 for demos."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise X402Error("Hedera signer timed out") from e

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")[:500]
        if "Cannot find package" in err:
            raise X402Error(
                "Hedera signer cannot resolve its Node deps. Install them next "
                f"to the signer: mkdir -p {SIGNER_HOME} && cp "
                f"{script.parent / 'package.json'} {SIGNER_HOME}/ && "
                f"cd {SIGNER_HOME} && npm install"
            )
        raise X402Error(f"Hedera signer failed: {err or f'exit {proc.returncode}'}")

    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except Exception as e:
        raise X402Error(f"Hedera signer returned invalid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise X402Error("Hedera signer must return a JSON object")
    return payload


async def auto_pay_settle(
    cfg: X402Config,
    *,
    resource_url: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Sign (daemon-side) + verify + settle. Returns (payment_payload, settlement)."""
    fee_payer = await resolve_fee_payer(cfg, client=client)
    payment_required = build_payment_required(
        cfg, resource_url=resource_url, fee_payer=fee_payer
    )
    payment_requirements = payment_requirements_from_required(payment_required)
    payment_payload = create_signed_payment_payload(payment_requirements, cfg=cfg)
    settlement = await verify_and_settle(
        cfg,
        payment_payload=payment_payload,
        payment_requirements=payment_requirements,
        client=client,
    )
    return payment_payload, settlement
