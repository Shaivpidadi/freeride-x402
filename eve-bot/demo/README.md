# Bots that can pay for things

An always-on Bot eventually meets a resource that wants money — a paywalled
dataset, a metered API. This wires eve's approval loop to a Hedera wallet so it
can buy one without waking you for every cent, and without ever being able to
spend without limit.

```
Bot ──▶ x402_fetch ──▶ resource
                          │ 402 PAYMENT-REQUIRED
                          ▼
                   under job budget? ──no──▶ approval card, run parks
                          │ yes
                          ▼
        FreeRide daemon ──sign──▶ Blocky402 ──settle──▶ Hedera
                          │
                          ▼
              retry with PAYMENT-SIGNATURE ──▶ 200 + content
                          │
                          ▼
                 job.paid in the activity feed
```

## Why the key is not here

The Bot never holds the payer key. It asks the local **FreeRide** daemon, which
signs and settles from `~/.freeride/.env`. A Bot reads inboxes and web pages, so
it is a prompt-injection target; the worst a compromised one can do here is ask
for a payment that the daemon's ceiling, the payee allowlist, and the job budget
all still have to agree to.

Three independent limits, smallest wins:

| Limit | Where | Default |
| --- | --- | --- |
| Per-call ceiling | `maxAmountTinybars` on the tool call | 0.001 HBAR |
| Per-job budget | `BOT_X402_JOB_BUDGET_TINYBARS` | 0.01 HBAR |
| Per-payment cap | `FREERIDE_X402_MAX_AMOUNT` (daemon) | 1 HBAR |

Plus `FREERIDE_X402_ALLOWED_PAYEES` on the daemon, if you want to fix who can
ever be paid.

Spend is read back from the activity feed rather than a counter, so the number
the policy enforces is the same one you audit, and it survives a restart.

## Run it

Needs the FreeRide daemon with a funded Hedera testnet payer
(`freeride wallet setup`), and agent payments explicitly turned on — a wallet
that pays for inference does not implicitly pay for agents:

```bash
FREERIDE_X402_AGENT_PAY_ENABLED=1 freeride start
```

Then, in two terminals:

```bash
node demo/x402-origin.mjs                       # a paywalled resource
BOT_STORE=fs BOT_DATA_DIR=.data npx tsx demo/x402-buy.ts
```

`x402-buy.ts` drives the same `agent/lib/x402.ts` the tool uses, so the loop is
real without needing Sandbox, Blob, and the console. Expect:

```
GET http://127.0.0.1:4021/report
  -> 402 Payment Required
  asks 0.001 HBAR -> 0.0.10471098
  within budget (0.001 HBAR of 0.01 HBAR) — paying
  settled: 0.0.7162784@1789258275.656591805
  https://hashscan.io/testnet/transaction/0.0.7162784@1789258275.656591805

GET http://127.0.0.1:4021/report (with PAYMENT-SIGNATURE)
  -> 200
```

Run it again with the budget already spent to watch it refuse:

```bash
BOT_X402_JOB_BUDGET_TINYBARS=100000 ... npx tsx demo/x402-buy.ts
#   OVER BUDGET — in the tool this parks on an operator approval card.
```

## Inside a Bot

`x402_fetch` is a teammate tool, so a job can just use it:

> Pull the Q3 competitor pricing report and summarise it. It's behind a paywall;
> spend up to 0.001 HBAR if you need to.

Under budget the Bot pays and keeps working. Over budget — or the first time a
payee appears — the run parks on an approval card showing amount, payee, and
resource, exactly the way `send_email` parks. Either way the payment lands in
the feed as **Paid**, with the HashScan link.

## What this demo is not

`demo/x402-origin.mjs` is the seller half, and it treats the presence of a
`PAYMENT-SIGNATURE` as proof. A real merchant posts the payload to a
facilitator's `/verify` first. The buyer side is fully real — the settlement is
on testnet and provable on HashScan — but do not put the origin behind anything
you care about.
