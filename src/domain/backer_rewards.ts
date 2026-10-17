import { parse } from "jsr:@std/csv@1.0.6";
import {
  backerDiscountPercent,
  BILLING_CATALOG,
  type KickstarterRewardKey,
} from "./billing.ts";

export interface CampaignMapping {
  readonly campaignId: string;
  readonly rewards: Readonly<Record<string, KickstarterRewardKey>>;
}

export interface BackerRecord {
  readonly backerId: string;
  readonly email: string;
  readonly rewardId: string;
  readonly reward: KickstarterRewardKey;
  readonly collectedCents: number;
  readonly refundedCents: number;
  readonly collectionStatus: "pending" | "collected" | "failed";
  readonly discountPercent: number;
}

const fields = [
  "backer_id",
  "email",
  "reward_id",
  "currency",
  "collected_cents",
  "refunded_cents",
  "collection_status",
] as const;
export type ImportColumns = Record<typeof fields[number], string>;
export const DEFAULT_IMPORT_COLUMNS: ImportColumns = Object.fromEntries(
  fields.map((field) => [field, field]),
) as ImportColumns;

export function validateCampaign(mapping: CampaignMapping): void {
  if (!/^[A-Za-z0-9_-]{1,100}$/u.test(mapping.campaignId)) {
    throw new TypeError("Invalid campaign identifier.");
  }
  const entries = Object.entries(mapping.rewards);
  if (!entries.length || entries.length > 100) {
    throw new TypeError("Between 1 and 100 reward mappings are required.");
  }
  for (const [rewardId, reward] of entries) {
    if (
      !/^[A-Za-z0-9_-]{1,100}$/u.test(rewardId) ||
      !Object.hasOwn(BILLING_CATALOG.kickstarter, reward)
    ) throw new TypeError("Invalid reward mapping.");
  }
}

export function parseBackerImport(
  csv: string,
  mapping: CampaignMapping,
  columns: ImportColumns = DEFAULT_IMPORT_COLUMNS,
): readonly BackerRecord[] {
  validateCampaign(mapping);
  if (new TextEncoder().encode(csv).length > 2_000_000) {
    throw new TypeError("CSV exceeds 2 MB.");
  }
  const records = parse(csv.replace(/^\uFEFF/u, ""), { skipFirstRow: false });
  const header = records.shift();
  if (!header || new Set(header).size !== header.length) {
    throw new TypeError("CSV requires unique column names.");
  }
  const indexes = Object.fromEntries(
    fields.map((field) => [field, header.indexOf(columns[field])]),
  ) as Record<typeof fields[number], number>;
  if (
    fields.some((field) => indexes[field] < 0) ||
    new Set(Object.values(columns)).size !== fields.length
  ) {
    throw new TypeError(
      "Required CSV column mapping is missing or duplicated.",
    );
  }
  if (!records.length || records.length > 5000) {
    throw new TypeError("Import must contain between 1 and 5000 backers.");
  }
  const seen = new Set<string>();
  return records.map((cells, index) => {
    const fail = () => {
      throw new TypeError(`Invalid or ambiguous backer row ${index + 2}.`);
    };
    if (cells.length !== header.length) return fail();
    const value = (field: typeof fields[number]) =>
      cells[indexes[field]].trim();
    const backerId = value("backer_id");
    const email = value("email");
    const rewardId = value("reward_id");
    const collectionStatus = value("collection_status");
    const collected = value("collected_cents");
    const refunded = value("refunded_cents");
    if (
      !/^[A-Za-z0-9_-]{1,100}$/u.test(backerId) || seen.has(backerId) ||
      email.length > 254 || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/u.test(email) ||
      !Object.hasOwn(mapping.rewards, rewardId) ||
      value("currency") !== "USD" || !/^(0|[1-9]\d{0,9})$/u.test(collected) ||
      !/^(0|[1-9]\d{0,9})$/u.test(refunded) ||
      !["pending", "collected", "failed"].includes(collectionStatus)
    ) return fail();
    const collectedCents = Number(collected);
    const refundedCents = Number(refunded);
    if (
      refundedCents > collectedCents ||
      (collectionStatus !== "collected" && collectedCents !== 0)
    ) return fail();
    seen.add(backerId);
    const status = collectionStatus as BackerRecord["collectionStatus"];
    return {
      backerId,
      email,
      rewardId,
      reward: mapping.rewards[rewardId],
      collectedCents,
      refundedCents,
      collectionStatus: status,
      discountPercent: backerDiscountPercent({
        currency: "usd",
        collectedCents,
        refundedCents,
        collectionStatus: status,
      }),
    };
  });
}

export function rewardEligible(record: BackerRecord): boolean {
  return record.collectionStatus === "collected" &&
    record.collectedCents - record.refundedCents >=
      BILLING_CATALOG.kickstarter[record.reward].amountCents;
}
