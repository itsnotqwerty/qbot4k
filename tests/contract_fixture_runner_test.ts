import { assertEquals } from "jsr:@std/assert@1.0.14";
import { evaluate } from "./contract_fixture_runner.ts";

Deno.test("tenant authorization rejects a different community", () => {
  const output = evaluate("authorize_cases", [{
    actor_community_id: 2,
    requested_community_id: 1,
    required_capability: "moderation.manage",
    granted_capabilities: ["moderation.manage"],
  }]);
  assertEquals(output, [{ authorized: false, reason: "tenant_mismatch" }]);
});

Deno.test("provider normalization preserves external identity", () => {
  const output = evaluate("normalize_provider", {
    external_event_id: 42,
    platform: " TwItCh ",
    username: " FixtureUser ",
  });
  assertEquals(output, {
    external_event_id: "42",
    platform: "twitch",
    username: "FixtureUser",
  });
});

Deno.test("moderation fixtures use the production rule evaluator", () => {
  const output = evaluate("moderate_message", {
    message: {
      platform: "twitch",
      platform_user_id: "fixture-user",
      username: "FixtureUser",
      channel_id: "fixture-channel",
      content_raw: "BAD visit HTTPS://example.test",
      sent_at: "2026-09-03T10:00:00+00:00",
    },
    rules: [{
      id: 7,
      name: "Blocked term",
      rule_type: "exact_term",
      pattern: "bad",
      severity: "medium",
      auto_enforce_action: null,
      enabled: true,
      enforcement_mode: "shadow",
      action_duration_seconds: 600,
    }],
  });
  assertEquals(output, [{
    rule_id: 7,
    rule_name: "Blocked term",
    rule_type: "exact_term",
    severity: "medium",
    reason_code: "exact_term",
    auto_enforce_action: null,
    enforcement_mode: "shadow",
    action_duration_seconds: 600,
  }]);
});

Deno.test("all frozen domain and job fixtures match Deno outputs", async () => {
  const fixture = JSON.parse(
    await Deno.readTextFile(
      new URL("./fixtures/contracts/golden.json", import.meta.url),
    ),
  ) as {
    scenarios: Array<{
      id: string;
      operation: string;
      input: Parameters<typeof evaluate>[1];
      expected: unknown;
    }>;
  };
  for (const scenario of fixture.scenarios) {
    try {
      assertEquals(
        await evaluate(scenario.operation, scenario.input),
        scenario.expected,
      );
    } catch (error) {
      throw new Error(`fixture ${scenario.id} failed`, { cause: error });
    }
  }
});
