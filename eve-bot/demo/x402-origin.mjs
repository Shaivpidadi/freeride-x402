#!/usr/bin/env node
/**
 * A paywalled resource, for demonstrating that a Bot can buy something.
 *
 * Speaks the merchant half of x402 v2: an unpaid request gets 402 with a
 * base64 `PAYMENT-REQUIRED` challenge; a request carrying `PAYMENT-SIGNATURE`
 * gets the goods. It is deliberately the *seller*, so the whole loop is real
 * on both ends — the Bot is not paying a mock.
 *
 * What it does not do is verify the payment against the chain. A real merchant
 * would post the payload to a facilitator's /verify before serving. Here the
 * settlement is already provable on HashScan from the payer's side, which is
 * what the demo is showing; treating presence-of-signature as proof keeps the
 * seller to one file. Do not deploy this as an actual paywall.
 *
 *   node demo/x402-origin.mjs
 *   PORT=4021 PAY_TO=0.0.10471098 AMOUNT=100000 node demo/x402-origin.mjs
 */
import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 4021);
const PAY_TO = process.env.PAY_TO ?? "0.0.10471098";
const AMOUNT = process.env.AMOUNT ?? "100000"; // 0.001 HBAR
const NETWORK = process.env.NETWORK ?? "hedera:testnet";
const ASSET = process.env.ASSET ?? "0.0.0";
const FEE_PAYER = process.env.FEE_PAYER ?? "0.0.7162784";

/** The thing behind the paywall. */
const CONTENT = {
  report: "Q3 competitor pricing",
  rows: [
    { vendor: "Northwind", plan: "Team", monthlyUsd: 24, seatsMin: 3 },
    { vendor: "Contoso", plan: "Business", monthlyUsd: 31, seatsMin: 5 },
    { vendor: "Fabrikam", plan: "Growth", monthlyUsd: 19, seatsMin: 1 },
  ],
  note: "Paid resource. Delivered because the request carried a settled x402 payment.",
};

function challenge(resourceUrl) {
  return Buffer.from(
    JSON.stringify({
      x402Version: 2,
      error: "This resource requires payment",
      resource: {
        url: resourceUrl,
        description: "Competitor pricing report",
        mimeType: "application/json",
      },
      accepts: [
        {
          scheme: "exact",
          network: NETWORK,
          amount: AMOUNT,
          payTo: PAY_TO,
          maxTimeoutSeconds: 300,
          asset: ASSET,
          extra: { feePayer: FEE_PAYER },
        },
      ],
      extensions: {},
    }),
    "utf8",
  ).toString("base64");
}

const server = createServer((req, res) => {
  const url = `http://127.0.0.1:${PORT}${req.url}`;

  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, payTo: PAY_TO, amount: AMOUNT, network: NETWORK }));
    return;
  }

  const payment = req.headers["payment-signature"] ?? req.headers["x-payment"];

  if (payment === undefined || payment === "") {
    console.log(`402  ${req.method} ${req.url} — asking ${AMOUNT} tinybars to ${PAY_TO}`);
    res.writeHead(402, {
      "content-type": "application/json",
      "PAYMENT-REQUIRED": challenge(url),
    });
    res.end(
      JSON.stringify({
        error: { type: "payment_required", message: "Pay to read this report.", amount: AMOUNT },
      }),
    );
    return;
  }

  console.log(`200  ${req.method} ${req.url} — payment presented (${String(payment).length} chars)`);
  res.writeHead(200, {
    "content-type": "application/json",
    "PAYMENT-RESPONSE": Buffer.from(JSON.stringify({ success: true }), "utf8").toString("base64"),
  });
  res.end(JSON.stringify(CONTENT));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`x402 origin on http://127.0.0.1:${PORT}  (${AMOUNT} tinybars -> ${PAY_TO})`);
});
