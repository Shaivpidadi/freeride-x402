# FreeRide

**Free AI inference until it runs out — then the agent pays for itself on Hedera.**

Coding agents run on free inference tiers, and free tiers run out mid-task. The
usual answers are a paid gateway you have to adopt, or a bare x402 meter you
have to wire up yourself. FreeRide is neither: it is a local gateway that keeps
the free tier working across six providers, and when free genuinely cannot
serve, the agent buys the inference it needs — itself, on Hedera, without
leaving the loop.

Free stays free. Paying is the escape hatch.

```
  you ──▶ agent ──▶ FreeRide daemon ──free failover──▶ 6 providers
                          │
                    free exhausted
                          │
                          ▼
              sign x402 (Hedera SDK)
                          │
                    Blocky402 /verify + /settle
                          │
                          ▼
                 paid inference · answer continues
```

---

## What is in here

| | |
|---|---|
| **`FreeRideV3/`** | The gateway. A local daemon on `:11343` that fans requests across OpenRouter, Groq, NVIDIA NIM, HuggingFace, Cerebras, Cloudflare and your own Ollama, with automatic failover — and the Hedera x402 cash lane when free cannot serve. Python / FastAPI. |
| **`ridex/`** | The agent. A native terminal coding agent (a fork of [vercel-labs/fx](https://github.com/vercel-labs/fx)) that reads files, writes code and runs commands, with every model call served through FreeRide. Zig. |
| **`eve-bot/`** | The Bots. Always-on AI teammates, each with its own computer and browser, that can **buy** things mid-job inside a budget — with a human approval card above it. Next.js + [eve](https://www.npmjs.com/package/eve). |

Both the agent and the gateway answer to one command: **`freeride`**.

---

## How Hedera is used

The cash lane speaks **x402 v2** over HTTP and settles on **Hedera testnet**
through the [Blocky402](https://blocky402.com) facilitator.

1. Free providers are tried first. Always.
2. When they cannot serve, the daemon builds an x402 `PAYMENT-REQUIRED`
   challenge and signs a payment with the Hedera SDK (`@x402/hedera`,
   ECDSA secp256k1).
3. Blocky402 `/verify` then `/settle` puts a `CRYPTOTRANSFER` on Hedera
   testnet: the agent's wallet debited, the merchant credited, the facilitator
   covering the network fee.
4. Only then does the paid inference run. The response carries
   `PAYMENT-RESPONSE`, `X-FreeRide-Lane: paid`, and the transaction id.

| | |
|---|---|
| Wire | x402 v2 — `PAYMENT-REQUIRED` → `PAYMENT-SIGNATURE` → `PAYMENT-RESPONSE` |
| Network | `hedera:testnet` |
| Facilitator | `https://api.testnet.blocky402.com` |
| Scheme | `exact`, HBAR (`0.0.0`) |
| Demo price | 100,000 tinybars = 0.001 HBAR per request |

The payer key lives in `~/.freeride/.env` and **never leaves the daemon**. An
agent asks the daemon to pay; it never sees the key. That matters because a Bot
reads inboxes and web pages, so it is a prompt-injection target: the worst a
compromised one can do is request a payment that three independent limits still
have to agree to.

---

## Quickstart

Requires Python 3.10+, and Node 18+ for the Hedera signer.

```bash
curl -sSL https://api.free-ride.xyz/ridex.sh | sh   # agent + gateway + signer deps
freeride                                            # interactive session
freeride ask "read vendors.csv and find the cheapest option"
```

A funded testnet payer **ships with the package**, so the cash lane works with
no setup at all. It is a fallback, never a preference — your own payer always
wins, `freeride wallet status` says which is in use, and
`FREERIDE_X402_DEMO_WALLET=0` refuses it. Its key is public by definition:
testnet only, demo amounts only.

Your own wallet:

```bash
freeride wallet setup      # payer account, ECDSA key, pay_to
freeride wallet status     # never prints the key
```

### Seeing the cash lane

Free-first means the paid lane is invisible whenever free works — correct
behaviour, and the reason it is hard to demo. So there is a switch:

```bash
freeride paid on           # or /paid on inside a session
freeride ask "..."         # settles on Hedera; free providers skipped
freeride paid off
```

A paid turn says what it cost, in its own footer:

```
  37s (↑22 ↓120) · 0.001 HBAR
```

`/paid` prints the receipt with the transaction id. It is process-local: a
restart returns to free-first, so an abandoned demo cannot leave the wallet
spending.

---

## Bots that can buy things

`eve-bot/` is the second half of the argument: the wallet is not a feature of
one coding agent, it is infrastructure any agent can spend through.

A Bot gets an `x402_fetch` tool. It fetches a URL, and if the answer is 402 it
settles on Hedera and retries. The interesting part is the policy, which is
eve's own approval hook:

```
under the job's budget  →  pay, keep working
over it, or a new payee →  park on an approval card, wait for a human
```

A bot that wakes you for every cent is not autonomous; one that can spend
without limit is not safe. Three limits apply, smallest wins:

| Limit | Where | Default |
|---|---|---|
| Per call | `maxAmountTinybars` on the tool call | 0.001 HBAR |
| Per job | `BOT_X402_JOB_BUDGET_TINYBARS` | 0.01 HBAR |
| Per payment | `FREERIDE_X402_MAX_AMOUNT` (daemon) | 0.01 HBAR |

Plus an optional payee allowlist. Every payment lands in the job's activity
feed in green, with a HashScan link, and records whether it was authorised by
policy or by a human.

Agent payments are **off** until you turn them on — a wallet that pays for
inference does not implicitly hand every local process a spending limit:

```bash
freeride wallet budget --max-amount 0.05hbar --agent-pay on
```

Run the lane end to end without standing up the whole console:

```bash
cd eve-bot
node demo/x402-origin.mjs &                          # a paywalled resource
BOT_STORE=fs BOT_DATA_DIR=.data npx tsx demo/x402-buy.ts
```

---

## Demo

`DEMO.md` is the shot-by-shot runbook. The short version:

```bash
freeride ask "..."      # free, six providers, nothing spent
freeride paid on
freeride ask "..."      # same answer, 0.001 HBAR, green footer, tx id
```

Then paste the transaction into
`https://hashscan.io/testnet/transaction/<id>` and watch the transfer list.

---

## Status and honesty

- **Testnet only.** Nothing here has been pointed at mainnet.
- The demo price (0.001 HBAR) is **below the network fee** the facilitator pays
  to settle it. It is a demo price, not a business model. Real margin would be
  a COGS floor or premium tiers on the same 402 gate.
- `demo/x402-origin.mjs` is a paywalled *seller* for the demo. It treats the
  presence of a payment as proof; a real merchant would `/verify` with the
  facilitator first. The buyer side is fully real.
- `FREERIDE_X402_DRY_RUN=1` exists for tests. It moves no HBAR and must never
  be enabled anywhere that should collect real payment.

## Tests

```bash
cd FreeRideV3 && pytest -q      # 939
cd ridex      && zig build test # 8337/8344
cd eve-bot    && npm run typecheck
```

The five failing agent tests are pre-existing and environmental: macOS
Terminal's shell-session restore writes to stdout, which two `run_command`
suites assert is clean.

## Licence

FreeRide: MIT. `ridex` is a fork of vercel-labs/fx, Apache-2.0 — see
`ridex/LICENSE` and `ridex/THIRD_PARTY_NOTICES.md`.
