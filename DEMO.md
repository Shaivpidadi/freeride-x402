# Demo runbook

Five minutes, three surfaces, one idea: **free inference until it runs out, then
the agent pays for itself on Hedera.**

Record with QuickTime (File → New Screen Recording) at 1920x1080. Terminal at a
large font — 16pt or more; judges watch on laptops.

---

## Before you hit record

```bash
freeride restart                 # daemon healthy, launchd supervised
freeride paid off                # start on the free lane
freeride doctor                  # everything green, wallet ready
node ~/Desktop/oss/ETHOnline-hedera/eve-bot/demo/x402-origin.mjs &   # the paywall
```

Have a browser tab open on HashScan testnet, and one on the eve-bot console.

Balances worth knowing so you can narrate them:

```bash
curl -s "https://testnet.mirrornode.hedera.com/api/v1/accounts/0.0.10471098" \
  | python3 -c 'import sys,json;print((json.load(sys.stdin).get("balance") or {}).get("balance")/1e8,"HBAR")'
```

---

## Shot 1 — the problem (20s)

Say it, don't show it: coding agents run on free tiers, and free tiers run out
mid-task. The usual answers are a paid gateway you have to adopt, or a bare
x402 meter you have to wire up yourself.

## Shot 2 — free inference works (40s)

```bash
cd ~/freeride-test
freeride ask "reply with the single word pong"
```

Point at the footer: `3s (↑4 ↓10)`. No payment. Six providers behind it, with
failover — this is the product on an ordinary day.

```bash
freeride doctor
```

Six providers keyed, the x402 lane armed but idle.

## Shot 3 — the agent pays for itself (60s)

Open the session:

```bash
freeride
```

Inside it:

```
/paid on
reply with the single word pong
```

The line to land on is the footer:

```
6s (↑6 ↓93) · 0.001 HBAR
```

Green, next to the time and tokens. Say plainly: **all six free providers are
still healthy — it paid because we asked it to, so you can see the lane. In
normal use this fires only when free cannot serve.**

Then:

```
/paid
```

It prints the receipt: amount and Hedera transaction id.

## Shot 4 — the money is real (30s)

Paste the transaction into HashScan:

```
https://hashscan.io/testnet/transaction/<id>
```

Show the transfer list: the payer debited 0.001 HBAR, the merchant credited,
the facilitator covering the network fee. Note the honest detail — the network
fee is larger than the payment, because this is a demo price.

## Shot 5 — it is infrastructure, not one agent's trick (90s)

Open the console at `http://localhost:3200/bot` and give HQ a job:

```
Assign Atlas a job: read the competitor pricing report at
http://127.0.0.1:4021/report and summarise the vendors. It is behind an x402
paywall charging 100000 tinybars — pay it from the team wallet.
```

Watch HQ delegate, Atlas call `x402_fetch`, and the answer come back. Expand
the job's steps and point at the green **Paid** row with its HashScan link.

Then the part that matters. Lower the budget below the price and run it again:

```bash
# in eve-bot/.env
BOT_X402_JOB_BUDGET_TINYBARS=50000
```

The run parks on an approval card showing amount, payee and the bot's stated
reason. Nothing is charged while it waits. Approve it, and only then does the
payment settle — recorded in the feed as `human` rather than `policy`.

Say it: **a bot that wakes you for every cent is not autonomous; one that can
spend without limit is not safe. A budget with a human backstop is what makes
an unattended agent trustworthy with a wallet.**

## Shot 6 — close (20s)

Three limits, smallest wins: per call, per job, per payment at the daemon. The
payer key never leaves the daemon, so a prompt-injected bot can ask for a
payment but cannot take the wallet. Free stays free; paying is the escape
hatch.

---

## If something misbehaves

| Symptom | Cause |
| --- | --- |
| `Safety reviewer unavailable` | old binary; rebuild `zig build` |
| Bot job cancelled | Vercel OIDC expired — `vercel env pull` |
| 402 with no payment | wallet not ready; `freeride wallet status` |
| No green footer | gateway too old; reinstall the daemon |

Turn the demo switch off when you are done:

```bash
freeride paid off
```
