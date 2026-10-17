import {
  assertEquals,
  assertRejects,
  assertThrows,
} from "jsr:@std/assert@1.0.14";
import {
  parseBackerImport,
  rewardEligible,
} from "../src/domain/backer_rewards.ts";
import { EXPECTED_SCHEMA_VERSION } from "../src/data/database.ts";
import { MIGRATION_NAMES } from "../src/data/operations.ts";
import { createPaidAccessHandler } from "../src/web/web_paid_access.ts";
import type { WebAuthController } from "../src/web/web_auth.ts";
import { ResendRewardDelivery } from "../src/providers/reward_email.ts";
import { WebRewardsController } from "../src/web/web_rewards.ts";
import type { BackerService } from "../src/data/backer_repository.ts";

Deno.test("backer persistence migration matches readiness", () => {
  assertEquals(MIGRATION_NAMES.length, EXPECTED_SCHEMA_VERSION);
});

Deno.test("campaign sender verification requires a matching verified Resend domain", async () => {
  let verified = false;
  let authorized = true;
  const delivery = new ResendRewardDelivery(
    "re_fixture",
    "rewards@example.test",
    "https://example.test",
    "signing",
    () =>
      Promise.resolve(
        authorized
          ? Response.json({
            data: [{
              name: "example.test",
              status: verified ? "verified" : "pending",
            }],
          })
          : new Response(null, { status: 401 }),
      ),
  );
  await assertRejects(() => delivery.verifySender(), Error, "not verified");
  verified = true;
  await delivery.verifySender();
  authorized = false;
  await assertRejects(() => delivery.verifySender(), Error, "unavailable");
});

Deno.test("claim delivery derives stable secret-bound codes and idempotent messages", async () => {
  const requests: RequestInit[] = [];
  const delivery = new ResendRewardDelivery(
    "re_fixture",
    "rewards@example.test",
    "https://example.test",
    "fixture-signing-key",
    (_input, init) => {
      requests.push(init!);
      return Promise.resolve(Response.json({ id: "mail_fixture" }));
    },
  );
  const code = await delivery.claimCode("nonce1");
  assertEquals(code.length, 64);
  assertEquals(await delivery.claimCode("nonce1"), code);
  assertEquals(await delivery.claimCode("nonce2") === code, false);
  await delivery.send("backer@example.test", code, "stable-key");
  await delivery.send("backer@example.test", code, "stable-key");
  assertEquals(requests[0].body, requests[1].body);
  assertEquals(
    new Headers(requests[0].headers).get("idempotency-key"),
    "reward-claim:stable-key",
  );
  assertEquals(new ResendRewardDelivery(null, null, null, null).ready, false);
});

Deno.test("reward HTTP rejects hostile mutations and exports only safe staff reconciliation columns", async () => {
  let staff = false;
  let writes = 0;
  const session = { userId: "42", sessionVersion: 1 };
  const auth = {
    accountSession: () => Promise.resolve(session),
    platformSession: () => Promise.resolve(staff ? session : null),
  } as unknown as WebAuthController;
  const service = {
    confirm: () => {
      writes++;
      return Promise.resolve();
    },
    overview: () =>
      Promise.resolve({
        backers: [{
          id: 1,
          backer_id: "=FORMULA()",
          record_json: "private@example.test",
          claim_hash: "private-code",
        }],
      }),
  } as unknown as BackerService;
  const controller = new WebRewardsController(auth, service, null, false);
  const request = (origin: string, body = "consent=confirmed") =>
    new Request("https://example.test/admin/rewards/confirm", {
      method: "POST",
      headers: { origin, "content-type": "application/x-www-form-urlencoded" },
      body,
    });
  assertEquals(
    (await controller.action(request("https://example.test"), "confirm"))
      .status,
    403,
  );
  staff = true;
  assertEquals(
    (await controller.action(request("https://hostile.test"), "confirm"))
      .status,
    403,
  );
  assertEquals(
    (await controller.action(
      request("https://example.test", "consent=no"),
      "confirm",
    )).status,
    400,
  );
  assertEquals(
    (await controller.action(
      request("https://example.test", "x".repeat(2_200_001)),
      "confirm",
    )).status,
    413,
  );
  assertEquals(writes, 0);
  const exported = await controller.export(
    new Request(
      "https://example.test/admin/rewards/export.csv?campaign=fixture",
    ),
  );
  assertEquals(exported.headers.get("cache-control"), "no-store");
  const csv = await exported.text();
  assertEquals(csv.includes("'=FORMULA()"), true);
  assertEquals(csv.includes("private"), false);
  staff = false;
  assertEquals(
    (await controller.export(
      new Request("https://example.test/admin/rewards/export.csv"),
    )).status,
    403,
  );
});

const header =
  "backer_id,email,reward_id,currency,collected_cents,refunded_cents,collection_status\n";
const mapping = {
  campaignId: "fixture",
  rewards: { term: "sixMonths", lifetime: "lifetime" },
} as const;

Deno.test("backer import requires mapped rewards and verified net collection", () => {
  const rows = parseBackerImport(
    header +
      "1,backer@example.test,term,USD,5000,0,collected\n2,pending@example.test,term,USD,0,0,pending",
    mapping,
  );
  assertEquals(rows[0].discountPercent, 20);
  assertEquals(rewardEligible(rows[0]), true);
  assertEquals(rewardEligible(rows[1]), false);
  const refunded = parseBackerImport(
    header + "1,backer@example.test,lifetime,USD,14999,1,collected",
    mapping,
  )[0];
  assertEquals(rewardEligible(refunded), false);
});

Deno.test("backer import rejects duplicates, missing collection evidence and unknown rewards", () => {
  for (
    const row of [
      "1,a@example.test,unknown,USD,5000,0,collected",
      "1,a@example.test,term,EUR,5000,0,collected",
      "1,a@example.test,term,USD,50.00,0,collected",
      "1,a@example.test,term,USD,5000,0,pending",
      "1,a@example.test,term,USD,5000,6000,collected",
      "1,a@example.test,term,USD,5000,0,collected\n1,b@example.test,term,USD,5000,0,collected",
    ]
  ) assertThrows(() => parseBackerImport(header + row, mapping), TypeError);
  assertThrows(
    () => parseBackerImport("email,amount\na@example.test,50", mapping),
    TypeError,
  );
});

Deno.test("paid web gate preserves recovery routes and platform staff access", async () => {
  let staff = false;
  const session = {
    userId: "42",
    username: "Fixture",
    role: "owner",
    communityId: 7,
    sessionVersion: 1,
    expiresAt: new Date(Date.now() + 1000).toISOString(),
  } as const;
  const auth: Pick<WebAuthController, "accountSession" | "platformSession"> = {
    accountSession: () => Promise.resolve(session),
    platformSession: () => Promise.resolve(staff ? session : null),
  };
  const handler = createPaidAccessHandler(
    () => Promise.resolve(new Response("ok")),
    auth,
    () => Promise.resolve(false),
  );
  assertEquals(
    (await handler(new Request("https://example.test/dashboard"))).status,
    402,
  );
  for (
    const path of [
      "/billing",
      "/billing/rewards/claim",
      "/account",
      "/login",
      "/admin/rewards",
      "/users/42/export.json",
      "/webhooks/stripe",
      "/community/switch",
      "/auth/discord/callback",
      "/terms",
      "/privacy",
    ]
  ) {
    assertEquals(
      (await handler(new Request(`https://example.test${path}`))).status,
      200,
    );
  }
  staff = true;
  assertEquals(
    (await handler(new Request("https://example.test/dashboard"))).status,
    200,
  );
});
