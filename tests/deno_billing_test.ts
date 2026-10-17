import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  type AccessGrant,
  addAccessMonths,
  backerDiscountPercent,
  BILLING_CATALOG,
  kickstarterAccessEnd,
  resolveOrganizationEntitlement,
} from "../src/domain/billing.ts";

const now = new Date("2026-10-17T12:00:00.000Z");
const grant: AccessGrant = {
  organizationId: 7,
  source: "stripe",
  sourceId: "sub_1",
  kind: "fixed_term",
  status: "active",
  startsAt: "2026-10-01T00:00:00.000Z",
  endsAt: "2026-11-01T00:00:00.000Z",
};

Deno.test("billing catalog contains approved USD offers for one community", () => {
  assertEquals(BILLING_CATALOG.currency, "usd");
  assertEquals(BILLING_CATALOG.communityLimit, 1);
  assertEquals(BILLING_CATALOG.subscriptions, {
    monthly: { amountCents: 999, intervalMonths: 1 },
    annual: { amountCents: 9_999, intervalMonths: 12 },
  });
  assertEquals(BILLING_CATALOG.kickstarter, {
    threeMonths: { amountCents: 2_500, accessMonths: 3 },
    sixMonths: { amountCents: 5_000, accessMonths: 6 },
    oneYear: { amountCents: 10_000, accessMonths: 12 },
    lifetime: { amountCents: 14_999, accessMonths: null },
  });
  assertEquals(Object.isFrozen(BILLING_CATALOG.kickstarter.threeMonths), true);
});

Deno.test("Kickstarter access uses selected reward and calendar months", () => {
  const startsAt = "2026-01-31T12:34:56.789Z";
  assertEquals(
    kickstarterAccessEnd("threeMonths", startsAt),
    "2026-04-30T12:34:56.789Z",
  );
  assertEquals(
    kickstarterAccessEnd("sixMonths", startsAt),
    "2026-07-31T12:34:56.789Z",
  );
  assertEquals(
    kickstarterAccessEnd("oneYear", startsAt),
    "2027-01-31T12:34:56.789Z",
  );
  assertEquals(kickstarterAccessEnd("lifetime", startsAt), null);
  assertEquals(
    addAccessMonths("2024-02-29T00:00:00.000Z", 12),
    "2025-02-28T00:00:00.000Z",
  );
  assertEquals(
    addAccessMonths("2023-11-30T00:00:00.000Z", 3),
    "2024-02-29T00:00:00.000Z",
  );
});

Deno.test("billing rejects invalid dates, durations, and reward keys", () => {
  for (const months of [0, -1, 1.5, NaN, Infinity, 1_201]) {
    assertThrows(() => addAccessMonths(now.toISOString(), months), TypeError);
  }
  for (const date of ["invalid", "2026-02-30T00:00:00.000Z", "2026-01-01"]) {
    assertThrows(() => addAccessMonths(date, 3), TypeError);
  }
  assertThrows(
    () => kickstarterAccessEnd("toString" as "oneYear", now.toISOString()),
    TypeError,
  );
});

Deno.test("backer discount requires collected USD amount net of refunds", () => {
  for (const amount of [2_500, 4_999, 5_000, 10_000, 14_999]) {
    assertEquals(
      backerDiscountPercent({
        currency: "usd",
        collectedCents: amount,
        refundedCents: 0,
        collectionStatus: "collected",
      }),
      amount >= 5_000 ? 20 : 0,
    );
  }
  for (const collectionStatus of ["pending", "failed"] as const) {
    assertEquals(
      backerDiscountPercent({
        currency: "usd",
        collectedCents: 10_000,
        refundedCents: 0,
        collectionStatus,
      }),
      0,
    );
  }
  assertEquals(
    backerDiscountPercent({
      currency: "eur",
      collectedCents: 10_000,
      refundedCents: 0,
      collectionStatus: "collected",
    }),
    0,
  );
  for (const refundedCents of [5_000, 5_001]) {
    assertEquals(
      backerDiscountPercent({
        currency: "usd",
        collectedCents: 10_000,
        refundedCents,
        collectionStatus: "collected",
      }),
      refundedCents === 5_000 ? 20 : 0,
    );
  }
  for (const collectedCents of [-1, 1.5, NaN, Infinity]) {
    assertThrows(() =>
      backerDiscountPercent({
        currency: "usd",
        collectedCents,
        refundedCents: 0,
        collectionStatus: "collected",
      }), TypeError);
  }
  assertThrows(() =>
    backerDiscountPercent({
      currency: "usd",
      collectedCents: 5_000,
      refundedCents: 5_001,
      collectionStatus: "collected",
    }), TypeError);
});

Deno.test("entitlements isolate organizations and use exclusive expiry", () => {
  assertEquals(resolveOrganizationEntitlement(8, [grant], now).active, false);
  assertEquals(
    resolveOrganizationEntitlement(7, [grant], new Date(grant.startsAt)).active,
    true,
  );
  assertEquals(
    resolveOrganizationEntitlement(7, [grant], new Date(grant.endsAt!)).active,
    false,
  );
  assertEquals(
    resolveOrganizationEntitlement(7, [grant], new Date("2026-09-01")).active,
    false,
  );
  assertEquals(
    resolveOrganizationEntitlement(7, [grant], now).communityLimit,
    1,
  );
  assertEquals(resolveOrganizationEntitlement(7, [], now).communityLimit, 0);
});

Deno.test("revoking a payment grant does not remove independent backer access", () => {
  const lifetime: AccessGrant = {
    ...grant,
    source: "kickstarter",
    sourceId: "campaign:backer",
    kind: "lifetime",
    endsAt: null,
  };
  const result = resolveOrganizationEntitlement(7, [{
    ...grant,
    status: "revoked",
  }, lifetime], now);
  assertEquals(result.active, true);
  assertEquals(result.endsAt, null);
  assertEquals(result.grants, [lifetime]);
  assertEquals(
    resolveOrganizationEntitlement(7, [grant, {
      ...lifetime,
      status: "revoked",
    }], now).endsAt,
    grant.endsAt,
  );
});

Deno.test("entitlements retain independent sources and the longest active term", () => {
  const reward: AccessGrant = {
    ...grant,
    source: "kickstarter",
    sourceId: "backer",
    endsAt: "2027-01-01T00:00:00.000Z",
  };
  const result = resolveOrganizationEntitlement(7, [grant, reward], now);
  assertEquals(result.endsAt, reward.endsAt);
  assertEquals(result.grants.length, 2);
  assertEquals(Object.isFrozen(result.grants[0]), true);
  assertEquals(Object.isFrozen(grant), false);
});

Deno.test("invalid grants cannot accidentally confer lifetime access", () => {
  for (
    const invalid of [
      { ...grant, endsAt: null },
      { ...grant, endsAt: grant.startsAt },
      { ...grant, sourceId: " " },
      { ...grant, kind: "lifetime" as const, endsAt: null },
      { ...grant, kind: "lifetime" as const, source: "kickstarter" as const },
    ]
  ) {
    assertThrows(
      () => resolveOrganizationEntitlement(7, [invalid], now),
      TypeError,
    );
  }
  assertThrows(() => resolveOrganizationEntitlement(0, [], now), TypeError);
  assertThrows(
    () => resolveOrganizationEntitlement(7, [], new Date(NaN)),
    TypeError,
  );
});
