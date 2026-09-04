import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import type { AnnouncementService } from "../src/web/web_announcements.ts";
import { WebAnnouncementsController } from "../src/web/web_announcements.ts";

const secret = "announcement-test-secret";

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
  const service: AnnouncementService = {
    list: (communityId) =>
      Promise.resolve({
        community: {
          name: `Community ${communityId}`,
          timezone: "America/New_York",
        },
        installations: [{ id: 3, display_name: "Discord" }],
        items: [{
          id: 5,
          target_external_id: "general",
          body: "Hello",
          status: "draft",
          attempt_count: 0,
        }],
      }),
    create: (...args) => {
      calls.push([...args]);
      return Promise.resolve(5);
    },
    transition: (...args) => {
      calls.push([...args]);
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
  const controller = new WebAnnouncementsController(auth, service);
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
      controller,
    ).handler(),
  };
}

Deno.test("announcements are tenant-bound and usable without JavaScript", async () => {
  const app = await fixture();
  const page = await app.handler(
    new Request("http://localhost/announcements", { headers: app.headers }),
  );
  const html = await page.text();
  assertEquals(page.status, 200);
  assertEquals(html.includes("Community 7"), true);
  assertEquals(html.includes('action="/announcements/5/approve"'), true);
  const response = await app.handler(
    new Request("http://localhost/announcements/5/approve", {
      method: "POST",
      headers: {
        ...app.headers,
        "content-type": "application/x-www-form-urlencoded",
        origin: "http://localhost",
      },
      body: "scheduled_at=2026-04-01T12%3A00",
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/announcements?status=Announcement%20scheduled",
  );
  assertEquals(app.calls[0], [7, 42, 5, "approve", "2026-04-01T12:00"]);
});

Deno.test("announcement mutations require an administrator", async () => {
  const app = await fixture("viewer");
  const response = await app.handler(
    new Request("http://localhost/announcements", {
      method: "POST",
      headers: { ...app.headers, origin: "http://localhost" },
    }),
  );
  assertEquals(response.status, 403);
  assertEquals(app.calls.length, 0);
});
