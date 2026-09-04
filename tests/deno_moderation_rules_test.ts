import { assertEquals } from "jsr:@std/assert@1.0.14";
import { NormalizedMessage } from "../src/core/models.ts";
import {
  containsStreambooViewerSpam,
  evaluateEgregiousContent,
  evaluateMessageModeration,
  isEgregiousContent,
  type ModerationRule,
} from "../src/domain/moderation_rules.ts";

function message(
  contentRaw: string,
  options: { moderator?: boolean; previous?: string } = {},
): NormalizedMessage {
  return new NormalizedMessage({
    platform: "twitch",
    platformUserId: "user-1",
    username: "viewer",
    channelId: "channel-1",
    contentRaw,
    sentAt: "2026-09-03T10:00:00+00:00",
    isModerator: options.moderator,
    metadata: options.previous === undefined
      ? {}
      : { previous_normalized_content: options.previous },
  });
}

function rule(
  ruleType: string,
  pattern: string,
  options: { enabled?: boolean; enforcementMode?: string } = {},
): ModerationRule {
  return {
    id: 7,
    name: `fixture:${ruleType}`,
    ruleType,
    pattern,
    severity: "high",
    autoEnforceAction: "timeout",
    enabled: options.enabled ?? true,
    enforcementMode: options.enforcementMode ?? "enforce",
    actionDurationSeconds: 600,
  };
}

Deno.test("moderation evaluates each Python rule type", () => {
  const scenarios: Array<[NormalizedMessage, ModerationRule]> = [
    [message("That BAD idea"), rule("exact_term", "bad")],
    [message("please buy   now"), rule("banned_phrase", String.raw`buy\s+now`)],
    [
      message(
        "cheap viewers at s\u200bt\u200br\u200be\u200ba\u200bm\u200bb\u200bo\u200bo",
      ),
      rule("streamboo_viewer_spam", ""),
    ],
    [message("visit HTTPS://example.test"), rule("link_restriction", "")],
    [
      message(" Same MESSAGE ", { previous: "same message" }),
      rule("duplicate_message", "same_user_same_content"),
    ],
  ];

  for (const [candidate, configuredRule] of scenarios) {
    assertEquals(evaluateMessageModeration(candidate, [configuredRule]), [{
      ruleId: 7,
      ruleName: `fixture:${configuredRule.ruleType}`,
      ruleType: configuredRule.ruleType,
      severity: "high",
      reasonCode: configuredRule.ruleType,
      autoEnforceAction: "timeout",
      enforcementMode: "enforce",
      actionDurationSeconds: 600,
    }]);
  }
});

Deno.test("moderation preserves bypass and invalid-rule behavior", () => {
  const moderator = message("bad https://streamboo.example viewers", {
    moderator: true,
  });
  assertEquals(
    evaluateMessageModeration(moderator, [
      rule("exact_term", "bad"),
      rule("link_restriction", ""),
      rule("streamboo_viewer_spam", ""),
      rule("banned_phrase", "["),
      rule("duplicate_message", "unsupported"),
      rule("exact_term", "bad", { enabled: false }),
      rule("exact_term", "bad", { enforcementMode: "disabled" }),
    ]).map((finding) => finding.ruleType),
    ["exact_term"],
  );
});

Deno.test("exact terms use Python-compatible Unicode word boundaries", () => {
  assertEquals(
    evaluateMessageModeration(message("ébad bad!"), [rule("exact_term", "bad")])
      .length,
    1,
  );
  assertEquals(
    evaluateMessageModeration(message("ébad"), [rule("exact_term", "bad")]),
    [],
  );
});

Deno.test("Streamboo matching tolerates separators and format characters", () => {
  assertEquals(
    containsStreambooViewerSpam("BEST followers at STREAM-B00"),
    true,
  );
  assertEquals(
    containsStreambooViewerSpam(
      "cheap viewers at s\u200bt\u200br\u200be\u200ba\u200bm\u200bb\u200bo\u200bo",
    ),
    true,
  );
  assertEquals(containsStreambooViewerSpam("Get viewers now"), false);
  assertEquals(containsStreambooViewerSpam("streamboo is bad"), false);
});

Deno.test("egregious evaluation preserves boundaries and moderator bypass", () => {
  const configuredRule = rule("egregious_term", "");
  assertEquals(isEgregiousContent("display a swastika"), true);
  assertEquals(isEgregiousContent("naziism"), false);
  assertEquals(
    evaluateEgregiousContent(message("swastika"), configuredRule).length,
    1,
  );
  assertEquals(
    evaluateEgregiousContent(
      message("swastika", { moderator: true }),
      configuredRule,
    ),
    [],
  );
});
