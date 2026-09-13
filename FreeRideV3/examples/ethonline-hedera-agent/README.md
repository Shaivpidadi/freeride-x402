# FreeRide × Hedera x402 (ETHOnline demo agent)

Honest product story for judges:

- **Free stays free.** FreeRide still failovers across free-tier providers first.
- **Paid is a cash lane**, not a catalog clone. When free keys/providers cannot serve, the gateway returns HTTP **402** with x402 v2 `PAYMENT-REQUIRED`.
- Client pays on **Hedera testnet** via the **Blocky402** facilitator (`https://api.testnet.blocky402.com`), retries with `PAYMENT-SIGNATURE`, and FreeRide settles then forwards via a **paid OpenRouter** key.

```
Agent → FreeRide /v1/chat/completions
      → free failover…
      → 402 PAYMENT-REQUIRED (Hedera exact / tinybars)
      → agent signs (@x402/hedera)
      → retry PAYMENT-SIGNATURE
      → Blocky402 verify + settle
      → paid OpenRouter inference
      → 200 + PAYMENT-RESPONSE
```

## Prerequisites

1. FreeRide gateway on branch `ethonline/hedera-x402` with x402 env set (see `docs/ethonline-hedera-x402.md`).
2. Node.js 18+.
3. For **live** settle: Hedera testnet account + HBAR, and a real `FREERIDE_X402_PAY_TO` on the gateway.
4. For **local/judge dry-run**: gateway `FREERIDE_X402_DRY_RUN=1` + agent `FREERIDE_X402_AGENT_DRY_RUN=1` (no wallet).

## Install

```bash
cd examples/ethonline-hedera-agent
npm install
```

## Gateway env (minimum)

```bash
export FREERIDE_X402_ENABLED=1
export FREERIDE_X402_PAY_TO=0.0.YOUR_RECEIVER
export FREERIDE_X402_PAID_OPENROUTER_API_KEY=sk-or-...   # paid upstream
# Optional demo without wallet:
export FREERIDE_X402_DRY_RUN=1
```

Start FreeRide as usual (`freeride serve` / your usual entrypoint) on e.g. `:11343`.

## Run agent

### Dry-run (no Hedera wallet)

```bash
export FREERIDE_URL=http://127.0.0.1:11343
export FREERIDE_X402_AGENT_DRY_RUN=1
npm start
# or: npm run dry-run
```

### Live Hedera testnet

```bash
export FREERIDE_URL=http://127.0.0.1:11343
export HEDERA_ACCOUNT_ID=0.0.xxxx
export HEDERA_PRIVATE_KEY=0x...
export HEDERA_NETWORK=testnet
# ensure gateway DRY_RUN is unset/0
npm start
```

## Judge checklist

- [ ] Free request succeeds when free keys work (no payment header).
- [ ] With free exhausted / no usable keys + x402 enabled → **402** + `PAYMENT-REQUIRED`.
- [ ] Retry with `PAYMENT-SIGNATURE` → **200**, `PAYMENT-RESPONSE`, `_freeride_paid=hedera-x402`.
- [ ] Facilitator is Blocky402 testnet; network `hedera:testnet`; asset `0.0.0` (HBAR tinybars).
- [ ] README/product identity remains FreeRide (free-first), not a paid-only marketplace.

## Notes

- Package deps `@x402/hedera` / `@x402/fetch` versions may need bumping to match current npm; dry-run path does not import them.
- MVP paid lane on the gateway is **non-streaming** for demo reliability.
