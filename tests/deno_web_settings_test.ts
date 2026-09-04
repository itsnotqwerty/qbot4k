import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import type { SettingsService } from "../src/web/web_settings.ts";
import { WebSettingsController } from "../src/web/web_settings.ts";

const secret = "settings-test-secret";

async function fixture(role = "admin") {
  const calls: unknown[][] = [];
  const store: OperatorAuthStore = {
    completeLogin: () => Promise.reject(new Error("not used")),
    switchCommunity: () => Promise.resolve(null),
    auditLogout: () => Promise.resolve(),
    resolveSession: () =>
      Promise.resolve({
        status: "active",
        sessionVersion: 2,
        memberships: [{ id: 7, name: "Alpha", slug: "alpha", role }],
      }),
  };
  const auth = new WebAuthController(
    {
      dashboardSessionSecret: secret,
      discordOauthClientId: null,
      discordOauthClientSecret: null,
      discordOauthRedirectUri: null,
      operatorGuildIds: [],
    },
    { authenticate: () => Promise.reject(new Error("not used")) },
    store,
  );
  const service: SettingsService = {
    snapshot: () =>
      Promise.resolve({
        community: {
          name: "Alpha",
          slug: "alpha",
          locale: "en-US",
          timezone: "UTC",
        },
        policy: {
          message_retention_days: 30,
          analytics_retention_days: 90,
          anti_abuse_enforcement_mode: "shadow",
          message_burst_limit: 10,
          message_burst_window_seconds: 5,
          mention_limit: 5,
          join_raid_limit: 10,
          join_raid_window_seconds: 60,
        },
        installations: [],
        destinations: [],
        operators: [],
        invitations: [],
      }),
    update: (...args) => {
      calls.push(["update", ...args]);
      return Promise.resolve();
    },
    invite: (...args) => {
      calls.push(["invite", ...args]);
      return Promise.resolve(9);
    },
    access: (...args) => {
      calls.push(["access", ...args]);
      return Promise.resolve();
    },
  };
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  const controller = new WebSettingsController(auth, service);
  return {
    calls,
    headers: { cookie: `qbot4k_session=${cookie}` },
    handler: createApp(
      undefined,
      auth,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      controller,
    ).handler(),
  };
}

Deno.test("settings page and profile form work without JavaScript", async () => {
  const app = await fixture("viewer");
  const page = await app.handler(
    new Request("http://localhost/settings", { headers: app.headers }),
  );
  const html = await page.text();
  assertEquals(page.status, 200);
  assertEquals(html.includes('action="/settings"'), true);
  assertEquals(html.includes("Invite operator"), false);
  const response = await app.handler(
    new Request("http://localhost/settings", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "http://localhost",
        "content-type": "application/x-www-form-urlencoded",
      },
      body:
        "name=Alpha&locale=en-US&timezone=UTC&message_retention_days=30&analytics_retention_days=90&anti_abuse_enforcement_mode=shadow&message_burst_limit=10&message_burst_window_seconds=5&mention_limit=5&join_raid_limit=10&join_raid_window_seconds=60",
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/settings?status=Settings%20saved",
  );
  assertEquals(app.calls[0]?.slice(0, 3), ["update", 7, 42]);
});

Deno.test("operator invitations and ownership confirmation are admin-only", async () => {
  const admin = await fixture();
  const headers = {
    ...admin.headers,
    origin: "http://localhost",
    "content-type": "application/json",
  };
  const invitation = await admin.handler(
    new Request("http://localhost/api/operators/invitations", {
      method: "POST",
      headers,
      body: JSON.stringify({
        discord_user_id: "123",
        role: "moderator",
        expires_hours: 24,
      }),
    }),
  );
  assertEquals(invitation.status, 201);
  assertEquals(await invitation.json(), { invitation_id: 9 });
  const denied = await admin.handler(
    new Request("http://localhost/api/operators/8/transfer-ownership", {
      method: "POST",
      headers,
      body: JSON.stringify({ confirmation: "wrong" }),
    }),
  );
  assertEquals(denied.status, 409);
  assertEquals(admin.calls.length, 1);
  const accepted = await admin.handler(
    new Request("http://localhost/api/operators/8/transfer-ownership", {
      method: "POST",
      headers,
      body: JSON.stringify({ confirmation: "TRANSFER OWNERSHIP 8" }),
    }),
  );
  assertEquals(accepted.status, 200);
  assertEquals(admin.calls[1], ["access", 7, 42, 8, "transfer-ownership", ""]);
  const viewer = await fixture("viewer");
  assertEquals(
    (await viewer.handler(
      new Request("http://localhost/api/operators/invitations", {
        method: "POST",
        headers: { ...viewer.headers, origin: "http://localhost" },
      }),
    )).status,
    403,
  );
});
