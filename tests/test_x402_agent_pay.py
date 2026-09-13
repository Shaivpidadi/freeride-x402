"""Hermetic tests for the agent-facing x402 payment endpoint.

This route signs real value on behalf of any local caller, so its guards are
the security boundary: everything here asserts that a payment is *refused*
before the signer is ever reached. The happy path is covered too, with the
signer and facilitator stubbed — settling for real belongs in a live demo,
not a unit test.
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from freeride.core.health import ProviderHealth
from freeride.server.app import create_app

PAY_URL = "/v1/_freeride/x402/pay"
POLICY_URL = "/v1/_freeride/x402/policy"


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FREERIDE_EVENTS", "0")
    ProviderHealth.reset()
    for key in list(os.environ):
        if key.startswith("FREERIDE_X402") or key.startswith("HEDERA_"):
            monkeypatch.delenv(key, raising=False)
    # Same reason as tests/test_x402_hedera.py: the bundled demo payer would
    # otherwise satisfy "no payer configured".
    monkeypatch.setenv("FREERIDE_X402_DEMO_WALLET", "0")
    yield
    ProviderHealth.reset()


def _wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured, agent-payable wallet."""
    monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
    monkeypatch.setenv("FREERIDE_X402_AGENT_PAY_ENABLED", "1")
    monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.5000")
    monkeypatch.setenv("FREERIDE_X402_PAYER_ACCOUNT", "0.0.4000")
    monkeypatch.setenv("FREERIDE_X402_PAYER_KEY", "0xabc")


def _client() -> TestClient:
    return TestClient(create_app(providers=[]))


def _requirements(**overrides) -> dict:
    base = {
        "scheme": "exact",
        "network": "hedera:testnet",
        "amount": "100000",
        "payTo": "0.0.5000",
        "maxTimeoutSeconds": 300,
        "asset": "0.0.0",
        "extra": {"feePayer": "0.0.7162784"},
    }
    base.update(overrides)
    return base


def _post(client: TestClient, **body):
    return client.post(PAY_URL, json=body)


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    """A wallet that pays for inference does not implicitly pay for agents."""
    monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
    monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.5000")
    monkeypatch.setenv("FREERIDE_X402_PAYER_ACCOUNT", "0.0.4000")
    monkeypatch.setenv("FREERIDE_X402_PAYER_KEY", "0xabc")
    resp = _post(_client(), paymentRequirements=_requirements())
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "agent_pay_disabled"


def test_requires_payer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
    monkeypatch.setenv("FREERIDE_X402_AGENT_PAY_ENABLED", "1")
    monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.5000")
    resp = _post(_client(), paymentRequirements=_requirements())
    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "no_payer"


def test_amount_above_cap_is_refused(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_MAX_AMOUNT", "100000")
    resp = _post(_client(), paymentRequirements=_requirements(amount="100001"))
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["type"] == "amount_above_cap"
    assert body["error"]["max_amount"] == 100000


def test_amount_at_cap_is_allowed(monkeypatch: pytest.MonkeyPatch):
    """The cap is a ceiling, not an exclusive bound."""
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_MAX_AMOUNT", "100000")
    with (
        patch(
            "freeride.server.routes.x402.create_signed_payment_payload",
            return_value={"x402Version": 2, "payer": "0.0.4000"},
        ),
        patch(
            "freeride.server.routes.x402.verify_and_settle",
            new=AsyncMock(return_value={"success": True, "transaction": "0.0.1@2.3"}),
        ),
    ):
        resp = _post(_client(), paymentRequirements=_requirements(amount="100000"))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_wrong_network_is_refused(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    resp = _post(_client(), paymentRequirements=_requirements(network="eip155:80002"))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "network_not_allowed"


def test_payee_allowlist_is_enforced(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_ALLOWED_PAYEES", "0.0.9999, 0.0.8888")
    resp = _post(_client(), paymentRequirements=_requirements(payTo="0.0.5000"))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "payee_not_allowed"


def test_payee_allowlist_admits_listed_payee(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_ALLOWED_PAYEES", "0.0.5000")
    with (
        patch(
            "freeride.server.routes.x402.create_signed_payment_payload",
            return_value={"x402Version": 2, "payer": "0.0.4000"},
        ),
        patch(
            "freeride.server.routes.x402.verify_and_settle",
            new=AsyncMock(return_value={"success": True, "transaction": "0.0.1@2.3"}),
        ),
    ):
        resp = _post(_client(), paymentRequirements=_requirements())
    assert resp.status_code == 200


@pytest.mark.parametrize("amount", ["-1", "0", "abc", None])
def test_bad_amounts_are_refused(monkeypatch: pytest.MonkeyPatch, amount):
    _wallet(monkeypatch)
    resp = _post(_client(), paymentRequirements=_requirements(amount=amount))
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_requirements"


def test_missing_requirements_is_refused(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    resp = _post(_client(), nonsense=True)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_requirements"


def test_never_signs_when_a_guard_rejects(monkeypatch: pytest.MonkeyPatch):
    """The wallet must not be touched by a request the policy refuses."""
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_MAX_AMOUNT", "1")
    with patch("freeride.server.routes.x402.create_signed_payment_payload") as signer:
        resp = _post(_client(), paymentRequirements=_requirements(amount="500000"))
    assert resp.status_code == 403
    signer.assert_not_called()


def test_accepts_a_full_challenge_header(monkeypatch: pytest.MonkeyPatch):
    """Agents forward the 402's PAYMENT-REQUIRED value verbatim."""
    _wallet(monkeypatch)
    challenge = {
        "x402Version": 2,
        "accepts": [_requirements()],
    }
    encoded = base64.b64encode(json.dumps(challenge).encode()).decode()
    with (
        patch(
            "freeride.server.routes.x402.create_signed_payment_payload",
            return_value={"x402Version": 2, "payer": "0.0.4000"},
        ),
        patch(
            "freeride.server.routes.x402.verify_and_settle",
            new=AsyncMock(return_value={"success": True, "transaction": "0.0.1@2.3"}),
        ),
    ):
        resp = _post(_client(), paymentRequired=encoded)
    assert resp.status_code == 200
    assert resp.json()["payTo"] == "0.0.5000"


def test_returns_signature_and_settlement(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    payload = {"x402Version": 2, "scheme": "exact", "payer": "0.0.4000"}
    with (
        patch(
            "freeride.server.routes.x402.create_signed_payment_payload",
            return_value=payload,
        ),
        patch(
            "freeride.server.routes.x402.verify_and_settle",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "transaction": "0.0.7162784@1.2",
                    "payer": "0.0.4000",
                }
            ),
        ),
    ):
        resp = _post(_client(), paymentRequirements=_requirements(), resourceUrl="https://x.test/a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["transaction"] == "0.0.7162784@1.2"
    assert body["amount"] == "100000"
    # The signature is the payload the caller replays as PAYMENT-SIGNATURE.
    assert json.loads(base64.b64decode(body["paymentSignature"])) == payload


def test_facilitator_failure_surfaces_as_502(monkeypatch: pytest.MonkeyPatch):
    from freeride.core.x402_hedera import X402Error

    _wallet(monkeypatch)
    with (
        patch(
            "freeride.server.routes.x402.create_signed_payment_payload",
            return_value={"x402Version": 2},
        ),
        patch(
            "freeride.server.routes.x402.verify_and_settle",
            new=AsyncMock(side_effect=X402Error("settlement rejected")),
        ),
    ):
        resp = _post(_client(), paymentRequirements=_requirements())
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "payment_failed"


def test_policy_reports_limits_without_leaking_the_key(monkeypatch: pytest.MonkeyPatch):
    _wallet(monkeypatch)
    monkeypatch.setenv("FREERIDE_X402_MAX_AMOUNT", "250000")
    monkeypatch.setenv("FREERIDE_X402_ALLOWED_PAYEES", "0.0.5000")
    body = _client().get(POLICY_URL).json()
    assert body["agent_pay_enabled"] is True
    assert body["payer_configured"] is True
    assert body["max_amount"] == "250000"
    assert body["allowed_payees"] == ["0.0.5000"]
    assert "0xabc" not in json.dumps(body)
