import { assertEquals } from "jsr:@std/assert@1.0.14";
import { calculateDerivedSignals } from "../src/domain/signals.ts";

Deno.test("derived signals preserve versioned values evidence and explanations", () => {
  const signals = calculateDerivedSignals({
    userId: 7,
    messageCount: 4,
    channelCount: 2,
    platformCount: 2,
    accountCount: 2,
    eligibleMessageCount: 3,
    positiveCount: 1,
    negativeCount: 1,
    negativePoints: 10,
    replyCount: 1,
    welcomePositiveCount: 1,
    welcomeCount: 2,
    welcomeDuplicateCount: 1,
    findingCount: 2,
    severityPoints: 1.6,
    moderationPenaltyPoints: 25,
    windowStart: "2026-09-03T10:00:00+00:00",
    windowEnd: "2026-09-03T11:00:00+00:00",
  }, "2026-09-03T12:00:00+00:00");

  assertEquals(signals.length, 15);
  assertEquals(signals.find((item) => item.signalKey === "risk.composite"), {
    userId: 7,
    signalKey: "risk.composite",
    value: 36.75,
    confidence: 0.2,
    evidenceCount: 4,
    windowStart: "2026-09-03T10:00:00+00:00",
    windowEnd: "2026-09-03T11:00:00+00:00",
    details: {
      unit: "score_0_100",
      negative_ratio: 0.25,
      moderation_rate: 0.5,
      severity_rate: 0.4,
      formula: "negative_ratio*45 + moderation_rate*35 + severity_rate*20",
      independent_of_social_score: true,
    },
    analyzerVersion: 2,
    calculatedAt: "2026-09-03T12:00:00+00:00",
  });
});

Deno.test("empty evidence yields bounded zero-data signals", () => {
  const signals = calculateDerivedSignals({
    userId: 9,
    messageCount: 0,
    channelCount: 0,
    platformCount: 0,
    accountCount: 0,
    eligibleMessageCount: 0,
    positiveCount: 0,
    negativeCount: 0,
    negativePoints: 0,
    replyCount: 0,
    welcomePositiveCount: 0,
    welcomeCount: 0,
    welcomeDuplicateCount: 0,
    findingCount: 0,
    severityPoints: 0,
    moderationPenaltyPoints: 0,
  }, "2026-09-03T12:00:00+00:00");

  const risk = signals.find((item) => item.signalKey === "risk.composite");
  assertEquals(risk?.value, 0);
  assertEquals(risk?.confidence, 0);
  assertEquals(risk?.evidenceCount, 0);
});
