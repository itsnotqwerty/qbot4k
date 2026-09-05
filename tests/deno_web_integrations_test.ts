import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import type {
  IntegrationOAuthGateway,
  IntegrationService,
} from "../src/web/web_integrations.ts";
import { WebIntegrationsController } from "../src/web/web_integrations.ts";

const secret = "integration-test-secret";
async function fixture(
  role = "admin",
  eventsubConfigured = false,
  discordRedirectUri = "http://localhost/integrations/discord/callback",
  twitchRedirectUri = "http://localhost/integrations/twitch/callback",
) {
  const calls: unknown[][] = [];
  const store: OperatorAuthStore = {
    completeLogin: () => Promise.reject(new Error("unused")),
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
    { authenticate: () => Promise.reject(new Error("unused")) },
    store,
  );
  const service: IntegrationService = {
    snapshot: () =>
      Promise.resolve({
        community: { id: 7, name: "Alpha" },
        guilds: [{ guild_id: "123" }],
        installations: [],
      }),
    createDiscordIntent: (input) => {
      calls.push(["discord-intent", input]);
      return Promise.resolve();
    },
    completeDiscordIntent: (state) => {
      calls.push(["discord-complete", state]);
      return Promise.resolve();
    },
    createTwitchIntent: (input) => {
      calls.push(["twitch-intent", input]);
      return Promise.resolve();
    },
    completeTwitchIntent: (...args) => {
      calls.push(["twitch-complete", ...args]);
      return Promise.resolve(4);
    },
    revoke: (...args) => {
      calls.push(["revoke", ...args]);
      return Promise.resolve();
    },
  };
  const gateway: IntegrationOAuthGateway = {
    exchangeDiscord: () => Promise.resolve(),
    exchangeTwitch: () =>
      Promise.resolve({
        accessToken: "access",
        refreshToken: "refresh",
        scopes: ["moderator:read:followers"],
        broadcasterId: "99",
        broadcasterLogin: "channel",
      }),
  };
  const settings = {
    dashboardSessionSecret: secret,
    discordOauthClientId: "discord-client",
    discordOauthClientSecret: "discord-secret",
    discordOauthRedirectUri: discordRedirectUri,
    twitchClientId: "twitch-client",
    twitchClientSecret: "twitch-secret",
    twitchOauthRedirectUri: twitchRedirectUri,
    credentialEncryptionKey: "unused-in-fake",
    twitchEventsubSecret: eventsubConfigured ? "0123456789abcdef" : null,
    twitchEventsubCallbackUrl: eventsubConfigured
      ? "https://example.test/eventsub"
      : null,
  };
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  const controller = new WebIntegrationsController(
    auth,
    service,
    gateway,
    settings,
    eventsubConfigured
      ? {
        reconcile: (input: unknown) => {
          calls.push(["eventsub-reconcile", input]);
          return Promise.resolve();
        },
      }
      : null,
  );
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

Deno.test("integration links persist tenant-bound reviewed intents", async () => {
  const app = await fixture();
  const page = await app.handler(
    new Request("http://localhost/integrations", { headers: app.headers }),
  );
  const html = await page.text();
  assertEquals(page.status, 200);
  assertEquals(html.includes('action="/integrations/discord/link"'), true);
  assertEquals(html.includes('href="/styles.css"'), true);
  assertEquals(html.includes("Add QBot4K to server"), true);
  assertEquals(html.includes("Connect Twitch channel"), true);
  const response = await app.handler(
    new Request("http://localhost/integrations/twitch/link", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "http://localhost",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "broadcaster_login=Channel&scope=moderator%3Aread%3Afollowers",
    }),
  );
  assertEquals(response.status, 302);
  const location = new URL(response.headers.get("location")!);
  assertEquals(location.hostname, "id.twitch.tv");
  assertEquals(location.searchParams.get("scope"), "moderator:read:followers");
  assertEquals((app.calls[0]?.[1] as { communityId: number }).communityId, 7);
});

Deno.test("proxied link posts accept forwarded origin headers", async () => {
  const app = await fixture();
  const proxiedHeaders = {
    ...app.headers,
    origin: "https://qbot4k.dev",
    host: "qbot4k.dev",
    "x-forwarded-proto": "https",
    "content-type": "application/x-www-form-urlencoded",
  };
  const twitch = await app.handler(
    new Request("http://127.0.0.1:8080/integrations/twitch/link", {
      method: "POST",
      headers: proxiedHeaders,
      body: "broadcaster_login=Channel&scope=moderator%3Aread%3Afollowers",
    }),
  );
  assertEquals(twitch.status, 302);
  const discord = await app.handler(
    new Request("http://127.0.0.1:8080/integrations/discord/link", {
      method: "POST",
      headers: proxiedHeaders,
      body: "community_id=7&guild_id=123",
    }),
  );
  assertEquals(discord.status, 302);
});

Deno.test("link posts accept null origin from same-site form navigation", async () => {
  const app = await fixture();
  const response = await app.handler(
    new Request("https://qbot4k.dev/integrations/discord/link", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "null",
        "sec-fetch-site": "same-origin",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "community_id=7&guild_id=123",
    }),
  );
  assertEquals(response.status, 302);
});

Deno.test("Discord installation uses its callback when login OAuth is configured", async () => {
  const app = await fixture(
    "admin",
    false,
    "https://qbot4k.dev/oauth/discord/callback",
  );
  const response = await app.handler(
    new Request("http://localhost/integrations/discord/link", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "http://localhost",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "community_id=7&guild_id=123",
    }),
  );
  assertEquals(response.status, 302);
  const location = new URL(response.headers.get("location")!);
  assertEquals(
    location.searchParams.get("redirect_uri"),
    "https://qbot4k.dev/integrations/discord/callback",
  );
});

Deno.test("Twitch link ignores stale localhost callback configuration", async () => {
  const app = await fixture(
    "admin",
    false,
    undefined,
    "http://localhost/integrations/twitch/callback",
  );
  const response = await app.handler(
    new Request("https://qbot4k.dev/integrations/twitch/link", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "https://qbot4k.dev",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "broadcaster_login=channel&scope=moderator%3Aread%3Afollowers",
    }),
  );
  assertEquals(response.status, 302);
  const location = new URL(response.headers.get("location")!);
  assertEquals(
    location.searchParams.get("redirect_uri"),
    "https://qbot4k.dev/integrations/twitch/callback",
  );
});

Deno.test("integration revocation requires exact confirmation and admin role", async () => {
  const admin = await fixture();
  const headers = {
    ...admin.headers,
    origin: "http://localhost",
    "content-type": "application/json",
  };
  const denied = await admin.handler(
    new Request("http://localhost/api/integrations/4/revoke", {
      method: "POST",
      headers,
      body: JSON.stringify({ confirmation: "wrong" }),
    }),
  );
  assertEquals(denied.status, 409);
  const accepted = await admin.handler(
    new Request("http://localhost/api/integrations/4/revoke", {
      method: "POST",
      headers,
      body: JSON.stringify({ confirmation: "REVOKE INTEGRATION 4" }),
    }),
  );
  assertEquals(accepted.status, 200);
  assertEquals(admin.calls[0], ["revoke", 7, 42, 4]);
  const viewer = await fixture("viewer");
  assertEquals(
    (await viewer.handler(
      new Request("http://localhost/api/integrations/4/revoke", {
        method: "POST",
        headers: { ...viewer.headers, origin: "http://localhost" },
      }),
    )).status,
    403,
  );
});

Deno.test("Twitch OAuth completion reconciles configured EventSub", async () => {
  const app = await fixture("admin", true);
  const link = await app.handler(
    new Request("http://localhost/integrations/twitch/link", {
      method: "POST",
      headers: {
        ...app.headers,
        origin: "http://localhost",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "broadcaster_login=channel&scope=moderator%3Aread%3Afollowers",
    }),
  );
  const state = new URL(link.headers.get("location")!).searchParams.get(
    "state",
  )!;
  const callback = await app.handler(
    new Request(
      `http://localhost/integrations/twitch/callback?code=code-1&state=${
        encodeURIComponent(state)
      }`,
      { headers: app.headers },
    ),
  );
  assertEquals(callback.status, 302);
  assertEquals(app.calls[1][0], "twitch-complete");
  assertEquals(app.calls[2][0], "eventsub-reconcile");
  const input = app.calls[2][1] as {
    communityId: number;
    installationId: number;
    grant: { broadcasterId: string };
  };
  assertEquals(input.communityId, 7);
  assertEquals(input.installationId, 4);
  assertEquals(input.grant.broadcasterId, "99");
});
