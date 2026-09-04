import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import { ActorAttribution, TenantContext } from "../src/core/contexts.ts";
import {
  coerceTimestamp,
  NormalizedMessage,
  normalizedMessageFromObservation,
  normalizeMessageContent,
  observationFromMessage,
  processingJobFromRow,
} from "../src/core/models.ts";

Deno.test("tenant context requires positive identifiers", () => {
  for (const communityId of [null, "", 0, -1]) {
    assertThrows(() => TenantContext.require(communityId));
  }
  assertThrows(() => TenantContext.require(1, { installationId: 0 }));
  const tenant = TenantContext.require("2", { installationId: "3" });
  assertEquals([tenant.communityId, tenant.installationId], [2, 3]);
});

Deno.test("operator attribution requires actor identity", () => {
  assertThrows(() => new ActorAttribution("operator"));
  assertThrows(() => new ActorAttribution("operator", 0));
  const actor = new ActorAttribution(" Operator ", 7);
  assertEquals([actor.actorType, actor.actorId], ["operator", 7]);
});

Deno.test("normalized messages preserve observation tenant context", () => {
  assertEquals(normalizeMessageContent("  Hello\n  WORLD  "), "hello world");
  assertEquals(normalizeMessageContent("Straße Σς İ"), "strasse σσ i̇");
  const message = new NormalizedMessage({
    platform: "discord",
    platformMessageId: "message-2",
    platformUserId: "target-2",
    username: "target",
    channelId: "channel-2",
    contentRaw: "  Hello\n  WORLD  ",
    sentAt: "2026-01-02T03:04:05+00:00",
    roleNames: ["moderator"],
    isModerator: true,
    metadata: { community_id: 2, installation_id: 4 },
  });
  const observation = observationFromMessage(message);

  assertEquals(message.contentNormalized, "hello world");
  assertEquals(observation.communityId, 2);
  assertEquals(observation.installationId, 4);
  assertEquals(observation.attributes.role_names, ["moderator"]);
  assertEquals(observation.attributes.is_moderator, true);
});

Deno.test("message observations decode with Python-compatible attributes", () => {
  const message = normalizedMessageFromObservation({
    platform: "discord",
    event_type: "message.created",
    external_event_id: "message-3",
    actor_platform_user_id: "user-3",
    actor_username: "name",
    container_id: "channel-3",
    context_id: null,
    text_raw: null,
    occurred_at: "2026-01-02T03:04:05+00:00",
    attributes_json:
      '{"role_names":["moderator",7],"is_moderator":[],"source":"fixture"}',
  });

  assertEquals(message.roleNames, ["moderator", "7"]);
  assertEquals(message.isModerator, false);
  assertEquals(message.contentRaw, "");
  assertEquals(message.metadata, { source: "fixture" });
  assertThrows(
    () =>
      normalizedMessageFromObservation({
        event_type: "user.joined",
        attributes_json: "{}",
      }),
    TypeError,
    "Cannot construct a message from user.joined",
  );
  assertThrows(
    () =>
      normalizedMessageFromObservation({
        event_type: "message.created",
        attributes_json: "[]",
      }),
    TypeError,
    "Observation attributes must be a JSON object",
  );
});

Deno.test("observation conversion rejects missing tenant context", () => {
  const message = new NormalizedMessage({
    platform: "discord",
    platformUserId: "target",
    username: "target",
    channelId: "channel",
    contentRaw: "hello",
    sentAt: "2026-01-02T03:04:05+00:00",
  });
  assertThrows(
    () => observationFromMessage(message),
    TypeError,
    "community_id is required",
  );
});

Deno.test("timestamps and processing rows decode into shared models", () => {
  assertEquals(
    coerceTimestamp("2026-01-02T03:04:05Z"),
    "2026-01-02T03:04:05.000+00:00",
  );
  const job = processingJobFromRow({
    id: 9,
    community_id: 2,
    stage: "analysis",
    job_type: "score",
    observation_id: null,
    payload_json: '{"source":"fixture"}',
    attempts: 1,
    max_attempts: 3,
  });
  assertEquals(job.communityId, 2);
  assertEquals(job.payload, { source: "fixture" });
  assertThrows(
    () => processingJobFromRow({ community_id: 1, payload_json: "[]" }),
    TypeError,
    "Processing job payload must be a JSON object",
  );
});
