import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import {
  createOauthState,
  createSessionCookie,
} from "../src/security/security.ts";
import {
  determineOperatorRole,
  type DiscordIdentity,
  type OperatorAuthStore,
  type OperatorMembership,
  WebAuthController,
} from "../src/web/web_auth.ts";

const now = new Date("2026-01-02T03:04:05.000Z");
const settings = {
  dashboardSessionSecret: "test-session-secret",
  discordOauthClientId: "discord-client",
  discordOauthClientSecret: "discord-secret",
  discordOauthRedirectUri: "https://qbot.example/oauth/discord/callback",
  operatorGuildIds: ["guild-1"],
};
const identity: DiscordIdentity = {
  userId: "9001",
  username: "Operator",
  guildIds: ["guild-1"],
  permissions: { "guild-1": "8" },
  guildNames: { "guild-1": "Alpha Guild" },
  ownedGuildIds: [],
};
const memberships: readonly OperatorMembership[] = [
  { id: 7, name: "Alpha", slug: "alpha", role: "owner" },
  { id: 8, name: "Beta", slug: "beta", role: "moderator" },
];

class FakeOperatorStore implements OperatorAuthStore {
  readonly logins: string[] = [];
  readonly switches: number[] = [];
  readonly logouts: number[] = [];

  completeLogin(received: DiscordIdentity, role: string) {
    this.logins.push(`${received.userId}:${role}`);
    return Promise.resolve({
      operatorId: 42,
      status: "active",
      sessionVersion: 3,
      memberships,
    });
  }

  switchCommunity(operatorId: number, communityId: number) {
    this.switches.push(communityId);
    return Promise.resolve(
      operatorId === 42
        ? memberships.find((item) => item.id === communityId)?.role ?? null
        : null,
    );
  }

  auditLogout(operatorId: number) {
    this.logouts.push(operatorId);
    return Promise.resolve();
  }

  resolveSession(operatorId: number) {
    return Promise.resolve(
      operatorId === 42
        ? { status: "active", sessionVersion: 3, memberships }
        : null,
    );
  }
}

function fixture() {
  const store = new FakeOperatorStore();
  const auth = new WebAuthController(
    settings,
    { authenticate: () => Promise.resolve(identity) },
    store,
    () => now,
  );
  return { handler: createApp(undefined, auth).handler(), store };
}

function cookieFrom(response: Response, name: string): string {
  const header = response.headers.get("set-cookie") ?? "";
  const match = header.match(new RegExp(`${name}=([^;,]+)`));
  return match?.[1] ?? "";
}

Deno.test("Discord role detection preserves admin bits in large permission values", () => {
  assertEquals(
    determineOperatorRole({
      ...identity,
      permissions: { "guild-1": "1152921504606846984" },
    }, ["guild-1"]),
    "admin",
  );
});

Deno.test("Discord login redirects with a signed state cookie", async () => {
  const { handler } = fixture();
  const response = await handler(new Request("https://qbot.example/login"));
  assertEquals(response.status, 302);
  const target = new URL(response.headers.get("location")!);
  assertEquals(target.origin, "https://discord.com");
  assertEquals(target.searchParams.get("scope"), "identify guilds");
  assertEquals(
    target.searchParams.get("redirect_uri"),
    settings.discordOauthRedirectUri,
  );
  assertEquals(
    cookieFrom(response, "qbot4k_oauth_state"),
    target.searchParams.get("state"),
  );
});

Deno.test("both Discord callback paths create a tenant-scoped session", async () => {
  for (const path of ["/auth/discord/callback", "/oauth/discord/callback"]) {
    const { handler, store } = fixture();
    const state = await createOauthState(
      settings.dashboardSessionSecret,
      "callback-state",
    );
    const response = await handler(
      new Request(
        `https://qbot.example${path}?code=code-1&state=${
          encodeURIComponent(state)
        }`,
      ),
    );
    assertEquals(response.status, 302);
    assertEquals(response.headers.get("location"), "/dashboard");
    assertEquals(cookieFrom(response, "qbot4k_session").length > 40, true);
    assertEquals(store.logins, ["9001:admin"]);
  }
});

Deno.test("Discord callback rejects invalid state and unauthorized guilds", async () => {
  const { handler } = fixture();
  const invalid = await handler(
    new Request(
      "https://qbot.example/oauth/discord/callback?code=code-1&state=invalid",
    ),
  );
  assertEquals(invalid.status, 400);

  const store = new FakeOperatorStore();
  const auth = new WebAuthController(
    settings,
    {
      authenticate: () => Promise.resolve({ ...identity, guildIds: ["other"] }),
    },
    store,
    () => now,
  );
  const state = await createOauthState(
    settings.dashboardSessionSecret,
    "denied-state",
  );
  const denied = await createApp(undefined, auth).handler()(
    new Request(
      `https://qbot.example/oauth/discord/callback?code=code-1&state=${
        encodeURIComponent(state)
      }`,
    ),
  );
  assertEquals(denied.status, 403);
  assertEquals(store.logins, []);
});

Deno.test("dashboard and community switch enforce the signed tenant session", async () => {
  const { handler, store } = fixture();
  const session = await createSessionCookie(settings.dashboardSessionSecret, {
    userId: "42",
    username: "Operator",
    role: "owner",
    expiresAt: "2026-01-02T15:04:05.000Z",
    communityId: 7,
    sessionVersion: 3,
  });
  const dashboard = await handler(
    new Request("https://qbot.example/dashboard", {
      headers: { cookie: `qbot4k_session=${session}` },
    }),
  );
  assertEquals(dashboard.status, 200);
  const html = await dashboard.text();
  assertEquals(
    html.includes('form method="post" action="/community/switch"'),
    true,
  );
  assertEquals(
    html.includes('<option value="7" selected>Alpha</option>'),
    true,
  );
  assertEquals(html.includes('method="post" action="/logout"'), true);

  const health = await handler(
    new Request("https://qbot.example/system-health", {
      headers: { cookie: `qbot4k_session=${session}` },
    }),
  );
  assertEquals(health.status, 200);
  assertEquals((await health.text()).includes("System health"), true);

  const switched = await handler(
    new Request("https://qbot.example/community/switch", {
      method: "POST",
      headers: {
        cookie: `qbot4k_session=${session}`,
        "content-type": "application/x-www-form-urlencoded",
        origin: "https://qbot.example",
      },
      body: "community_id=8",
    }),
  );
  assertEquals(switched.status, 302);
  assertEquals(switched.headers.get("location"), "/dashboard");
  assertEquals(store.switches, [8]);

  const denied = await handler(
    new Request("https://qbot.example/community/switch", {
      method: "POST",
      headers: {
        cookie: `qbot4k_session=${session}`,
        "content-type": "application/x-www-form-urlencoded",
        origin: "https://qbot.example",
      },
      body: "community_id=99",
    }),
  );
  assertEquals(denied.status, 403);

  const originMismatch = await handler(
    new Request("https://qbot.example/community/switch", {
      method: "POST",
      headers: {
        cookie: `qbot4k_session=${session}`,
        "content-type": "application/x-www-form-urlencoded",
        origin: "https://attacker.example",
      },
      body: "community_id=8",
    }),
  );
  assertEquals(originMismatch.status, 403);
});

Deno.test("logout audits the operator and clears the session", async () => {
  const { handler, store } = fixture();
  const session = await createSessionCookie(settings.dashboardSessionSecret, {
    userId: "42",
    username: "Operator",
    role: "owner",
    expiresAt: "2026-01-02T15:04:05.000Z",
    communityId: 7,
    sessionVersion: 3,
  });
  const response = await handler(
    new Request("https://qbot.example/logout", {
      method: "POST",
      headers: {
        cookie: `qbot4k_session=${session}`,
        origin: "https://qbot.example",
      },
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(response.headers.get("location"), "/login");
  assertEquals(response.headers.get("set-cookie")?.includes("Max-Age=0"), true);
  assertEquals(store.logouts, [42]);
});
