#!/usr/bin/env node
/**
 * FreeRide daemon Hedera x402 signer.
 *
 * stdin:  JSON paymentRequirements (one accepts[] entry / exact scheme)
 * stdout: JSON PaymentPayload
 *
 * Env:
 *   HEDERA_ACCOUNT_ID / FREERIDE_X402_PAYER_ACCOUNT
 *   HEDERA_PRIVATE_KEY / FREERIDE_X402_PAYER_KEY
 *   HEDERA_NETWORK = testnet|mainnet (default testnet)
 *
 * Install deps once from this folder:
 *   cd scripts && npm install
 */
import { readFileSync } from "node:fs";

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

async function main() {
  const raw = readStdin().trim();
  if (!raw) {
    console.error("x402_hedera_sign: expected paymentRequirements JSON on stdin");
    process.exit(2);
  }
  let requirements;
  try {
    requirements = JSON.parse(raw);
  } catch (e) {
    console.error("x402_hedera_sign: invalid JSON:", e.message);
    process.exit(2);
  }

  const accountId =
    process.env.FREERIDE_X402_PAYER_ACCOUNT || process.env.HEDERA_ACCOUNT_ID;
  const privateKey =
    process.env.FREERIDE_X402_PAYER_KEY || process.env.HEDERA_PRIVATE_KEY;
  if (!accountId || !privateKey) {
    console.error(
      "x402_hedera_sign: set FREERIDE_X402_PAYER_ACCOUNT + FREERIDE_X402_PAYER_KEY (or HEDERA_*)",
    );
    process.exit(2);
  }

  const { ExactHederaScheme } = await import("@x402/hedera/exact/client");
  const { createClientHederaSigner, PrivateKey } = await import("@x402/hedera");

  const network =
    process.env.HEDERA_NETWORK === "mainnet" ? "hedera:mainnet" : "hedera:testnet";

  let key;
  try {
    key = PrivateKey.fromStringECDSA(privateKey);
  } catch {
    key = PrivateKey.fromString(privateKey);
  }

  const signer = createClientHederaSigner(accountId, key, { network });
  const scheme = new ExactHederaScheme(signer);
  const signed = await scheme.createPaymentPayload(2, requirements);

  const out = {
    x402Version: 2,
    scheme: requirements.scheme || "exact",
    network: requirements.network || network,
    accepted: requirements,
    payload: signed.payload,
    payer: accountId,
  };
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  console.error("x402_hedera_sign:", e?.message || e);
  process.exit(1);
});
