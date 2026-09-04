import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  antiAbuseWindowKey,
  calculateJoinRaidFinding,
  calculateMessageAbuseFindings,
  validateAntiAbusePolicy,
} from "../src/domain/abuse.ts";

const policy = validateAntiAbusePolicy({
  enabled: true,
  enforcementMode: " EnFoRcE ",
  messageBurstLimit: 2.9,
  messageBurstWindowSeconds: 30.8,
  mentionLimit: 2,
  joinRaidLimit: 3,
  joinRaidWindowSeconds: 60,
});

Deno.test("anti-abuse policy normalizes mode and integer bounds", () => {
  assertEquals(policy.enforcementMode, "enforce");
  assertEquals(policy.messageBurstLimit, 2);
  assertEquals(policy.messageBurstWindowSeconds, 30);
  assertThrows(
    () =>
      validateAntiAbusePolicy({
        ...policy,
        enforcementMode: "audit",
        mentionLimit: 0,
      }),
    TypeError,
    "anti-abuse enforcement mode",
  );
});

Deno.test("message findings preserve inclusive thresholds order and moderator bypass", () => {
  const input = {
    communityId: 7,
    platformAccountId: 42,
    occurredAt: "2026-08-06T05:01:00Z",
    recentMessageCount: 2,
    mentionCount: 2,
    isModerator: false,
  };
  assertEquals(
    calculateMessageAbuseFindings(policy, input).map((finding) =>
      finding.reasonCode
    ),
    ["message_flood", "mention_spam"],
  );
  assertEquals(
    calculateMessageAbuseFindings(policy, { ...input, isModerator: true }),
    [],
  );
});

Deno.test("join raids and dedupe keys align to policy windows", () => {
  assertEquals(
    calculateJoinRaidFinding(policy, {
      communityId: 7,
      occurredAt: "2026-08-06T05:02:02Z",
      joinCount: 2,
    }),
    null,
  );
  assertEquals(
    antiAbuseWindowKey("join_raid", 7, 0, "2026-08-06T05:02:02Z", 60),
    "anti-abuse:7:join_raid:0:29766542",
  );
});
