/**
 * Paying for things a Bot needs, on Hedera, without holding a key.
 *
 * A Bot works unattended, so sooner or later it meets a resource that wants
 * money: a paywalled dataset, a metered API. x402 makes that a normal HTTP
 * exchange — 402 with a `PAYMENT-REQUIRED` challenge, retry with a
 * `PAYMENT-SIGNATURE`.
 *
 * The signing happens in the local FreeRide daemon, not here. The payer key
 * lives in `~/.freeride/.env` on the operator's machine and never enters this
 * process, so a Bot (or a prompt injected into one) can ask for a payment but
 * can never exfiltrate the wallet. FreeRide enforces its own ceiling and payee
 * allowlist on top of whatever budget this file applies.
 *
 * Amounts are tinybars throughout: 100_000 tinybars = 0.001 HBAR.
 */

import { record, recentActivity } from "./activity";

/** Where the FreeRide daemon listens. Same default the gateway ships with. */
const FREERIDE_URL = process.env.FREERIDE_URL ?? "http://127.0.0.1:11343";

/** Per-job ceiling in tinybars. Above this, a human signs off. */
export const JOB_BUDGET_TINYBARS = Number(
  process.env.BOT_X402_JOB_BUDGET_TINYBARS ?? 1_000_000, // 0.01 HBAR
);

export const TINYBARS_PER_HBAR = 100_000_000;

export function formatHbar(tinybars: number): string {
  return `${(tinybars / TINYBARS_PER_HBAR).toFixed(8).replace(/0+$/, "").replace(/\.$/, "")} HBAR`;
}

export interface PaymentRequirements {
  readonly scheme: string;
  readonly network: string;
  readonly amount: string;
  readonly payTo: string;
  readonly asset?: string;
  readonly maxTimeoutSeconds?: number;
  readonly extra?: Record<string, unknown>;
}

export interface PaymentResult {
  readonly paymentSignature: string;
  readonly transaction: string | null;
  readonly payer: string | null;
  readonly payTo: string;
  readonly amount: string;
  readonly network: string;
}

export interface WalletPolicy {
  readonly agentPayEnabled: boolean;
  readonly payerConfigured: boolean;
  readonly network: string;
  readonly maxAmount: string;
  readonly allowedPayees: readonly string[];
  readonly dryRun: boolean;
}

export class X402Error extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

/** What the local wallet will allow, so callers can decide before spending. */
export async function walletPolicy(): Promise<WalletPolicy | null> {
  try {
    const response = await fetch(`${FREERIDE_URL}/v1/_freeride/x402/policy`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) return null;
    const body = (await response.json()) as Record<string, unknown>;
    return {
      agentPayEnabled: body.agent_pay_enabled === true,
      payerConfigured: body.payer_configured === true,
      network: String(body.network ?? ""),
      maxAmount: String(body.max_amount ?? "0"),
      allowedPayees: Array.isArray(body.allowed_payees) ? (body.allowed_payees as string[]) : [],
      dryRun: body.dry_run === true,
    };
  } catch {
    return null;
  }
}

/**
 * Decode a `PAYMENT-REQUIRED` header into the first offer we can pay.
 *
 * A challenge may list several; we take the first on our own network so a
 * multi-chain resource does not push us onto a chain this wallet cannot sign.
 */
export function requirementsFromChallenge(
  header: string,
  network: string,
): PaymentRequirements | null {
  let decoded: unknown;
  try {
    decoded = JSON.parse(Buffer.from(header, "base64").toString("utf8"));
  } catch {
    return null;
  }
  if (typeof decoded !== "object" || decoded === null) return null;
  const accepts = (decoded as { accepts?: unknown }).accepts;
  if (!Array.isArray(accepts)) return null;
  const match = accepts.find(
    (entry) =>
      typeof entry === "object" &&
      entry !== null &&
      (entry as { network?: unknown }).network === network,
  );
  return (match as PaymentRequirements | undefined) ?? null;
}

/** Ask the daemon to sign and settle. Returns the retry header value. */
export async function payViaFreeride(
  requirements: PaymentRequirements,
  resourceUrl: string,
): Promise<PaymentResult> {
  let response: Response;
  try {
    response = await fetch(`${FREERIDE_URL}/v1/_freeride/x402/pay`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ paymentRequirements: requirements, resourceUrl }),
      signal: AbortSignal.timeout(90_000),
    });
  } catch (error) {
    throw new X402Error(
      "daemon_unreachable",
      `Could not reach the FreeRide daemon at ${FREERIDE_URL}. Start it with \`freeride start\`. (${String(error)})`,
    );
  }

  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok || body.ok !== true) {
    const error = (body.error ?? {}) as { type?: string; message?: string };
    throw new X402Error(
      error.type ?? `http_${response.status}`,
      error.message ?? `Payment failed with HTTP ${response.status}`,
    );
  }

  return {
    paymentSignature: String(body.paymentSignature ?? ""),
    transaction: body.transaction === undefined ? null : String(body.transaction),
    payer: body.payer === undefined ? null : String(body.payer),
    payTo: String(body.payTo ?? requirements.payTo),
    amount: String(body.amount ?? requirements.amount),
    network: String(body.network ?? requirements.network),
  };
}

/**
 * Tinybars already spent on a job.
 *
 * Read back from the activity feed rather than a counter: the feed is the
 * durable, append-only record the operator audits, and a job can span process
 * restarts. One source of truth beats two that can disagree about money.
 */
export async function spentOnJob(workspaceId: string, jobId: string): Promise<number> {
  const events = await recentActivity(workspaceId, { jobId, limit: 500 });
  let total = 0;
  for (const event of events) {
    if (event.kind !== "job.paid") continue;
    const amount = Number((event.data as { amountTinybars?: unknown } | undefined)?.amountTinybars);
    if (Number.isFinite(amount)) total += amount;
  }
  return total;
}

/**
 * How a payment came to be authorized.
 *
 * The approval hook and `execute` are separate calls, so the decision has to
 * be carried between them or the feed cannot say who authorized a payment.
 * They share `callId`. `unknown` exists so the audit trail never *claims* a
 * human signed off when we cannot prove it.
 */
export type Authorization = "policy" | "human" | "unknown";

const decisions = new Map<string, Authorization>();
const MAX_TRACKED = 200;

export function noteAuthorization(callId: string, decision: Authorization): void {
  if (decisions.size >= MAX_TRACKED) {
    const oldest = decisions.keys().next().value;
    if (oldest !== undefined) decisions.delete(oldest);
  }
  decisions.set(callId, decision);
}

/** Reads and clears the decision for a call. */
export function takeAuthorization(callId: string): Authorization {
  const found = decisions.get(callId);
  decisions.delete(callId);
  return found ?? "unknown";
}

/** Write a payment into the feed the operator reads. */
export async function recordPayment(input: {
  workspaceId: string;
  botId: string | null;
  jobId: string;
  amountTinybars: number;
  payTo: string;
  resourceUrl: string;
  transaction: string | null;
  approved: Authorization;
}): Promise<void> {
  const explorer =
    input.transaction === null
      ? null
      : `https://hashscan.io/testnet/transaction/${input.transaction}`;
  await record({
    workspaceId: input.workspaceId,
    botId: input.botId,
    jobId: input.jobId,
    kind: "job.paid",
    text: `Paid ${formatHbar(input.amountTinybars)} to ${input.payTo} for ${input.resourceUrl}`,
    data: {
      amountTinybars: input.amountTinybars,
      payTo: input.payTo,
      resourceUrl: input.resourceUrl,
      transaction: input.transaction,
      approved: input.approved,
      ...(explorer === null ? {} : { explorer }),
    },
  });
}
