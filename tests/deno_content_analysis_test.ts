import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  CONTENT_ANALYZER_VERSION,
  understandContent,
} from "../src/domain/content_analysis.ts";

Deno.test("content analysis preserves threat intent entities and context", () => {
  const result = understandContent(
    "I will hurt Alice Smith at home alice@example.com",
    { referenced_message_id: 42, thread_id: "thread-7" },
  );
  assertEquals(CONTENT_ANALYZER_VERSION, 3);
  assertEquals(result.languageCode, "pt");
  assertEquals(result.languageConfidence, 0.55);
  assertEquals(result.sentimentLabel, "negative");
  assertEquals(result.sentimentScore, -0.3162);
  assertEquals(result.intentLabel, "threat");
  assertEquals(result.threatLevel, "critical");
  assertEquals(result.threatScore, 0.95);
  assertEquals(result.indicators, [
    "direct_future_threat",
    "possible_personal_data_exposure",
  ]);
  assertEquals(result.conversation, {
    is_question: false,
    reply_to: "42",
    thread_id: "thread-7",
    mentioned_accounts: [],
  });
  assertEquals(result.entities, [
    ["email", "alice@example.com", "alice@example.com", 0.92, 32, 49],
    ["named_entity", "Alice Smith", "alice smith", 0.62, 12, 23],
  ]);
});

Deno.test("content analysis preserves question precedence and mention normalization", () => {
  const result = understandContent("Could you help @Analyst with this?", {
    message_reference_id: "message-1",
  });
  assertEquals(result.intentLabel, "question");
  assertEquals(result.intentConfidence, 0.85);
  assertEquals(result.entities, [
    ["mention", "Analyst", "analyst", 0.92, 15, 23],
    ["named_entity", "Could", "could", 0.62, 0, 5],
    ["named_entity", "Analyst", "analyst", 0.62, 16, 23],
  ]);
  assertEquals(result.conversation.mentioned_accounts, ["Analyst"]);
});
