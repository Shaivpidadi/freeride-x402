# ETHOnline: FreeRide Hedera x402 paid lane

## Honest architecture

FreeRide is still a **free-first** local gateway. Free inference and free-tier failover do not go away.

When free providers/keys **cannot serve** (no usable keys, all attempts exhausted, or stream pre-first-chunk failure), and x402 is enabled, FreeRide offers a **paid cash lane** via Hedera + [Blocky402](https://blocky402.com).

### Seamless path (primary UX — ridex)

If payer credentials are in `~/.freeride/.env`, the **daemon auto-pays**:

1. Free exhausts on chat **or fx** (`POST /v3/ai/language-model`).
2. Gateway signs with local payer key (`scripts/x402_hedera_sign.mjs` / `@x402/hedera`).
3. Blocky402 `/verify` + `/settle`.
4. Paid OpenRouter serves the request.
5. Response includes `PAYMENT-RESPONSE` + `X-FreeRide-Lane: paid`.
6. Client (ridex) sees a normal success — no crypto UI.

### Classic 402 path (no payer key)

1. Respond **402** with `PAYMENT-REQUIRED`.
2. Client signs and retries with `PAYMENT-SIGNATURE`.
3. Same verify/settle → paid OpenRouter.

```
┌─────────┐   free     ┌──────────────┐   free keys    ┌─────────────┐
│  ridex  │ ─────────► │ FreeRide     │ ─────────────► │ OpenRouter/ │
│ / agent │            │ chat + fx    │                │ Groq/NIM/…  │
└─────────┘            └──────┬───────┘                └─────────────┘
                              │ free exhausted + x402 on
                              ▼
                    payer in ~/.freeride/.env?
                     /                    \
                   yes                     no
                    │                       │
                    ▼                       ▼
            auto-sign + settle        402 PAYMENT-REQUIRED
                    │                       │
                    ▼                       ▼
            paid OpenRouter           client signs + retry
                    │                       │
                    └───────────┬───────────┘
                                ▼
                     200 + PAYMENT-RESPONSE
                     X-FreeRide-Lane: paid
```

## Wallet CLI

```bash
freeride wallet status   # enabled, pay_to, payer set?, dry_run (never prints key)
freeride wallet setup    # account id + ECDSA key + pay_to → ~/.freeride/.env
freeride doctor          # includes an x402/wallet line when relevant
```

Aliases accepted for payer env:

| Preferred | Also accepted |
|-----------|---------------|
| `FREERIDE_X402_PAYER_ACCOUNT` | `HEDERA_ACCOUNT_ID` |
| `FREERIDE_X402_PAYER_KEY` | `HEDERA_PRIVATE_KEY` |

## x402 v2 HTTP wire

| Step | Header | Value |
|------|--------|--------|
| Server challenge | `PAYMENT-REQUIRED` | base64(JSON PaymentRequired) |
| Client payment | `PAYMENT-SIGNATURE` | base64(JSON PaymentPayload) (also `X-PAYMENT`) |
| Server settlement | `PAYMENT-RESPONSE` | base64(JSON SettlementResponse) |
| Lane marker | `X-FreeRide-Lane` | `paid` on successful cash-lane responses |

## Environment

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `FREERIDE_X402_ENABLED` | to enable | off | Set `1` to enable cash lane |
| `FREERIDE_X402_PAY_TO` | when enabled | — | Hedera account receiving payment |
| `FREERIDE_X402_PAYER_ACCOUNT` | for auto-pay | — | Local payer account |
| `FREERIDE_X402_PAYER_KEY` | for auto-pay | — | ECDSA private key (never logged) |
| `FREERIDE_X402_NETWORK` | no | `hedera:testnet` | CAIP-2 network |
| `FREERIDE_X402_AMOUNT` | no | `100000` | tinybars (0.001 HBAR) |
| `FREERIDE_X402_ASSET` | no | `0.0.0` | HBAR |
| `FREERIDE_X402_FACILITATOR` | no | `https://api.testnet.blocky402.com` | Blocky402 |
| `FREERIDE_X402_FEE_PAYER` | no | from `/supported` | Facilitator co-signer |
| `FREERIDE_X402_RESOURCE_URL` | no | request URL | Resource.url in challenge |
| `FREERIDE_X402_PAID_OPENROUTER_API_KEY` | for paid inference | falls back to `OPENROUTER_API_KEY` | Upstream after settle |
| `FREERIDE_X402_DRY_RUN` | no | off | Skip real verify/settle; stub signer without Node |

### Dry-run warning

`FREERIDE_X402_DRY_RUN=1` is for **unit tests and local demos without a wallet**. It does **not** move HBAR. Never enable in a public deployment that should collect real payment.

## Signing helper

Live auto-pay shells out to:

```bash
cd scripts && npm install   # once: @x402/hedera
# Python feeds paymentRequirements JSON on stdin → PaymentPayload on stdout
node scripts/x402_hedera_sign.mjs
```

Requires Node 18+. Dry-run skips Node entirely.

## Code map

- `freeride/core/x402_hedera.py` — config, PaymentRequired, facilitator, auto-pay sign/settle
- `freeride/server/routes/chat.py` — free failover → auto-pay or 402 → paid OpenRouter
- `freeride/server/routes/fx.py` — same cash lane for ridex (`/v3/ai/language-model`)
- `freeride/cli/cmd_wallet.py` — `freeride wallet status|setup`
- `scripts/x402_hedera_sign.mjs` — ExactHederaScheme signer
- `examples/ethonline-hedera-agent/` — Node demo consumer (classic 402 client path)
- `tests/test_x402_hedera.py` — hermetic pytest

## Streaming limitation (MVP)

- **Chat**: paid lane forces non-streaming completion.
- **fx**: if payment is needed **before** the first chunk (no usable keys / payment-upfront / non-stream exhaustion), auto-pay runs, then:
  - non-stream clients get JSON;
  - stream clients get a **one-shot SSE wrap** of the paid completion (so ridex keeps working).
- If free streaming already shipped headers/chunks and then dies mid-stream, HTTP 402 is no longer possible (same as other mid-stream limits). Prefer auto-pay on the next turn.

## Demo script (judges)

### A. Dry-run auto-pay (seamless)

```bash
export FREERIDE_X402_ENABLED=1
export FREERIDE_X402_PAY_TO=0.0.8011510
export FREERIDE_X402_PAYER_ACCOUNT=0.0.dry
export FREERIDE_X402_PAYER_KEY=dry-run-not-used
export FREERIDE_X402_DRY_RUN=1
export FREERIDE_X402_PAID_OPENROUTER_API_KEY=sk-or-v1-...
# unset free keys to force cash lane:
# unset OPENROUTER_API_KEY GROQ_API_KEY …
freeride serve
```

A chat or fx request should return **200** with `X-FreeRide-Lane: paid` (not 402).

### B. Classic 402 (no payer)

Same as A but **without** payer env → HTTP 402 + `PAYMENT-REQUIRED`, then use `examples/ethonline-hedera-agent`.

### C. Live Hedera testnet

1. `freeride wallet setup` (payer + pay_to).
2. `cd scripts && npm install`.
3. Leave `DRY_RUN` unset; fund payer with testnet HBAR.
4. Exhaust free keys → daemon auto-pays via Blocky402.

## Judge checklist

- [ ] Free path still works when free keys are healthy (no payment).
- [ ] Free exhaustion + x402 on + **payer set** → auto-pay → 200 + `X-FreeRide-Lane: paid`.
- [ ] Free exhaustion + x402 on + **no payer** → HTTP 402 + `PAYMENT-REQUIRED`.
- [ ] fx dialect (`/v3/ai/language-model`) shares the same cash lane.
- [ ] `freeride wallet status|setup` works; doctor mentions x402 when enabled.
- [ ] Facilitator = Blocky402 testnet; scheme `exact`; network `hedera:testnet`.
- [ ] `FREERIDE_X402_DRY_RUN` documented as non-production.
