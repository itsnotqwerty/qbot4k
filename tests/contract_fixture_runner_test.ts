import { evaluate } from "./contract_fixture_runner.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      `expected ${JSON.stringify(expected)}, received ${
        JSON.stringify(actual)
      }`,
    );
  }
}

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
