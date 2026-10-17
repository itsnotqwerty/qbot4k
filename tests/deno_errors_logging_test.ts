import { assertEquals } from "jsr:@std/assert@1.0.14";
import { AuthorizationError, ValidationError } from "../src/core/errors.ts";
import { type LogRecord, StructuredLogger } from "../src/core/logging.ts";

Deno.test("application errors expose stable typed responses", () => {
  const error = new ValidationError("community_id is required", {
    field: "community_id",
  });
  assertEquals(error.status, 400);
  assertEquals(error.toJSON(), {
    code: "validation_error",
    message: "community_id is required",
    details: { field: "community_id" },
  });
  assertEquals(new AuthorizationError().status, 403);
});

Deno.test("structured logger filters levels and records context", () => {
  const records: LogRecord[] = [];
  const logger = new StructuredLogger(
    "qbot4k.test",
    "INFO",
    (record) => records.push(record),
  );
  logger.debug("hidden");
  logger.info("ready", { role: "jobs" });
  logger.error("failed", new Error("database unavailable"));

  assertEquals(records.length, 2);
  assertEquals(records[0].logger, "qbot4k.test");
  assertEquals(records[0].context, { role: "jobs" });
  assertEquals(records[1].error?.message, "database unavailable");
});

Deno.test("structured logger redacts credentials recursively", () => {
  const records: LogRecord[] = [];
  const logger = new StructuredLogger(
    "qbot4k.test",
    "INFO",
    (record) => records.push(record),
  );
  logger.error(
    "provider failed with Bearer message-token",
    new Error(
      "postgresql://operator:database-secret@db.example/qbot4k token=raw-token",
    ),
    {
      authorization: "Bearer access-token",
      nested: {
        refresh_token: "refresh-token",
        database_url: "postgresql://operator:database-secret@db.example/qbot4k",
      },
      cookies: ["qbot4k_session=session-secret"],
    },
  );

  const serialized = JSON.stringify(records[0]);
  for (
    const secret of [
      "access-token",
      "refresh-token",
      "database-secret",
      "raw-token",
      "session-secret",
      "message-token",
    ]
  ) {
    assertEquals(serialized.includes(secret), false);
  }
  assertEquals(records[0].context?.authorization, "[REDACTED]");
  assertEquals(
    records[0].error?.message,
    "postgresql://[REDACTED]@db.example/qbot4k token=[REDACTED]",
  );
});

Deno.test("Stripe credentials are redacted in messages, errors and nested context", () => {
  const records: LogRecord[] = [];
  const logger = new StructuredLogger(
    "qbot4k.stripe",
    "INFO",
    (record) => records.push(record),
  );
  logger.error(
    "Stripe rejected sk_test_privateFixture",
    new Error("Invalid rk_live_restrictedFixture"),
    {
      provider_response: ["whsec_signingFixture", "sk_live_otherFixture"],
      STRIPE_SECRET_KEY: "nonstandard-secret-value",
      stripeWebhookSecret: "nonstandard-webhook-value",
    },
  );
  const serialized = JSON.stringify(records);
  for (
    const value of [
      "sk_test_privateFixture",
      "rk_live_restrictedFixture",
      "whsec_signingFixture",
      "sk_live_otherFixture",
      "nonstandard-secret-value",
      "nonstandard-webhook-value",
    ]
  ) {
    assertEquals(serialized.includes(value), false);
  }
  assertEquals(records[0].message, "Stripe rejected [REDACTED]");
});

Deno.test("reward email credentials and claim fields are redacted", () => {
  const records: LogRecord[] = [];
  new StructuredLogger("reward", "INFO", (record) => records.push(record))
    .error("re_fixtureSecret", new Error("re_another-secret"), {
      claimCode: "fixture-code",
      claim_hash: "fixture-hash",
      claim_nonce: "fixture-nonce",
    });
  for (
    const value of [
      "re_fixtureSecret",
      "re_another-secret",
      "fixture-code",
      "fixture-hash",
      "fixture-nonce",
    ]
  ) assertEquals(JSON.stringify(records).includes(value), false);
});
