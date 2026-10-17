export const BILLING_CATALOG = Object.freeze({
  version: 1,
  currency: "usd",
  communityLimit: 1,
  installationLimits: Object.freeze({ twitch: 1, discord: 3 }),
  subscriptions: Object.freeze({
    monthly: Object.freeze({ amountCents: 999, intervalMonths: 1 }),
    annual: Object.freeze({ amountCents: 9_999, intervalMonths: 12 }),
  }),
  kickstarter: Object.freeze({
    threeMonths: Object.freeze({ amountCents: 2_500, accessMonths: 3 }),
    sixMonths: Object.freeze({ amountCents: 5_000, accessMonths: 6 }),
    oneYear: Object.freeze({ amountCents: 10_000, accessMonths: 12 }),
    lifetime: Object.freeze({ amountCents: 14_999, accessMonths: null }),
  }),
  backerDiscount: Object.freeze({ minimumPledgeCents: 5_000, percentOff: 20 }),
});

export type KickstarterRewardKey = keyof typeof BILLING_CATALOG.kickstarter;
export type AccessGrantSource = "stripe" | "kickstarter" | "manual";

export interface AccessGrant {
  readonly organizationId: number;
  readonly source: AccessGrantSource;
  readonly sourceId: string;
  readonly kind: "fixed_term" | "lifetime";
  readonly status: "active" | "revoked";
  readonly startsAt: string;
  readonly endsAt: string | null;
}

export interface OrganizationEntitlement {
  readonly active: boolean;
  readonly complimentary?: boolean;
  readonly communityLimit: number;
  readonly endsAt: string | null;
  readonly grants: readonly AccessGrant[];
}

function positiveId(value: number): void {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError("organization ID must be a positive integer");
  }
}

function timestamp(value: string): number {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u.test(value)) {
    throw new TypeError("timestamp must be a canonical UTC ISO string");
  }
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf()) || date.toISOString() !== value) {
    throw new TypeError("invalid timestamp");
  }
  return date.valueOf();
}

export function addAccessMonths(startsAt: string, months: number): string {
  const start = new Date(timestamp(startsAt));
  if (!Number.isSafeInteger(months) || months < 1 || months > 1_200) {
    throw new TypeError("access months must be between 1 and 1200");
  }
  const result = new Date(start);
  result.setUTCDate(1);
  result.setUTCMonth(result.getUTCMonth() + months);
  const lastDay = new Date(result);
  lastDay.setUTCMonth(lastDay.getUTCMonth() + 1, 0);
  result.setUTCDate(Math.min(start.getUTCDate(), lastDay.getUTCDate()));
  const endsAt = result.toISOString();
  timestamp(endsAt);
  return endsAt;
}

export function kickstarterAccessEnd(
  rewardKey: KickstarterRewardKey,
  startsAt: string,
): string | null {
  timestamp(startsAt);
  if (!Object.hasOwn(BILLING_CATALOG.kickstarter, rewardKey)) {
    throw new TypeError("unknown Kickstarter reward");
  }
  const months = BILLING_CATALOG.kickstarter[rewardKey].accessMonths;
  return months === null ? null : addAccessMonths(startsAt, months);
}

export function backerDiscountPercent(input: {
  readonly currency: string;
  readonly collectedCents: number;
  readonly refundedCents: number;
  readonly collectionStatus: "pending" | "collected" | "failed";
}): number {
  if (
    !Number.isSafeInteger(input.collectedCents) || input.collectedCents < 0 ||
    !Number.isSafeInteger(input.refundedCents) || input.refundedCents < 0 ||
    input.refundedCents > input.collectedCents
  ) {
    throw new TypeError("pledge amounts must be valid nonnegative minor units");
  }
  const discount = BILLING_CATALOG.backerDiscount;
  return input.currency === BILLING_CATALOG.currency &&
      input.collectionStatus === "collected" &&
      input.collectedCents - input.refundedCents >= discount.minimumPledgeCents
    ? discount.percentOff
    : 0;
}

export function resolveOrganizationEntitlement(
  organizationId: number,
  grants: readonly AccessGrant[],
  now = new Date(),
): OrganizationEntitlement {
  positiveId(organizationId);
  const currentTime = now.valueOf();
  if (!Number.isFinite(currentTime)) {
    throw new TypeError("invalid current time");
  }
  const eligible: AccessGrant[] = [];
  for (const grant of grants) {
    if (grant.organizationId !== organizationId || grant.status !== "active") {
      continue;
    }
    const start = timestamp(grant.startsAt);
    const end = grant.endsAt === null ? null : timestamp(grant.endsAt);
    if (
      !grant.sourceId.trim() ||
      !["stripe", "kickstarter", "manual"].includes(grant.source) ||
      !["fixed_term", "lifetime"].includes(grant.kind) ||
      (grant.kind === "lifetime" &&
        (end !== null || grant.source !== "kickstarter")) ||
      (grant.kind === "fixed_term" && (end === null || end <= start))
    ) {
      throw new TypeError("invalid access grant");
    }
    if (start <= currentTime && (end === null || currentTime < end)) {
      eligible.push(Object.freeze({ ...grant }));
    }
  }
  const lifetime = eligible.some((grant) => grant.kind === "lifetime");
  const endsAt = eligible.length && !lifetime
    ? new Date(Math.max(...eligible.map((grant) => timestamp(grant.endsAt!))))
      .toISOString()
    : null;
  return Object.freeze({
    active: eligible.length > 0,
    communityLimit: eligible.length > 0 ? BILLING_CATALOG.communityLimit : 0,
    endsAt,
    grants: Object.freeze(eligible),
  });
}
