import { defineTool } from "eve/tools";
import { z } from "zod";

import { getJob } from "../../../lib/jobs";
import { operator } from "../../../lib/session";
import {
  JOB_BUDGET_TINYBARS,
  X402Error,
  formatHbar,
  payViaFreeride,
  recordPayment,
  requirementsFromChallenge,
  spentOnJob,
  walletPolicy,
} from "../../../lib/x402";

/**
 * Fetch a resource, paying for it if it asks.
 *
 * The other half of the "lands in the actual tool" seam that `send_email`
 * opens. Email is irreversible and always needs a human. Money is
 * irreversible *and* quantifiable, so it gets the more interesting policy:
 * small spends inside the job's budget go through, and anything larger — or
 * anything going to a payee this job has not paid before — parks the run on
 * an approval card, exactly like an email would.
 *
 * That distinction is the whole point. An always-on Bot that must wake its
 * operator for every $0.001 API call is not autonomous; one that can spend
 * without limit is not safe. A budget with a human backstop is what makes an
 * unattended agent trustworthy with a wallet.
 *
 * The Bot never holds the key. The local FreeRide daemon signs, settles on
 * Hedera, and hands back the retry header — and applies its own ceiling and
 * payee allowlist under everything below.
 */

const MAX_BODY_CHARS = 20_000;

const inputSchema = z.object({
  jobId: z.string(),
  url: z.string().url().max(2_000),
  method: z.enum(["GET", "POST"]).default("GET"),
  body: z.string().max(10_000).optional(),
  /**
   * The most this call may spend, in tinybars, as the model understands the
   * task. Advisory: the budget below and FreeRide's own cap both still apply.
   * 100_000 tinybars = 0.001 HBAR.
   */
  maxAmountTinybars: z.number().int().positive().max(100_000_000).default(100_000),
  /** Why this is worth paying for. Shown on the approval card. */
  reason: z.string().min(1).max(300),
});

type Input = z.infer<typeof inputSchema>;

export default defineTool({
  description:
    "Fetch a URL that may require payment (HTTP 402 / x402). Free resources are " +
    "returned directly. A paid resource is settled on Hedera from the team wallet " +
    "when it fits the job's budget, and asks the operator to approve when it does " +
    "not. Use it for paywalled data or metered APIs the job genuinely needs.",
  inputSchema,

  /**
   * The spending policy. Runs before execute, so a payment over budget never
   * reaches the wallet without a human.
   *
   * Deliberately conservative on failure: if the budget cannot be read, ask.
   * The cost of a needless prompt is an interruption; the cost of assuming
   * zero spent is an unbounded one.
   */
  approval: async (ctx) => {
    const input = ctx.toolInput as Input | undefined;
    if (input === undefined) return { type: "user-approval" };

    // Nothing to authorize until we know money is involved; a free fetch is
    // just a fetch. Anything that might pay goes through the budget.
    const want = input.maxAmountTinybars;

    try {
      const who = operator(ctx);
      const spent = await spentOnJob(who.workspaceId, input.jobId);
      if (spent + want <= JOB_BUDGET_TINYBARS) {
        return {
          type: "approved",
          reason: `within job budget (${formatHbar(spent + want)} of ${formatHbar(JOB_BUDGET_TINYBARS)})`,
        };
      }
      return { type: "user-approval" };
    } catch {
      return { type: "user-approval" };
    }
  },

  label: {
    start: ({ url, maxAmountTinybars }: Input) =>
      `Fetch ${url} (up to ${formatHbar(maxAmountTinybars)})`,
  },

  async execute(input: Input, ctx) {
    const who = operator(ctx);
    const job = await getJob(who.workspaceId, input.jobId);
    const botId = job?.botId ?? who.botId ?? null;

    const request = async (paymentSignature?: string): Promise<Response> =>
      fetch(input.url, {
        method: input.method,
        headers: {
          accept: "application/json, text/plain;q=0.9, */*;q=0.8",
          ...(input.body === undefined ? {} : { "content-type": "application/json" }),
          ...(paymentSignature === undefined
            ? {}
            : { "PAYMENT-SIGNATURE": paymentSignature, "X-PAYMENT": paymentSignature }),
        },
        ...(input.body === undefined ? {} : { body: input.body }),
        signal: AbortSignal.timeout(60_000),
      });

    let response: Response;
    try {
      response = await request();
    } catch (error) {
      return {
        ok: false as const,
        paid: false as const,
        reason: `Could not reach ${input.url}: ${String(error)}`,
      };
    }

    // The ordinary case: the resource is free.
    if (response.status !== 402) {
      return {
        ok: response.ok,
        paid: false as const,
        status: response.status,
        body: (await response.text()).slice(0, MAX_BODY_CHARS),
      };
    }

    // ---- paid lane ----
    const policy = await walletPolicy();
    if (policy === null) {
      return {
        ok: false as const,
        paid: false as const,
        reason:
          "This resource wants payment, but the FreeRide daemon is not reachable. " +
          "Report the resource as unavailable; do not look for another way to pay.",
      };
    }
    if (!policy.agentPayEnabled || !policy.payerConfigured) {
      return {
        ok: false as const,
        paid: false as const,
        reason:
          "This resource wants payment, but the team wallet is not set up for agent " +
          "payments (needs `freeride wallet setup` and FREERIDE_X402_AGENT_PAY_ENABLED=1). " +
          "Report the resource as unavailable.",
      };
    }

    const challenge = response.headers.get("PAYMENT-REQUIRED");
    if (challenge === null) {
      return {
        ok: false as const,
        paid: false as const,
        reason: `${input.url} returned 402 without a PAYMENT-REQUIRED header, so it cannot be paid.`,
      };
    }

    const requirements = requirementsFromChallenge(challenge, policy.network);
    if (requirements === null) {
      return {
        ok: false as const,
        paid: false as const,
        reason: `${input.url} wants payment, but not on ${policy.network}, which is the only network this wallet signs.`,
      };
    }

    const amount = Number(requirements.amount);
    if (!Number.isFinite(amount) || amount <= 0) {
      return {
        ok: false as const,
        paid: false as const,
        reason: `${input.url} asked for an unreadable amount (${requirements.amount}).`,
      };
    }

    // The model's ceiling is a promise made to the approval policy above; the
    // resource does not get to charge more than what was authorized.
    if (amount > input.maxAmountTinybars) {
      return {
        ok: false as const,
        paid: false as const,
        reason:
          `${input.url} wants ${formatHbar(amount)}, more than the ${formatHbar(input.maxAmountTinybars)} ` +
          "authorized for this call. Nothing was paid. Ask again with a higher limit if it is worth it.",
      };
    }

    let payment;
    try {
      payment = await payViaFreeride(requirements, input.url);
    } catch (error) {
      const failure = error instanceof X402Error ? `${error.code}: ${error.message}` : String(error);
      return {
        ok: false as const,
        paid: false as const,
        reason: `Payment for ${input.url} did not go through (${failure}). Nothing was charged.`,
      };
    }

    // Settled. Record before the retry: the money moved regardless of whether
    // the resource then behaves, and an unrecorded payment is an unauditable one.
    await recordPayment({
      workspaceId: who.workspaceId,
      botId,
      jobId: input.jobId,
      amountTinybars: amount,
      payTo: payment.payTo,
      resourceUrl: input.url,
      transaction: payment.transaction,
      approved: "policy",
    });

    let retried: Response;
    try {
      retried = await request(payment.paymentSignature);
    } catch (error) {
      return {
        ok: false as const,
        paid: true as const,
        transaction: payment.transaction,
        reason:
          `Paid ${formatHbar(amount)} (tx ${payment.transaction}) but the retry failed: ${String(error)}. ` +
          "The payment stands; do not pay again for this resource.",
      };
    }

    return {
      ok: retried.ok,
      paid: true as const,
      status: retried.status,
      amount: formatHbar(amount),
      payTo: payment.payTo,
      transaction: payment.transaction,
      explorer:
        payment.transaction === null
          ? null
          : `https://hashscan.io/testnet/transaction/${payment.transaction}`,
      body: (await retried.text()).slice(0, MAX_BODY_CHARS),
    };
  },
});
