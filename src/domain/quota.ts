import type { DatabaseConnection } from "../data/database.ts";

export const DEFAULT_TENANT_QUOTAS = Object.freeze({
  ingestion: Object.freeze({ limitCount: 10_000, windowSeconds: 60 }),
  api: Object.freeze({ limitCount: 5_000, windowSeconds: 60 }),
  jobs: Object.freeze({ limitCount: 10_000, windowSeconds: 60 }),
  exports: Object.freeze({ limitCount: 100, windowSeconds: 3_600 }),
  announcements: Object.freeze({ limitCount: 1_000, windowSeconds: 60 }),
  moderation: Object.freeze({ limitCount: 1_000, windowSeconds: 60 }),
});

export type TenantQuotaType = keyof typeof DEFAULT_TENANT_QUOTAS;

export class TenantQuotaExceededError extends Error {
  constructor(
    readonly quotaType: TenantQuotaType,
    readonly retryAfterSeconds: number,
  ) {
    super(`tenant ${quotaType} quota exceeded`);
  }
}

export function normalizeQuotaType(quotaType: string): TenantQuotaType {
  const normalized = quotaType.trim().toLocaleLowerCase();
  if (!(normalized in DEFAULT_TENANT_QUOTAS)) {
    throw new TypeError("unsupported tenant quota type");
  }
  return normalized as TenantQuotaType;
}

export function validateQuotaPolicy(
  limitCount: number,
  windowSeconds: number,
): Readonly<{ limitCount: number; windowSeconds: number }> {
  const limit = Math.trunc(limitCount);
  const window = Math.trunc(windowSeconds);
  if (limit < 1 || limit > 1_000_000) {
    throw new TypeError("quota limit must be between 1 and 1000000");
  }
  if (window < 1 || window > 86_400) {
    throw new TypeError("quota window must be between 1 and 86400 seconds");
  }
  return Object.freeze({ limitCount: limit, windowSeconds: window });
}

export function calculateQuotaWindow(
  epoch: number,
  windowSeconds: number,
): Readonly<{ windowEpoch: number; retryAfterSeconds: number }> {
  const normalizedEpoch = Math.trunc(epoch);
  const window = Math.trunc(windowSeconds);
  const windowEpoch = normalizedEpoch - (normalizedEpoch % window);
  return Object.freeze({
    windowEpoch,
    retryAfterSeconds: windowEpoch + window - normalizedEpoch,
  });
}

export async function consumeTenantQuota(
  connection: DatabaseConnection,
  communityId: number,
  quotaType: TenantQuotaType,
  amount = 1,
  now = new Date(),
): Promise<number> {
  const increment = Math.trunc(amount);
  if (increment < 1) throw new TypeError("quota amount must be positive");
  const policy = (await connection.query(
    "SELECT limit_count,window_seconds FROM tenant_quota_policies WHERE community_id=$1 AND quota_type=$2",
    [communityId, quotaType],
  ))[0];
  const defaults = DEFAULT_TENANT_QUOTAS[quotaType];
  const limit = integer(
    policy?.limit_count ?? defaults.limitCount,
    "limit_count",
  );
  const windowSeconds = integer(
    policy?.window_seconds ?? defaults.windowSeconds,
    "window_seconds",
  );
  const epoch = Math.floor(now.valueOf() / 1000);
  const window = calculateQuotaWindow(epoch, windowSeconds);
  const windowStart = new Date(window.windowEpoch * 1000).toISOString();
  const usage = integer(
    (await connection.query(
      `INSERT INTO tenant_quota_usage(community_id,quota_type,window_start,usage_count)
       VALUES ($1,$2,$3,$4)
       ON CONFLICT(community_id,quota_type,window_start)
       DO UPDATE SET usage_count=tenant_quota_usage.usage_count+EXCLUDED.usage_count
       RETURNING usage_count`,
      [communityId, quotaType, windowStart, increment],
    ))[0]?.usage_count ?? 0,
    "usage_count",
  );
  if (usage > limit) {
    throw new TenantQuotaExceededError(quotaType, window.retryAfterSeconds);
  }
  return limit - usage;
}

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) throw new TypeError(`${name} is invalid`);
  return parsed;
}
