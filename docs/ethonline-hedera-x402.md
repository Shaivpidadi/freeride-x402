# ETHOnline: FreeRide Hedera x402 paid lane

## Honest architecture

FreeRide is still a **free-first** local gateway. Free inference and free-tier failover do not go away.

When free providers/keys **cannot serve** (no usable keys, all attempts exhausted, or stream pre-first-chunk failure), and x402 is enabled, FreeRide offers a **paid cash lane**:

1. Respond **402** with header `PAYMENT-REQUIRED` = base64(JSON PaymentRequired) for **Hedera testnet** `exact` scheme.
2. Client pays (signs) and retries with `PAYMENT-SIGNATURE` (legacy `X-PAYMENT` also accepted).
3. Gateway **verify + settle** via [Blocky402](https://blocky402.com) facilitator `https://api.testnet.blocky402.com`.
4. On success, forward once via **paid OpenRouter** key and return `PAYMENT-RESPONSE`.

This is **not** a BlockRun-style paid catalog. It is an optional cash escape hatch when free dies.

```
┌─────────┐   free     ┌──────────────┐   free keys    ┌─────────────┐
│  Agent  │ ─────────► │ FreeRide     │ ─────────────► │ OpenRouter/ │
│         │            │ /v1/chat/…   │                │ Groq/NIM/…  │
└─────────┘            └──────┬───────┘                └─────────────┘
                              │ free exhausted + x402 on
                              ▼
                       402 PAYMENT-REQUIRED
                              │
                              ▼
                       Agent signs Hedera exact
                              │
                              ▼
                       PAYMENT-SIGNATURE retry
                              │
                              ▼
                       Blocky402 /verify + /settle
                              │
                              ▼
                       Paid OpenRouter → 200 + PAYMENT-RESPONSE
```

## x402 v2 HTTP wire

| Step | Header | Value |
|------|--------|--------|
| Server challenge | `PAYMENT-REQUIRED` | base64(JSON PaymentRequired) |
| Client payment | `PAYMENT-SIGNATURE` | base64(JSON PaymentPayload) (also `X-PAYMENT`) |
| Server settlement | `PAYMENT-RESPONSE` | base64(JSON SettlementResponse) |

Facilitator body for verify/settle:

```json
{ "x402Version": 2, "paymentPayload": {…}, "paymentRequirements": {…} }
```

`GET /supported` advertises `hedera:testnet` with feePayer `0.0.7162784` (override with env if needed).

## Environment

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `FREERIDE_X402_ENABLED` | to enable | off | Set `1` to enable cash lane |
| `FREERIDE_X402_PAY_TO` | when enabled | — | Hedera account receiving payment |
| `FREERIDE_X402_NETWORK` | no | `hedera:testnet` | CAIP-2 network |
| `FREERIDE_X402_AMOUNT` | no | `100000` | tinybars (0.001 HBAR) |
| `FREERIDE_X402_ASSET` | no | `0.0.0` | HBAR |
| `FREERIDE_X402_FACILITATOR` | no | `https://api.testnet.blocky402.com` | Blocky402 |
| `FREERIDE_X402_FEE_PAYER` | no | from `/supported` | Facilitator co-signer |
| `FREERIDE_X402_RESOURCE_URL` | no | request URL | Resource.url in challenge |
| `FREERIDE_X402_PAID_OPENROUTER_API_KEY` | for paid inference | falls back to `OPENROUTER_API_KEY` | Upstream after settle |
| `FREERIDE_X402_DRY_RUN` | no | off | **Demo/tests only**: skip real verify/settle; still requires `PAYMENT-SIGNATURE`; returns fake settlement |

### Dry-run warning

`FREERIDE_X402_DRY_RUN=1` is for **unit tests and local demos without a wallet**. It does **not** move HBAR. Never enable in a public deployment that should collect real payment.

## Code map

- `freeride/core/x402_hedera.py` — config, PaymentRequired, facilitator client, 402 helpers
- `freeride/server/routes/chat.py` — free failover; on exhaust → 402; on payment header → verify/settle → paid OpenRouter
- `examples/ethonline-hedera-agent/` — Node demo consumer
- `tests/test_x402_hedera.py` — hermetic pytest

## Demo script (judges)

### A. Dry-run (no wallet)

Terminal 1 — gateway:

```bash
export FREERIDE_X402_ENABLED=1
export FREERIDE_X402_PAY_TO=0.0.8011510   # any valid-looking id for challenge
export FREERIDE_X402_DRY_RUN=1
export FREERIDE_X402_PAID_OPENROUTER_API_KEY=sk-or-v1-...
# Optionally unset free keys to force 402 immediately:
# unset OPENROUTER_API_KEY GROQ_API_KEY …
freeride serve   # or your usual serve command
```

Terminal 2 — agent:

```bash
cd examples/ethonline-hedera-agent
npm install
export FREERIDE_URL=http://127.0.0.1:11343
export FREERIDE_X402_AGENT_DRY_RUN=1
npm start
```

### B. Live Hedera testnet

1. Fund a testnet account (HBAR faucet).
2. Set gateway `FREERIDE_X402_PAY_TO` to **your** receiver account; leave `DRY_RUN` unset.
3. Set agent `HEDERA_ACCOUNT_ID` + `HEDERA_PRIVATE_KEY`.
4. Exhaust free keys (or unset them) so the first call gets 402, then agent signs and settles via Blocky402.

## Judge checklist

- [ ] Free path still works when free keys are healthy (no payment).
- [ ] Free exhaustion + x402 on → HTTP 402 + `PAYMENT-REQUIRED` (not a silent paid redirect).
- [ ] Payment retry → verify/settle → paid inference + `PAYMENT-RESPONSE`.
- [ ] Facilitator = Blocky402 testnet; scheme `exact`; network `hedera:testnet`.
- [ ] Product docs still describe FreeRide as free-first (paid = cash lane only).
- [ ] `FREERIDE_X402_DRY_RUN` documented as non-production.

## Blockers / live settle notes

- Live settle needs a real Hedera **payTo** account you control and a funded **payer** for the agent.
- Paid upstream needs a working OpenRouter key with credit (`FREERIDE_X402_PAID_OPENROUTER_API_KEY`).
- MVP paid path is **non-streaming** chat completions only.
