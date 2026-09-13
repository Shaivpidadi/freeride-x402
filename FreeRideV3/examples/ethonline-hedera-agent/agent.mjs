#!/usr/bin/env node
/**
 * FreeRide ETHOnline demo agent
 *
 * Flow:
 *   1. POST /v1/chat/completions (free lane)
 *   2. If 402 + PAYMENT-REQUIRED → sign Hedera exact payment
 *   3. Retry with PAYMENT-SIGNATURE → paid OpenRouter upstream
 *
 * Env:
 *   FREERIDE_URL                 default http://127.0.0.1:11343
 *   HEDERA_ACCOUNT_ID            payer account (0.0.x)
 *   HEDERA_PRIVATE_KEY           ECDSA private key (0x… or DER)
 *   HEDERA_NETWORK               testnet|mainnet (default testnet)
 *   FREERIDE_X402_AGENT_DRY_RUN  1 = mock PAYMENT-SIGNATURE (gateway must have DRY_RUN=1)
 *   MODEL                        default openrouter/free model id or "auto"
 */

import { Buffer } from "node:buffer";

const FREERIDE_URL = (process.env.FREERIDE_URL || "http://127.0.0.1:11343").replace(/\/$/, "");
const MODEL = process.env.MODEL || "openrouter/auto";
const AGENT_DRY_RUN = ["1", "true", "TRUE", "yes", "YES"].includes(
  (process.env.FREERIDE_X402_AGENT_DRY_RUN || "").trim(),
);

const chatBody = {
  model: MODEL,
  messages: [
    {
      role: "user",
      content:
        "Say hello in one short sentence. This request may ride FreeRide free tier or Hedera x402 paid lane.",
    },
  ],
  stream: false,
};

function b64json(obj) {
  return Buffer.from(JSON.stringify(obj), "utf8").toString("base64");
}

function decodePaymentRequired(headerVal) {
  const json = Buffer.from(headerVal, "base64").toString("utf8");
  return JSON.parse(json);
}

async function postChat(headers = {}) {
  const res = await fetch(`${FREERIDE_URL}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...headers,
    },
    body: JSON.stringify(chatBody),
  });
  const text = await res.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = { raw: text };
  }
  return { res, body };
}

async function signWithX402(paymentRequired) {
  const accountId = process.env.HEDERA_ACCOUNT_ID;
  const privateKey = process.env.HEDERA_PRIVATE_KEY;
  if (!accountId || !privateKey) {
    throw new Error(
      "Set HEDERA_ACCOUNT_ID and HEDERA_PRIVATE_KEY to sign a real Hedera payment, or FREERIDE_X402_AGENT_DRY_RUN=1 for local demo.",
    );
  }

  // Dynamic import so dry-run works without installing native deps in CI.
  const { ExactHederaScheme } = await import("@x402/hedera/exact/client");
  const { createClientHederaSigner, PrivateKey } = await import("@x402/hedera");

  const network =
    process.env.HEDERA_NETWORK === "mainnet" ? "hedera:mainnet" : "hedera:testnet";
  const accepts = paymentRequired.accepts?.[0];
  if (!accepts) throw new Error("PaymentRequired has no accepts[]");

  let key;
  try {
    key = PrivateKey.fromStringECDSA(privateKey);
  } catch {
    key = PrivateKey.fromString(privateKey);
  }

  const signer = createClientHederaSigner(accountId, key, { network });
  const scheme = new ExactHederaScheme(signer);
  const signed = await scheme.createPaymentPayload(2, accepts);
  return {
    x402Version: 2,
    scheme: "exact",
    network: accepts.network || network,
    accepted: accepts,
    payload: signed.payload,
  };
}

function mockDryRunPayload(paymentRequired) {
  const accepts = paymentRequired.accepts?.[0] || {
    scheme: "exact",
    network: "hedera:testnet",
    amount: "100000",
    payTo: "0.0.0",
    maxTimeoutSeconds: 300,
    asset: "0.0.0",
    extra: { feePayer: "0.0.7162784" },
  };
  return {
    x402Version: 2,
    scheme: "exact",
    network: accepts.network || "hedera:testnet",
    accepted: accepts,
    payload: { transaction: "agent-dry-run-mock" },
    payer: process.env.HEDERA_ACCOUNT_ID || "0.0.dry-run",
  };
}

async function main() {
  console.log("FreeRide ETHOnline Hedera x402 agent");
  console.log(`  URL:     ${FREERIDE_URL}`);
  console.log(`  model:   ${MODEL}`);
  console.log(`  dry-run: ${AGENT_DRY_RUN}`);

  console.log("\n[1] Free lane request…");
  let { res, body } = await postChat();

  if (res.ok) {
    const content = body?.choices?.[0]?.message?.content;
    console.log("[free] success via", body?._freeride_provider || res.headers.get("x-freeride-provider"));
    console.log(content || JSON.stringify(body, null, 2));
    return;
  }

  if (res.status !== 402) {
    console.error(`[free] unexpected status ${res.status}`);
    console.error(JSON.stringify(body, null, 2));
    process.exitCode = 1;
    return;
  }

  const prHeader = res.headers.get("PAYMENT-REQUIRED") || res.headers.get("payment-required");
  if (!prHeader) {
    console.error("402 without PAYMENT-REQUIRED header");
    console.error(JSON.stringify(body, null, 2));
    process.exitCode = 1;
    return;
  }

  const paymentRequired = decodePaymentRequired(prHeader);
  console.log("[2] Free exhausted → x402 challenge");
  console.log("    error:", paymentRequired.error);
  console.log("    accepts:", JSON.stringify(paymentRequired.accepts?.[0], null, 2));

  let paymentPayload;
  if (AGENT_DRY_RUN) {
    console.log("[3] Agent dry-run: mocking PAYMENT-SIGNATURE (gateway FREERIDE_X402_DRY_RUN=1 required)");
    paymentPayload = mockDryRunPayload(paymentRequired);
  } else {
    console.log("[3] Signing Hedera exact payment with @x402/hedera…");
    paymentPayload = await signWithX402(paymentRequired);
  }

  const signature = b64json(paymentPayload);
  console.log("[4] Retry with PAYMENT-SIGNATURE…");
  ({ res, body } = await postChat({ "PAYMENT-SIGNATURE": signature }));

  if (!res.ok) {
    console.error(`[paid] failed status ${res.status}`);
    console.error(JSON.stringify(body, null, 2));
    process.exitCode = 1;
    return;
  }

  const settlementHdr =
    res.headers.get("PAYMENT-RESPONSE") || res.headers.get("payment-response");
  if (settlementHdr) {
    try {
      console.log("[paid] settlement:", Buffer.from(settlementHdr, "base64").toString("utf8"));
    } catch {
      console.log("[paid] PAYMENT-RESPONSE present");
    }
  }
  console.log("[paid] provider:", body?._freeride_provider, "paid:", body?._freeride_paid);
  console.log(body?.choices?.[0]?.message?.content || JSON.stringify(body, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
