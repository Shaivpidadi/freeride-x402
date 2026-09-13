/**
 * The payment loop a Bot runs, without the Bot.
 *
 * Drives the same `agent/lib/x402.ts` the `x402_fetch` tool uses — fetch, 402,
 * settle on Hedera, retry, record — so the lane can be proven end to end
 * without standing up Sandbox, Blob, and the console.
 *
 *   node demo/x402-origin.mjs &                     # the seller
 *   BOT_STORE=fs BOT_DATA_DIR=.data \
 *     npx tsx demo/x402-buy.ts                      # the buyer
 *
 * Spends real testnet HBAR from the wallet in ~/.freeride/.env.
 */
import {
  JOB_BUDGET_TINYBARS,
  formatHbar,
  payViaFreeride,
  recordPayment,
  requirementsFromChallenge,
  spentOnJob,
  walletPolicy,
} from "../agent/lib/x402";

const RESOURCE = process.env.DEMO_RESOURCE ?? "http://127.0.0.1:4021/report";
const WORKSPACE = process.env.BOT_DEFAULT_WORKSPACE ?? "default";
const JOB_ID = process.env.DEMO_JOB_ID ?? "job_x402_demo";

async function main(): Promise<void> {
  const policy = await walletPolicy();
  if (policy === null) {
    throw new Error("FreeRide daemon unreachable. Run `freeride start`.");
  }
  console.log(
    `wallet: network=${policy.network} agentPay=${policy.agentPayEnabled} ` +
      `payer=${policy.payerConfigured} cap=${formatHbar(Number(policy.maxAmount))} dryRun=${policy.dryRun}`,
  );
  if (policy.dryRun) throw new Error("Refusing to demo a dry run.");

  const before = await spentOnJob(WORKSPACE, JOB_ID);
  console.log(`budget:  ${formatHbar(before)} spent of ${formatHbar(JOB_BUDGET_TINYBARS)}`);

  console.log(`\nGET ${RESOURCE}`);
  const first = await fetch(RESOURCE);
  console.log(`  -> ${first.status}${first.status === 402 ? " Payment Required" : ""}`);
  if (first.status !== 402) {
    console.log("  (resource was free; nothing to pay)");
    return;
  }

  const challenge = first.headers.get("PAYMENT-REQUIRED");
  if (challenge === null) throw new Error("402 without a PAYMENT-REQUIRED header");
  const requirements = requirementsFromChallenge(challenge, policy.network);
  if (requirements === null) throw new Error(`no offer on ${policy.network}`);
  const amount = Number(requirements.amount);
  console.log(`  asks ${formatHbar(amount)} -> ${requirements.payTo}`);

  if (before + amount > JOB_BUDGET_TINYBARS) {
    console.log("  OVER BUDGET — in the tool this parks on an operator approval card.");
    return;
  }
  console.log(
    `  within budget (${formatHbar(before + amount)} of ${formatHbar(JOB_BUDGET_TINYBARS)}) — paying`,
  );

  const payment = await payViaFreeride(requirements, RESOURCE);
  console.log(`  settled: ${payment.transaction}`);
  console.log(`  https://hashscan.io/testnet/transaction/${payment.transaction}`);

  await recordPayment({
    workspaceId: WORKSPACE,
    botId: "bot_demo",
    jobId: JOB_ID,
    amountTinybars: amount,
    payTo: payment.payTo,
    resourceUrl: RESOURCE,
    transaction: payment.transaction,
    approved: "policy",
  });

  const retried = await fetch(RESOURCE, {
    headers: { "PAYMENT-SIGNATURE": payment.paymentSignature },
  });
  console.log(`\nGET ${RESOURCE} (with PAYMENT-SIGNATURE)`);
  console.log(`  -> ${retried.status}`);
  console.log(`  ${(await retried.text()).slice(0, 200)}`);

  const after = await spentOnJob(WORKSPACE, JOB_ID);
  console.log(`\nledger: ${formatHbar(after)} spent on ${JOB_ID} (was ${formatHbar(before)})`);
}

await main();
