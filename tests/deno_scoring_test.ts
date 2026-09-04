import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  averageSocialScores,
  calculateTemporalRisk,
  clampSocialScore,
  defaultSocialScoreForName,
  enforcedSocialScoreForName,
  isPoweruserScore,
  scoreDeltaForMessage,
  scoreDeltaForModeration,
} from "../src/domain/scoring.ts";

Deno.test("social score bounds and averaging match Python", () => {
  assertEquals(clampSocialScore(349), 350);
  assertEquals(clampSocialScore(901), 900);
  assertEquals(isPoweruserScore(699), false);
  assertEquals(isPoweruserScore(700), true);
  assertEquals(averageSocialScores(700, 701), 700);
  assertEquals(averageSocialScores(701, 702), 702);
  assertEquals(defaultSocialScoreForName("ignored"), 500);
  assertEquals(enforcedSocialScoreForName("ignored", 1200), 900);
});

Deno.test("message scoring preserves command and sentiment behavior", () => {
  assertEquals(scoreDeltaForMessage("  "), null);
  assertEquals(scoreDeltaForMessage("!verify evidence"), null);
  assertEquals(scoreDeltaForMessage("/help"), null);
  assertEquals(scoreDeltaForMessage("thanks, great stream"), [
    1,
    "positive_message",
  ]);
  assertEquals(scoreDeltaForMessage("ordinary message"), [1, "message_sent"]);
});

Deno.test("message scoring keeps conservative negative matching", () => {
  assertEquals(scoreDeltaForMessage("you are an ashole"), [
    -10,
    "very_negative_content",
  ]);
  assertEquals(scoreDeltaForMessage("you are an asshole"), [
    -10,
    "very_negative_content",
  ]);
  assertEquals(
    scoreDeltaForMessage("How many viewers are watching before stream?"),
    [1, "message_sent"],
  );
  assertEquals(
    scoreDeltaForMessage("It is harder to grow on Twitch than YouTube."),
    [1, "message_sent"],
  );
  assertEquals(
    scoreDeltaForMessage(
      "Our church group has asian and american members, and we discussed welfare policy.",
    ),
    [1, "message_sent"],
  );
});

Deno.test("moderation score deltas preserve severity and action penalties", () => {
  assertEquals(
    scoreDeltaForModeration({ severity: "high", reasonCode: "egregious_term" }),
    [-20, "moderation_penalty"],
  );
  assertEquals(
    scoreDeltaForModeration({ severity: "high", actionType: "ban" }),
    [-70, "moderation_penalty"],
  );
  assertEquals(scoreDeltaForModeration({ severity: "unknown" }), [
    -25,
    "moderation_penalty",
  ]);
});

Deno.test("temporal risk requires a supported signal window", () => {
  assertEquals(calculateTemporalRisk([]), null);
  assertEquals(
    calculateTemporalRisk([{
      windowName: "unknown",
      value: 99,
      confidence: 1,
      evidenceCount: 99,
    }]),
    null,
  );
});
