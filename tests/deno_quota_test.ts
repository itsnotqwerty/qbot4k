import {
  calculateQuotaWindow,
  DEFAULT_TENANT_QUOTAS,
  normalizeQuotaType,
  validateQuotaPolicy,
} from "../src/domain/quota.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      `expected ${JSON.stringify(expected)}, received ${
        JSON.stringify(actual)
      }`,
    );
  }
}

Deno.test("quota types normalize to the frozen default policy", () => {
  assertEquals(normalizeQuotaType(" InGeStIoN "), "ingestion");
  assertEquals(DEFAULT_TENANT_QUOTAS.exports, {
    limitCount: 100,
    windowSeconds: 3600,
  });
});

Deno.test("quota policy validation preserves Python integer coercion", () => {
  assertEquals(validateQuotaPolicy(12.9, 60.8), {
    limitCount: 12,
    windowSeconds: 60,
  });
});

Deno.test("quota windows align to epoch boundaries and report retry delay", () => {
  assertEquals(calculateQuotaWindow(1_788_436_837, 60), {
    windowEpoch: 1_788_436_800,
    retryAfterSeconds: 23,
  });
});
