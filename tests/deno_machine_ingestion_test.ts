import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { CollectedObservation, Observation } from "../src/core/models.ts";
import type { ObservationCollector } from "../src/domain/observations.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import {
  MachineIngestionController,
  type MachineIngestionService,
  PostgresMachineIngestionRepository,
} from "../src/jobs/machine_ingestion.ts";

const secret = "eventsub-test-secret";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]>) {}
  query(
    sql: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.responses.shift() ?? []);
  }
  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

function fixture(options: { apiAuthorized?: boolean } = {}) {
  const observations: Observation[] = [];
  const calls: unknown[][] = [];
  const authStore: OperatorAuthStore = {
    completeLogin: () => Promise.reject(new Error("unused")),
    switchCommunity: () => Promise.resolve(null),
    auditLogout: () => Promise.resolve(),
    resolveSession: () => Promise.resolve(null),
  };
  const auth = new WebAuthController(
    {
      dashboardSessionSecret: "session",
      discordOauthClientId: null,
      discordOauthClientSecret: null,
      discordOauthRedirectUri: null,
      operatorGuildIds: [],
    },
    { authenticate: () => Promise.reject(new Error("unused")) },
    authStore,
  );
  const service: MachineIngestionService = {
    authorizeApiClient: (token, communityId) => {
      calls.push(["authorize", token, communityId]);
      return Promise.resolve(options.apiAuthorized ?? true);
    },
    resolveTwitchInstallation: (broadcasterId) => {
      calls.push(["resolve", broadcasterId]);
      return Promise.resolve({ communityId: 7, installationId: 4 });
    },
    recordSubscription: (communityId, subscription) => {
      calls.push(["subscription", communityId, subscription.id]);
      return Promise.resolve();
    },
    markSubscription: (subscriptionId, status) => {
      calls.push(["mark", subscriptionId, status]);
      return Promise.resolve();
    },
    upsertExternalSource: (input) => {
      calls.push(["external", input.sourceKey]);
      return Promise.resolve();
    },
  };
  const collector: ObservationCollector = {
    collect: (observation) => {
      observations.push(observation);
      return Promise.resolve<CollectedObservation>({
        observationId: 12,
        status: "persisted",
        analysisJobId: 15,
      });
    },
  };
  const controller = new MachineIngestionController(auth, service, collector, {
    twitchEventsubSecret: secret,
  });
  const handler = createApp(
    undefined,
    auth,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    controller,
  ).handler();
  return { calls, observations, handler };
}

Deno.test("API event ingestion binds a community-scoped bearer client", async () => {
  const app = await fixture();
  const body = JSON.stringify({
    community_id: 7,
    platform: "discord",
    event_type: "member.joined",
    external_event_id: "join-1",
    actor_platform_user_id: "user-1",
  });
  const response = await app.handler(
    new Request("http://localhost/api/events", {
      method: "POST",
      headers: {
        authorization: "Bearer client-secret",
        "content-type": "application/json",
        "content-length": String(body.length),
      },
      body,
    }),
  );
  assertEquals(response.status, 201);
  assertEquals(await response.json(), {
    status: "persisted",
    observation_id: 12,
    analysis_job_id: 15,
  });
  assertEquals(app.calls[0], ["authorize", "client-secret", 7]);
  assertEquals(app.observations[0].communityId, 7);
});

Deno.test("API event ingestion rejects a bearer key outside its tenant", async () => {
  const app = await fixture({ apiAuthorized: false });
  const body = JSON.stringify({
    community_id: 8,
    platform: "discord",
    event_type: "member.joined",
  });
  const response = await app.handler(
    new Request("http://localhost/api/events", {
      method: "POST",
      headers: {
        authorization: "Bearer wrong",
        "content-length": String(body.length),
      },
      body,
    }),
  );
  assertEquals(response.status, 401);
  assertEquals(app.observations.length, 0);
});

Deno.test("API clients require tenant scope and available rate budget", async () => {
  const allowed = new FakeConnection([
    [{
      id: 3,
      scopes_json: '["events.write"]',
      rate_limit_per_minute: 2,
    }],
    [{ request_count: 1 }],
  ]);
  assertEquals(
    await new PostgresMachineIngestionRepository(allowed).authorizeApiClient(
      "secret",
      7,
    ),
    true,
  );
  assertEquals(allowed.calls[0].parameters[1], 7);
  assertEquals(allowed.calls[0].sql.includes("status='active'"), true);

  const denied = new FakeConnection([[{
    id: 3,
    scopes_json: '["events.read"]',
    rate_limit_per_minute: 2,
  }]]);
  assertEquals(
    await new PostgresMachineIngestionRepository(denied).authorizeApiClient(
      "secret",
      7,
    ),
    false,
  );
  assertEquals(denied.calls.length, 1);

  const limited = new FakeConnection([
    [{ id: 3, scopes_json: '["*"]', rate_limit_per_minute: 1 }],
    [{ request_count: 2 }],
  ]);
  assertEquals(
    await new PostgresMachineIngestionRepository(limited).authorizeApiClient(
      "secret",
      7,
    ),
    false,
  );
});

Deno.test("machine ingestion rejects cross-origin mutations before authorization", async () => {
  const app = await fixture();
  const body = JSON.stringify({
    community_id: 7,
    platform: "discord",
    event_type: "member.joined",
  });
  const response = await app.handler(
    new Request("http://localhost/api/events", {
      method: "POST",
      headers: {
        origin: "https://other.example",
        authorization: "Bearer client-secret",
        "content-length": String(body.length),
      },
      body,
    }),
  );
  assertEquals(response.status, 403);
  assertEquals(await response.json(), { error: "origin_mismatch" });
  assertEquals(app.calls.length, 0);
});

Deno.test("external observations use the authenticated tenant and source identity", async () => {
  const app = await fixture();
  const body = JSON.stringify({
    community_id: 7,
    source_key: "Threat Feed",
    external_event_id: "item-1",
    text: "indicator",
    trust_weight: 0.8,
  });
  const response = await app.handler(
    new Request("http://localhost/api/external/observations", {
      method: "POST",
      headers: {
        authorization: "Bearer client-secret",
        "content-length": String(body.length),
      },
      body,
    }),
  );
  assertEquals(response.status, 201);
  assertEquals(app.calls[1], ["external", "threat feed"]);
  assertEquals(app.observations[0].platform, "external:threat feed");
  assertEquals(app.observations[0].communityId, 7);
});

Deno.test("EventSub verifies raw bytes and resolves an ingestion-capable installation", async () => {
  const app = await fixture();
  const messageId = "eventsub-1";
  const timestamp = new Date().toISOString();
  const body = JSON.stringify({
    subscription: {
      id: "sub-1",
      type: "channel.follow",
      status: "enabled",
      condition: { broadcaster_user_id: "broadcaster-1" },
    },
    event: {
      broadcaster_user_id: "broadcaster-1",
      user_id: "viewer-1",
      user_name: "Viewer",
    },
  });
  const signature = await sign(messageId + timestamp + body);
  const response = await app.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers: {
        "content-length": String(body.length),
        "Twitch-Eventsub-Message-Id": messageId,
        "Twitch-Eventsub-Message-Timestamp": timestamp,
        "Twitch-Eventsub-Message-Signature": signature,
        "Twitch-Eventsub-Message-Type": "notification",
      },
      body,
    }),
  );
  assertEquals(response.status, 202);
  assertEquals(app.observations[0].installationId, 4);
  assertEquals(app.observations[0].eventType, "channel.followed");
  assertEquals(app.calls.map((call) => call[0]), [
    "resolve",
    "subscription",
    "mark",
  ]);
});

Deno.test("EventSub rejects an invalid signature before tenant resolution", async () => {
  const app = await fixture();
  const body = "{}";
  const response = await app.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers: {
        "content-length": "2",
        "Twitch-Eventsub-Message-Id": "bad",
        "Twitch-Eventsub-Message-Timestamp": new Date().toISOString(),
        "Twitch-Eventsub-Message-Signature": "sha256=bad",
      },
      body,
    }),
  );
  assertEquals(response.status, 403);
  assertEquals(app.calls.length, 0);
});

Deno.test("EventSub rejects replayed message IDs before tenant resolution", async () => {
  const app = await fixture();
  const messageId = "eventsub-replay-1";
  const timestamp = new Date().toISOString();
  const body = JSON.stringify({
    subscription: {
      id: "sub-1",
      type: "channel.follow",
      status: "enabled",
      condition: { broadcaster_user_id: "broadcaster-1" },
    },
    event: {
      broadcaster_user_id: "broadcaster-1",
      user_id: "viewer-1",
    },
  });
  const headers = await eventsubHeaders(
    messageId,
    timestamp,
    body,
    "notification",
  );
  const first = await app.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers,
      body,
    }),
  );
  const second = await app.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers,
      body,
    }),
  );
  assertEquals(first.status, 202);
  assertEquals(second.status, 204);
  assertEquals(app.observations.length, 1);
  assertEquals(app.calls.map((call) => call[0]), [
    "resolve",
    "subscription",
    "mark",
  ]);
});

Deno.test("machine ingestion enforces bounded request bodies", async () => {
  const app = await fixture();
  const eventsub = await app.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers: { "content-length": "1048577" },
      body: "{}",
    }),
  );
  assertEquals(eventsub.status, 400);
  assertEquals(await eventsub.json(), { error: "invalid_body_length" });
  const event = await app.handler(
    new Request("http://localhost/api/events", {
      method: "POST",
      headers: { "content-length": "1048577" },
      body: "{}",
    }),
  );
  assertEquals(event.status, 413);
  assertEquals(await event.json(), { error: "body_too_large" });
});

Deno.test("EventSub challenge and revocation preserve protocol responses", async () => {
  const challengeApp = await fixture();
  const timestamp = new Date().toISOString();
  const challengeBody = JSON.stringify({
    challenge: "challenge-token",
    subscription: {
      id: "sub-1",
      type: "channel.follow",
      condition: { broadcaster_user_id: "broadcaster-1" },
    },
    event: {},
  });
  const challenge = await challengeApp.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers: await eventsubHeaders(
        "challenge-1",
        timestamp,
        challengeBody,
        "webhook_callback_verification",
      ),
      body: challengeBody,
    }),
  );
  assertEquals(challenge.status, 200);
  assertEquals(
    challenge.headers.get("content-type"),
    "text/plain; charset=utf-8",
  );
  assertEquals(await challenge.text(), "challenge-token");

  const revocationApp = await fixture();
  const revocationBody = JSON.stringify({
    subscription: {
      id: "sub-2",
      type: "channel.follow",
      status: "authorization_revoked",
      condition: { broadcaster_user_id: "broadcaster-1" },
    },
    event: {},
  });
  const revocation = await revocationApp.handler(
    new Request("http://localhost/webhooks/twitch/eventsub", {
      method: "POST",
      headers: await eventsubHeaders(
        "revoke-1",
        timestamp,
        revocationBody,
        "revocation",
      ),
      body: revocationBody,
    }),
  );
  assertEquals(revocation.status, 204);
  assertEquals(revocationApp.calls.at(-1), [
    "mark",
    "sub-2",
    "authorization_revoked",
  ]);
});

async function sign(message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)),
  );
  return `sha256=${
    [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("")
  }`;
}

async function eventsubHeaders(
  messageId: string,
  timestamp: string,
  body: string,
  messageType: string,
): Promise<HeadersInit> {
  return {
    "content-length": String(body.length),
    "Twitch-Eventsub-Message-Id": messageId,
    "Twitch-Eventsub-Message-Timestamp": timestamp,
    "Twitch-Eventsub-Message-Signature": await sign(
      messageId + timestamp + body,
    ),
    "Twitch-Eventsub-Message-Type": messageType,
  };
}
