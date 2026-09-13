"""Hermetic tests for Hedera x402 paid lane.

Covers PaymentRequired encode/decode, 402 on free exhaustion when enabled,
DRY_RUN paid path with PAYMENT-SIGNATURE, and disabled→503 behavior.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from freeride.core.chat_schema import ChatResponse
from freeride.core.errors import ErrorKind
from freeride.core.health import ProviderHealth
from freeride.core.x402_hedera import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    build_payment_required,
    decode_payment_payload,
    encode_payment_required,
    load_x402_config,
    payment_requirements_from_required,
)
from freeride.server.app import create_app


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FREERIDE_EVENTS", "0")
    monkeypatch.setattr(
        "freeride.core.cooldown.KeyCooldown.available_keys",
        lambda self, name, keys: list(keys),
    )
    ProviderHealth.reset()
    # Clear x402-related env between tests.
    for k in list(__import__("os").environ):
        if k.startswith("FREERIDE_X402") or k.startswith("HEDERA_"):
            monkeypatch.delenv(k, raising=False)
    # A demo payer ships with the package, so "no payer configured" is only
    # true once the bundled one is refused too.
    monkeypatch.setenv("FREERIDE_X402_DEMO_WALLET", "0")
    yield
    ProviderHealth.reset()


class _StubProvider:
    api_version = 1

    def __init__(self, name: str, *, chat_result=None, chat_raises=None):
        self.name = name
        self.embeddings_supported = False
        self._chat = chat_result
        self._raises = chat_raises
        self.forward_chat = AsyncMock(side_effect=self._do_chat)
        self._calls: list[str] = []

    async def _do_chat(self, request, model_id, key):  # noqa: ARG002
        self._calls.append(key)
        if self._raises is not None:
            raise self._raises
        return self._chat

    def classify_error(self, x):  # noqa: ARG002
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response):  # noqa: ARG002
        return None


def _ok_chat(content: str = "paid-hi") -> ChatResponse:
    return ChatResponse.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "x/y",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def _client(providers, monkeypatch, *, env_keys=None) -> TestClient:
    if env_keys:
        for k, v in env_keys.items():
            monkeypatch.setenv(k, v)
    app = create_app(providers=providers)
    return TestClient(app)


def _chat_body(model: str = "openrouter/auto"):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }


def _fake_payment_payload_b64(*, pay_to: str = "0.0.8011510") -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "hedera:testnet",
        "accepted": {
            "scheme": "exact",
            "network": "hedera:testnet",
            "amount": "100000",
            "payTo": pay_to,
            "maxTimeoutSeconds": 300,
            "asset": "0.0.0",
            "extra": {"feePayer": "0.0.7162784"},
        },
        "payload": {"transaction": "dry-run-tx"},
        "payer": "0.0.dry",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Unit: encoding / config
# ---------------------------------------------------------------------------


class TestPaymentRequiredCodec:
    def test_round_trip_payment_required(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
        monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.8011510")
        cfg = load_x402_config()
        pr = build_payment_required(
            cfg,
            resource_url="http://localhost:11343/v1/chat/completions",
            fee_payer="0.0.7162784",
        )
        assert pr["x402Version"] == 2
        assert pr["accepts"][0]["network"] == "hedera:testnet"
        assert pr["accepts"][0]["amount"] == "100000"
        assert pr["accepts"][0]["payTo"] == "0.0.8011510"
        assert pr["accepts"][0]["asset"] == "0.0.0"
        assert pr["accepts"][0]["extra"]["feePayer"] == "0.0.7162784"

        encoded = encode_payment_required(pr)
        decoded = decode_payment_payload(encoded)
        assert decoded["x402Version"] == 2
        assert decoded["accepts"][0]["payTo"] == "0.0.8011510"
        reqs = payment_requirements_from_required(decoded)
        assert reqs["scheme"] == "exact"

    def test_load_config_defaults(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
        monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.9")
        cfg = load_x402_config()
        assert cfg.ready is True
        assert cfg.network == "hedera:testnet"
        assert cfg.amount == "100000"
        assert cfg.facilitator.startswith("https://api.testnet.blocky402.com")


# ---------------------------------------------------------------------------
# Route: free exhaust → 402 / 503
# ---------------------------------------------------------------------------


class TestChatX402Exhaustion:
    def test_402_when_enabled_and_no_usable_keys(self, monkeypatch):
        # Provider registered but no keys → no_usable_keys → 402
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            },
        )
        r = client.post("/v1/chat/completions", json=_chat_body(model="x/y"))
        assert r.status_code == 402
        assert HEADER_PAYMENT_REQUIRED in r.headers
        body = r.json()
        assert body["error"]["type"] == "payment_required"
        assert body["free_exhaustion"]["error"]["type"] == "no_usable_keys"
        pr = decode_payment_payload(r.headers[HEADER_PAYMENT_REQUIRED])
        assert pr["accepts"][0]["payTo"] == "0.0.8011510"
        assert pr["accepts"][0]["network"] == "hedera:testnet"

    def test_503_when_disabled(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        client = _client([provider], monkeypatch)
        r = client.post("/v1/chat/completions", json=_chat_body(model="x/y"))
        assert r.status_code == 503
        assert HEADER_PAYMENT_REQUIRED not in r.headers
        assert r.json()["detail"]["error"]["type"] == "no_usable_keys"

    def test_402_when_all_attempts_exhausted(self, monkeypatch):
        # Key present but forward always fails → all_attempts_exhausted
        import httpx

        req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        resp = httpx.Response(503, request=req)
        err = httpx.HTTPStatusError("boom", request=req, response=resp)

        class _Failing(_StubProvider):
            def classify_error(self, x):  # noqa: ARG002
                return ErrorKind.UNAVAILABLE

        provider = _Failing("openrouter", chat_raises=err)
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "OPENROUTER_API_KEY": "free-key",
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            },
        )
        r = client.post("/v1/chat/completions", json=_chat_body(model="x/y"))
        assert r.status_code == 402
        assert HEADER_PAYMENT_REQUIRED in r.headers
        assert r.json()["error"]["type"] == "payment_required"


class TestChatX402PaidPath:
    def test_dry_run_payment_signature_invokes_paid_path(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat("from-paid"))
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
                "FREERIDE_X402_DRY_RUN": "1",
                "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid-secret-key",
                # Also set free key so free path would work — payment header
                # must skip free and use paid.
                "OPENROUTER_API_KEY": "free-key",
            },
        )
        sig = _fake_payment_payload_b64()
        r = client.post(
            "/v1/chat/completions",
            json=_chat_body(model="x/y"),
            headers={HEADER_PAYMENT_SIGNATURE: sig},
        )
        assert r.status_code == 200, r.text
        assert r.headers.get(HEADER_PAYMENT_RESPONSE)
        assert r.headers.get("X-FreeRide-Paid") == "hedera-x402"
        assert r.headers.get("X-FreeRide-Lane") == "paid"
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "from-paid"
        assert body["_freeride_paid"] == "hedera-x402"
        # Paid key used, not free key.
        assert provider._calls == ["paid-secret-key"]
        settlement = decode_payment_payload(r.headers[HEADER_PAYMENT_RESPONSE])
        assert settlement.get("dryRun") is True
        assert settlement.get("success") is True

    def test_payment_present_but_no_paid_key_after_settle(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        # Clear OPENROUTER fallback by not setting it; paid key unset.
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_DRY_RUN": "1",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            },
        )
        # Ensure paid key env is empty even if process has OPENROUTER.
        monkeypatch.delenv("FREERIDE_X402_PAID_OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        # Re-create client after env clear — load_x402_config reads at request time
        # so existing client is fine as long as env is clear.
        sig = _fake_payment_payload_b64()
        with patch(
            "freeride.server.routes.chat.paid_openrouter_key",
            return_value=None,
        ):
            r = client.post(
                "/v1/chat/completions",
                json=_chat_body(model="x/y"),
                headers={HEADER_PAYMENT_SIGNATURE: sig},
            )
        assert r.status_code == 503
        assert r.json()["detail"]["error"]["type"] == "x402_no_paid_upstream_key"

    def test_invalid_signature_returns_402(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_DRY_RUN": "1",
                "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            },
        )
        r = client.post(
            "/v1/chat/completions",
            json=_chat_body(model="x/y"),
            headers={HEADER_PAYMENT_SIGNATURE: "not-valid-base64-json!!!"},
        )
        assert r.status_code == 402
        assert HEADER_PAYMENT_REQUIRED in r.headers


# ---------------------------------------------------------------------------
# Daemon auto-pay (payer env set → no 402)
# ---------------------------------------------------------------------------


class TestChatX402AutoPay:
    def test_dry_run_auto_pay_when_payer_set(self, monkeypatch):
        """Free exhaust + payer credentials → paid path, not 402."""
        provider = _StubProvider("openrouter", chat_result=_ok_chat("auto-paid"))
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
                "FREERIDE_X402_DRY_RUN": "1",
                "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid-secret-key",
                "FREERIDE_X402_PAYER_ACCOUNT": "0.0.payer",
                "FREERIDE_X402_PAYER_KEY": "dry-run-key",
                # No free keys → free exhaust immediately
            },
        )
        r = client.post("/v1/chat/completions", json=_chat_body(model="x/y"))
        assert r.status_code == 200, r.text
        assert r.headers.get("X-FreeRide-Lane") == "paid"
        assert r.headers.get(HEADER_PAYMENT_RESPONSE)
        assert r.json()["choices"][0]["message"]["content"] == "auto-paid"
        assert provider._calls == ["paid-secret-key"]

    def test_no_payer_still_402(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        client = _client(
            [provider],
            monkeypatch,
            env_keys={
                "FREERIDE_X402_ENABLED": "1",
                "FREERIDE_X402_PAY_TO": "0.0.8011510",
                "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
                "FREERIDE_X402_DRY_RUN": "1",
                "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid-secret-key",
            },
        )
        r = client.post("/v1/chat/completions", json=_chat_body(model="x/y"))
        assert r.status_code == 402
        assert HEADER_PAYMENT_REQUIRED in r.headers


class TestFxX402AutoPay:
    def test_fx_dry_run_auto_pay_no_usable_keys(self, monkeypatch):
        from freeride.core.chat_schema import ChatResponse

        provider = _StubProvider("openrouter", chat_result=_ok_chat("fx-paid"))
        # Ensure x402 env
        for k, v in {
            "FREERIDE_X402_ENABLED": "1",
            "FREERIDE_X402_PAY_TO": "0.0.8011510",
            "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            "FREERIDE_X402_DRY_RUN": "1",
            "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid-secret-key",
            "FREERIDE_X402_PAYER_ACCOUNT": "0.0.payer",
            "FREERIDE_X402_PAYER_KEY": "dry-run-key",
        }.items():
            monkeypatch.setenv(k, v)

        app = create_app(providers=[provider])
        client = TestClient(app)
        body = {
            "prompt": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [],
            "toolChoice": {"type": "auto"},
        }
        r = client.post(
            "/v3/ai/language-model",
            json=body,
            headers={
                "ai-language-model-id": "auto",
                "ai-language-model-streaming": "false",
            },
        )
        assert r.status_code == 200, r.text
        assert r.headers.get("X-FreeRide-Lane") == "paid"
        assert r.headers.get(HEADER_PAYMENT_RESPONSE)
        assert provider._calls == ["paid-secret-key"]

    def test_fx_402_without_payer(self, monkeypatch):
        provider = _StubProvider("openrouter", chat_result=_ok_chat())
        for k, v in {
            "FREERIDE_X402_ENABLED": "1",
            "FREERIDE_X402_PAY_TO": "0.0.8011510",
            "FREERIDE_X402_FEE_PAYER": "0.0.7162784",
            "FREERIDE_X402_DRY_RUN": "1",
            "FREERIDE_X402_PAID_OPENROUTER_API_KEY": "paid",
        }.items():
            monkeypatch.setenv(k, v)
        # Clear any payer leftover
        monkeypatch.delenv("FREERIDE_X402_PAYER_ACCOUNT", raising=False)
        monkeypatch.delenv("FREERIDE_X402_PAYER_KEY", raising=False)
        monkeypatch.delenv("HEDERA_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("HEDERA_PRIVATE_KEY", raising=False)

        app = create_app(providers=[provider])
        client = TestClient(app)
        body = {
            "prompt": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [],
            "toolChoice": {"type": "auto"},
        }
        r = client.post(
            "/v3/ai/language-model",
            json=body,
            headers={
                "ai-language-model-id": "auto",
                "ai-language-model-streaming": "false",
            },
        )
        assert r.status_code == 402
        assert HEADER_PAYMENT_REQUIRED in r.headers


class TestPresetsDoNotTriggerPayment:
    """`freeride/*` presets must be resolved, not shipped upstream verbatim.

    Regression: the chat route only resolved the `auto` sentinels, so a preset
    id travelled to every provider as a literal model name, each 404'd, the
    x402 lane read that as "free exhausted" and settled a payment — for a
    request the paid upstream then rejected with 400. A routing bug that
    charges the user is worth a test.
    """

    @pytest.mark.parametrize(
        "model", ["freeride/coding", "freeride/fast", "freeride/quality", "freeride/free"]
    )
    def test_preset_is_resolved_before_dispatch(self, monkeypatch, model):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        provider = _StubProvider("openrouter", chat_result=_ok_chat("pong"))
        app = create_app(providers=[provider])

        async def _catalog(_providers, group=True):  # noqa: ARG001
            return {"openrouter": ["llama-3.1-8b-instruct"]}

        with (
            patch("freeride.server.routes.chat.get_or_fetch_catalog", new=_catalog),
            patch(
                "freeride.server.routes.chat.resolve_auto_model",
                return_value=("llama-3.1-8b-instruct", "openrouter"),
            ),
        ):
            resp = TestClient(app).post(
                "/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200, resp.text
        # The preset name must never reach a provider as a model id.
        sent = provider.forward_chat.await_args
        assert sent is not None
        assert sent.args[1] == "llama-3.1-8b-instruct"

    def test_preset_does_not_reach_the_cash_lane(self, monkeypatch):
        """With x402 armed, an unresolvable preset must not settle anything."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
        monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.5000")
        monkeypatch.setenv("FREERIDE_X402_PAYER_ACCOUNT", "0.0.4000")
        monkeypatch.setenv("FREERIDE_X402_PAYER_KEY", "0xabc")
        provider = _StubProvider("openrouter", chat_result=_ok_chat("pong"))
        app = create_app(providers=[provider])

        async def _catalog(_providers, group=True):  # noqa: ARG001
            return {"openrouter": ["llama-3.1-8b-instruct"]}

        with (
            patch("freeride.server.routes.chat.get_or_fetch_catalog", new=_catalog),
            patch(
                "freeride.server.routes.chat.resolve_auto_model",
                return_value=("llama-3.1-8b-instruct", "openrouter"),
            ),
            patch("freeride.core.x402_hedera.auto_pay_settle") as settle,
        ):
            resp = TestClient(app).post(
                "/v1/chat/completions",
                json={"model": "freeride/coding", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        settle.assert_not_called()


class TestForcePaidDemoSwitch:
    """FREERIDE_X402_FORCE_PAID skips the free ladder on purpose.

    The cash lane is invisible whenever free works — which is the product
    working as intended, and also why it cannot be shown without sabotaging
    the operator's provider keys. The switch exists for demos, so it must be
    off by default, must not fire without a ready wallet, and must actually
    reach the cash lane on both routes. A NameError in this path once made it
    to runtime because nothing exercised it.
    """

    def _armed(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
        monkeypatch.setenv("FREERIDE_X402_PAY_TO", "0.0.5000")
        monkeypatch.setenv("FREERIDE_X402_PAYER_ACCOUNT", "0.0.4000")
        monkeypatch.setenv("FREERIDE_X402_PAYER_KEY", "0xabc")
        monkeypatch.setenv("FREERIDE_X402_PAID_OPENROUTER_API_KEY", "sk-paid")

    def test_off_by_default(self, monkeypatch):
        from freeride.core.x402_hedera import load_x402_config

        assert load_x402_config().force_paid is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "YES"])
    def test_parses_truthy_values(self, monkeypatch, value):
        from freeride.core.x402_hedera import load_x402_config

        monkeypatch.setenv("FREERIDE_X402_FORCE_PAID", value)
        assert load_x402_config().force_paid is True

    def test_chat_route_pays_without_trying_free(self, monkeypatch):
        self._armed(monkeypatch)
        monkeypatch.setenv("FREERIDE_X402_FORCE_PAID", "1")
        # A provider that would happily serve for free; it must not be asked.
        provider = _StubProvider("openrouter", chat_result=_ok_chat("free-answer"))
        app = create_app(providers=[provider])

        async def _settle(cfg, **kwargs):  # noqa: ARG001
            return ({"payer": "0.0.4000"}, {"success": True, "transaction": "0.0.1@2.3"})

        async def _catalog(_providers, group=True):  # noqa: ARG001
            return {"openrouter": ["llama-3.1-8b-instruct"]}

        with (
            patch("freeride.server.routes.chat.get_or_fetch_catalog", new=_catalog),
            patch(
                "freeride.server.routes.chat.resolve_auto_model",
                return_value=("llama-3.1-8b-instruct", "openrouter"),
            ),
            patch("freeride.server.routes.chat.auto_pay_settle", new=_settle),
            patch(
                "freeride.server.routes.chat._complete_paid_lane",
                new=AsyncMock(return_value=JSONResponse(status_code=200, content={"paid": True})),
            ) as paid,
        ):
            resp = TestClient(app).post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        paid.assert_awaited()
        provider.forward_chat.assert_not_awaited()

    def test_does_not_fire_without_a_ready_wallet(self, monkeypatch):
        """No pay_to means no cash lane; the switch must not strand the request."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("FREERIDE_X402_FORCE_PAID", "1")
        monkeypatch.setenv("FREERIDE_X402_ENABLED", "1")
        provider = _StubProvider("openrouter", chat_result=_ok_chat("free-answer"))
        app = create_app(providers=[provider])

        async def _catalog(_providers, group=True):  # noqa: ARG001
            return {"openrouter": ["llama-3.1-8b-instruct"]}

        with (
            patch("freeride.server.routes.chat.get_or_fetch_catalog", new=_catalog),
            patch(
                "freeride.server.routes.chat.resolve_auto_model",
                return_value=("llama-3.1-8b-instruct", "openrouter"),
            ),
        ):
            resp = TestClient(app).post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            )

        # Falls through to the free ladder rather than failing.
        assert resp.status_code == 200
        provider.forward_chat.assert_awaited()

    def test_fx_route_reaches_the_cash_lane(self, monkeypatch):
        """Guards the exact NameError that shipped: the helper must exist."""
        from freeride.server.routes import fx as fx_route

        assert hasattr(fx_route, "_fx_maybe_cash_lane")
        source = __import__("inspect").getsource(fx_route.fx_chat)
        assert "force_paid" in source
        assert "_fx_maybe_cash_lane" in source
