import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  DASHBOARD_SURFACE_POLICIES,
  INSTALLATION_CAPABILITY_BY_SURFACE,
  NON_HTTP_SURFACE_POLICIES,
  requireNonHttpSurface,
} from "../src/security/surface_policy.ts";

Deno.test("non-HTTP surface inventory matches the frozen contract", () => {
  assertEquals([...NON_HTTP_SURFACE_POLICIES.keys()].sort(), [
    "command:addcom",
    "command:alias",
    "command:credit",
    "command:custom",
    "command:delcom",
    "command:editcom",
    "command:verify",
    "job:maintenance",
    "job:onboarding_checkpoints",
    "job:onboarding_roles",
    "job:scheduled_announcements",
    "job:twitch_live_announcements",
    "lookup:installation",
    "lookup:member",
    "lookup:moderation",
    "provider:announcement",
    "provider:live_control",
    "provider:moderation",
  ]);
  assertEquals(
    new Set(
      [...NON_HTTP_SURFACE_POLICIES.values()].map((policy) => policy.kind),
    ),
    new Set(["bot_command", "job", "direct_lookup", "provider_action"]),
  );
});

Deno.test("surface guards reject mismatches", () => {
  assertEquals(
    requireNonHttpSurface("job:maintenance", "system").scope,
    "community",
  );
  assertThrows(
    () => requireNonHttpSurface("job:maintenance", "session"),
    Deno.errors.PermissionDenied,
    "surface job:maintenance requires system",
  );
  assertThrows(() => requireNonHttpSurface("job:unknown", "system"), TypeError);
});

Deno.test("dashboard and provider policies preserve classifications", () => {
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_billing_checkout"), {
    capability: "billing.manage",
    guard: "organization_billing_session",
    scope: "organization",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_stripe_webhook"), {
    capability: "billing.events.ingest",
    guard: "stripe_signature",
    scope: "global",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_account"), {
    capability: "account.access",
    guard: "account_session",
    scope: "account",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_account_communities"), {
    capability: "account.access",
    guard: "account_session",
    scope: "account",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_dashboard_restart"), {
    capability: "platform.manage",
    guard: "platform_session",
    scope: "global",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_public_home"), {
    capability: "public.access",
    guard: "public",
    scope: "global",
    kind: "http",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_live_ops_stream"), {
    capability: "live_ops.read",
    guard: "session",
    scope: "community",
    kind: "sse",
  });
  assertEquals(DASHBOARD_SURFACE_POLICIES.get("_serve_twitch_eventsub"), {
    capability: "events.ingest",
    guard: "webhook_signature",
    scope: "installation",
    kind: "http",
  });
  assertEquals(
    INSTALLATION_CAPABILITY_BY_SURFACE.get("provider:moderation"),
    "moderation_actions",
  );
});
