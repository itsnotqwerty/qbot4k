import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import type {
  LiveOpsControlGateway,
  LiveOpsService,
} from "../src/web/web_live_ops.ts";
import { WebLiveOpsController } from "../src/web/web_live_ops.ts";

const secret = "live-ops-test-secret";
async function fixture(role = "admin") {
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
  const snapshot = {
    watermark: "1:2:3",
    community: { id: 7, name: "Alpha" },
    active_incidents: [{
      id: 3,
      title: "Raid",
      severity: "high",
      status: "open",
    }],
    open_alerts: [],
    operations: { pending_actions: 1, open_reviews: 2, dead_letters: 0 },
  };
  const service: LiveOpsService = {
    snapshot: (communityId) =>
      Promise.resolve({
        ...snapshot,
        community: { id: communityId, name: "Alpha" },
      }),
    context: (communityId, observationId) => {
      calls.push(["context", communityId, observationId]);
      return observationId === 404
        ? Promise.reject(new TypeError("observation_not_found"))
        : Promise.resolve({
          community_id: communityId,
          finding_observation_id: observationId,
          items: [],
        });
    },
    moderate: (...args) => {
      calls.push(["moderate", ...args]);
      return Promise.resolve(11);
    },
    incident: (...args) => {
      calls.push(["incident", ...args]);
      return Promise.resolve({
        incident_id: args[2],
        assigned_operator_id: 42,
      });
    },
    handoff: (...args) => {
      calls.push(["handoff", ...args]);
      return Promise.resolve(8);
    },
    shifts: () => Promise.resolve([{ id: 1, operator_id: 42 }]),
    schedule: (...args) => {
      calls.push(["schedule", ...args]);
      return Promise.resolve();
    },
    playbook: () => Promise.resolve({ run_id: 2, steps: [] }),
    completePlaybook: () => Promise.resolve(),
    createDestination: () => Promise.resolve(5),
  };
  const controls: LiveOpsControlGateway = {
    shield: () => Promise.resolve({ status: "pending_provider_confirmation" }),
    chat: () => Promise.resolve({ status: "pending_provider_confirmation" }),
  };
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  const controller = new WebLiveOpsController(
    auth,
    service,
    controls,
    "channel",
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
      undefined,
      controller,
    ).handler(),
  };
}

Deno.test("live operations HTML JSON and SSE bind the active tenant", async () => {
  const app = await fixture();
  const page = await app.handler(
    new Request("http://localhost/live-ops", { headers: app.headers }),
  );
  assertEquals(page.status, 200);
  assertEquals((await page.text()).includes("Alpha"), true);
  const api = await app.handler(
    new Request("http://localhost/api/live-ops?community_id=99", {
      headers: app.headers,
    }),
  );
  assertEquals((await api.json()).community.id, 7);
  const stream = await app.handler(
    new Request("http://localhost/api/live-ops/stream", {
      headers: app.headers,
    }),
  );
  assertEquals(
    stream.headers.get("content-type"),
    "text/event-stream; charset=utf-8",
  );
  assertEquals((await stream.text()).includes("event: snapshot"), true);
});

Deno.test("observation context binds the finding to the active tenant", async () => {
  const app = await fixture();
  const response = await app.handler(
    new Request(
      "http://localhost/api/observations/19/context?community_id=99",
      { headers: app.headers },
    ),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), {
    community_id: 7,
    finding_observation_id: 19,
    items: [],
  });
  assertEquals(app.calls[0], ["context", 7, 19]);
  const missing = await app.handler(
    new Request("http://localhost/api/observations/404/context", {
      headers: app.headers,
    }),
  );
  assertEquals(missing.status, 404);
});

Deno.test("live moderation requires exact permanent-ban confirmation", async () => {
  const app = await fixture();
  const headers = {
    ...app.headers,
    origin: "http://localhost",
    "content-type": "application/json",
  };
  const denied = await app.handler(
    new Request("http://localhost/api/live-ops/moderate", {
      method: "POST",
      headers,
      body: JSON.stringify({ message_id: 4, action_type: "ban" }),
    }),
  );
  assertEquals(denied.status, 409);
  assertEquals(app.calls.length, 0);
  const accepted = await app.handler(
    new Request("http://localhost/api/live-ops/moderate", {
      method: "POST",
      headers,
      body: JSON.stringify({
        message_id: 4,
        action_type: "ban",
        confirmation: "PERMANENT BAN",
      }),
    }),
  );
  assertEquals(accepted.status, 202);
  assertEquals(await accepted.json(), {
    action_id: 11,
    status: "pending_provider_confirmation",
  });
  assertEquals(app.calls[0]?.slice(0, 3), ["moderate", 7, 42]);
});

Deno.test("live incident and shift routes preserve response envelopes", async () => {
  const app = await fixture();
  const headers = {
    ...app.headers,
    origin: "http://localhost",
    "content-type": "application/json",
  };
  const incident = await app.handler(
    new Request("http://localhost/api/live-ops/incidents/3/route-on-call", {
      method: "POST",
      headers,
      body: "{}",
    }),
  );
  assertEquals(await incident.json(), {
    incident_id: 3,
    assigned_operator_id: 42,
  });
  const schedule = await app.handler(
    new Request("http://localhost/api/live-ops/shifts", {
      method: "POST",
      headers,
      body: JSON.stringify({
        operator_id: 42,
        starts_at: "2026-09-03T10:00:00Z",
        ends_at: "2026-09-03T11:00:00Z",
      }),
    }),
  );
  assertEquals(schedule.status, 200);
  assertEquals((await schedule.json()).shifts[0].operator_id, 42);
  assertEquals(app.calls[1]?.slice(0, 3), ["schedule", 7, 42]);
});
