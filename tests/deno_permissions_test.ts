import {
  permissionDecision,
  platformCapabilities,
  roleAllows,
} from "../src/security/permissions.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      `expected ${JSON.stringify(expected)}, received ${
        JSON.stringify(actual)
      }`,
    );
  }
}

Deno.test("role capabilities match the canonical permission catalog", () => {
  assertEquals(roleAllows("viewer", "community.read"), true);
  assertEquals(roleAllows("moderator", "live_ops.manage"), true);
  assertEquals(roleAllows("moderator", "moderation.bulk"), false);
  assertEquals(roleAllows("admin", "unknown.capability"), true);
});

Deno.test("permission overrides take precedence over role capabilities", () => {
  assertEquals(permissionDecision("admin", "audit.read", "deny"), false);
  assertEquals(permissionDecision("viewer", "audit.read", "grant"), true);
  assertEquals(permissionDecision(null, "dashboard.access"), false);
});

Deno.test("platform capabilities normalize known providers", () => {
  assertEquals(platformCapabilities(" Twitch "), [
    "events",
    "moderation_actions",
    "member_lifecycle",
    "announcements",
    "live_controls",
  ]);
  assertEquals(platformCapabilities("unknown"), []);
});
