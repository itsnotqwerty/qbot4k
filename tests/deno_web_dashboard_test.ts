import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { type OperatorAuthStore, WebAuthController } from "../src/web/web_auth.ts";
import {
  type DashboardOperations,
  WebDashboardController,
} from "../src/web/web_dashboard.ts";
import type {
  DashboardItem,
  DashboardQueryService,
} from "../src/web/web_queries.ts";

const secret = "dashboard-test-secret";
const memberships = [{ id: 7, name: "Alpha", slug: "alpha", role: "viewer" }];

class FakeQueries implements DashboardQueryService {
  readonly calls: string[] = [];
  overview(communityId: number): Promise<DashboardItem> {
    this.calls.push(`overview:${communityId}`);
    return Promise.resolve({ messages_total: 12, open_reviews: 2 });
  }
  users(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    this.calls.push(`users:${communityId}:${query.get("q") ?? ""}`);
    return Promise.resolve([{
      user_id: 4,
      primary_display_name: "Ada",
      message_count: 9,
    }]);
  }
  search(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    this.calls.push(`search:${communityId}:${query.get("q") ?? ""}`);
    return Promise.resolve([{
      id: 11,
      platform: "discord",
      text_raw: "matched",
    }]);
  }
  signals(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    this.calls.push(
      `signals:${communityId}:${query.getAll("signal").join(",")}`,
    );
    return Promise.resolve([{
      user_id: 4,
      signal_key: "risk.composite",
      value: 72,
    }]);
  }
  analytics(communityId: number): Promise<DashboardItem> {
    this.calls.push(`analytics:${communityId}`);
    return Promise.resolve({
      growth: [{ metric_date: "2026-01-01", joins: 3 }],
      evaluation: [],
    });
  }
  saveQuery(): Promise<number> {
    return Promise.resolve(31);
  }
  observationPivots(): Promise<DashboardItem | null> {
    return Promise.resolve({
      observation_id: 11,
      related_observation_count: 2,
    });
  }
  userDetail(): Promise<DashboardItem | null> {
    return Promise.resolve({
      user: { user_id: 4 },
      signals: [],
      lifecycle: [],
    });
  }
  linkUser(): Promise<"linked"> {
    return Promise.resolve("linked");
  }
  linkUsersByName(): Promise<import("../src/web/web_queries.ts").UserLinkResult> {
    return Promise.resolve({
      userId: 4,
      linkedUsernames: 1,
      linkedAccounts: 2,
      missingUsernames: ["Missing"],
    });
  }
  addUserNote(): Promise<boolean> {
    return Promise.resolve(true);
  }
  unlinkUser(): Promise<boolean> {
    return Promise.resolve(true);
  }
  reviewIdentitySuggestion(): Promise<boolean> {
    return Promise.resolve(true);
  }
  slo(): Promise<readonly import("../src/domain/slo.ts").TenantSloSample[]> {
    return Promise.resolve([{
      metricName: "open_dead_letters",
      value: 0,
      targetValue: 0,
      status: "met",
      evidenceCount: 0,
    }]);
  }
}

async function fixture(role = "viewer", operations?: DashboardOperations) {
  const store: OperatorAuthStore = {
    completeLogin: () => Promise.reject(new Error("not used")),
    switchCommunity: () => Promise.resolve(null),
    auditLogout: () => Promise.resolve(),
    resolveSession: () =>
      Promise.resolve({
        status: "active",
        sessionVersion: 2,
        memberships: [{ ...memberships[0], role }],
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
  const queries = new FakeQueries();
  const dashboard = new WebDashboardController(auth, queries, operations);
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  return {
    handler: createApp(undefined, auth, undefined, dashboard).handler(),
    headers: { cookie: `qbot4k_session=${cookie}` },
    queries,
  };
}

Deno.test("legacy dashboard operations preserve admin redirects and confirmation", async () => {
  const calls: string[] = [];
  const operations: DashboardOperations = {
    goLive: (communityId, operatorId) => {
      calls.push(`live:${communityId}:${operatorId}`);
      return Promise.resolve(2);
    },
    restart: (operatorId) => {
      calls.push(`restart:${operatorId}`);
      return Promise.resolve("qbot4k.service");
    },
    resetDatabase: (operatorId) => {
      calls.push(`reset:${operatorId}`);
      return Promise.resolve({ rowsDeleted: 9 });
    },
  };
  const { handler, headers } = await fixture("admin", operations);
  const post = (path: string, body?: URLSearchParams) =>
    handler(
      new Request(`http://localhost${path}`, {
        method: "POST",
        headers: { ...headers, origin: "http://localhost" },
        body,
      }),
    );
  assertEquals(
    (await post("/dashboard/go-live")).headers.get("location"),
    "/dashboard?status=Go%20Live%20sent%202%20pings",
  );
  assertEquals(
    (await post("/dashboard/restart")).headers.get("location"),
    "/dashboard?status=Restart%20requested%20for%20qbot4k.service",
  );
  assertEquals(
    (await post(
      "/dashboard/reset-database",
      new URLSearchParams({ confirmation: "NO" }),
    )).headers.get("location"),
    "/dashboard?status=Database%20reset%20cancelled",
  );
  assertEquals(
    (await post(
      "/dashboard/reset-database",
      new URLSearchParams({ confirmation: "RESET" }),
    )).headers.get("location"),
    "/dashboard?status=Database%20reset%20complete%3B%20deleted%209%20rows",
  );
  assertEquals(calls, ["live:7:42", "restart:42", "reset:42"]);
});

Deno.test("legacy dashboard operations reject denied roles and cross-origin requests", async () => {
  const operations: DashboardOperations = {
    goLive: () => Promise.resolve(0),
    restart: () => Promise.resolve("qbot4k.service"),
    resetDatabase: () => Promise.resolve({ rowsDeleted: 0 }),
  };
  const viewer = await fixture("viewer", operations);
  const denied = await viewer.handler(
    new Request("http://localhost/dashboard/go-live", {
      method: "POST",
      headers: { ...viewer.headers, origin: "http://localhost" },
    }),
  );
  assertEquals(denied.status, 403);

  const admin = await fixture("admin", operations);
  const crossOrigin = await admin.handler(
    new Request("http://localhost/dashboard/restart", {
      method: "POST",
      headers: { ...admin.headers, origin: "https://other.example" },
    }),
  );
  assertEquals(crossOrigin.status, 403);
  assertEquals(await crossOrigin.json(), { error: "origin_mismatch" });
});

Deno.test("DF3 dashboard HTML routes share tenant-scoped query services", async () => {
  const { handler, headers, queries } = await fixture();
  for (
    const path of [
      "/dashboard",
      "/users?q=ada",
      "/search?q=matched",
      "/signals?signal=risk.composite",
      "/analytics",
    ]
  ) {
    const response = await handler(
      new Request(`http://localhost${path}`, { headers }),
    );
    assertEquals(response.status, 200);
    assertEquals(
      response.headers.get("content-type"),
      "text/html; charset=utf-8",
    );
    assertEquals(
      (await response.text()).includes("Dashboard navigation"),
      true,
    );
  }
  assertEquals(queries.calls, [
    "overview:7",
    "users:7:ada",
    "search:7:matched",
    "signals:7:risk.composite",
    "analytics:7",
  ]);
});

Deno.test("DF3 dashboard APIs preserve response envelopes", async () => {
  const { handler, headers } = await fixture();
  const overview = await (await handler(
    new Request("http://localhost/api/overview", { headers }),
  )).json();
  const users = await (await handler(
    new Request("http://localhost/api/users", { headers }),
  )).json();
  const search = await (await handler(
    new Request("http://localhost/api/search", { headers }),
  )).json();
  const signals = await (await handler(
    new Request("http://localhost/api/signals?signal=risk.composite", {
      headers,
    }),
  )).json();
  const analytics = await (await handler(
    new Request("http://localhost/api/analytics", { headers }),
  )).json();
  assertEquals(overview.messages_total, 12);
  assertEquals(users.items[0].primary_display_name, "Ada");
  assertEquals(search.items[0].text_raw, "matched");
  assertEquals(signals.filters.signals, ["risk.composite"]);
  assertEquals(signals.items[0].signal_key, "risk.composite");
  assertEquals(analytics.growth[0].joins, 3);
});

Deno.test("DF3 dashboard redirects invalid sessions before querying", async () => {
  const { handler, queries } = await fixture();
  const response = await handler(new Request("http://localhost/api/users"));
  assertEquals(response.status, 302);
  assertEquals(response.headers.get("location"), "/login");
  assertEquals(queries.calls, []);
});

Deno.test("DF3 dashboard denies roles without the surface capability", async () => {
  const { handler, headers, queries } = await fixture("restricted");
  const response = await handler(
    new Request("http://localhost/api/analytics", { headers }),
  );
  assertEquals(response.status, 403);
  assertEquals(queries.calls, []);
});

Deno.test("DF3 exports preserve content types, names, and tenant scope", async () => {
  const analyst = await fixture("analyst");
  const search = await analyst.handler(
    new Request(
      "http://localhost/search/export.csv?q=matched",
      { headers: analyst.headers },
    ),
  );
  assertEquals(search.status, 200);
  assertEquals(search.headers.get("content-type"), "text/csv; charset=utf-8");
  assertEquals(
    search.headers.get("content-disposition"),
    'attachment; filename="qbot4k-observations.csv"',
  );
  assertEquals((await search.text()).includes("11,matched"), false);
  assertEquals(analyst.queries.calls, ["search:7:matched"]);

  const admin = await fixture("admin");
  const analytics = await admin.handler(
    new Request(
      "http://localhost/analytics/export.json",
      { headers: admin.headers },
    ),
  );
  assertEquals(analytics.status, 200);
  assertEquals(analytics.headers.get("content-type"), "application/json");
  assertEquals(
    analytics.headers.get("content-disposition"),
    'attachment; filename="qbot4k-community-7-analytics.json"',
  );
  const payload = await analytics.json();
  assertEquals(payload.community_id, 7);
  assertEquals(typeof payload.exported_at, "string");
});

Deno.test("DF3 exports preserve capability denials", async () => {
  const viewer = await fixture("viewer");
  for (const path of ["/search/export.csv", "/analytics/export.json"]) {
    const response = await viewer.handler(
      new Request(`http://localhost${path}`, {
        headers: viewer.headers,
      }),
    );
    assertEquals(response.status, 403);
    assertEquals(await response.text(), "Forbidden");
  }
  assertEquals(viewer.queries.calls, []);
});

Deno.test("DF3 legacy dashboard APIs preserve response envelopes", async () => {
  const admin = await fixture("admin");
  const detail = await (await admin.handler(
    new Request(
      "http://localhost/api/users/4",
      { headers: admin.headers },
    ),
  )).json();
  assertEquals(detail.user.user_id, 4);
  const pivots = await (await admin.handler(
    new Request(
      "http://localhost/api/observations/11/pivots",
      { headers: admin.headers },
    ),
  )).json();
  assertEquals(pivots.related_observation_count, 2);
  const saved = await (await admin.handler(
    new Request(
      "http://localhost/api/search/saved",
      {
        method: "POST",
        headers: { ...admin.headers, "content-type": "application/json" },
        body: JSON.stringify({ name: "Recent", query: "launch", filters: {} }),
      },
    ),
  )).json();
  assertEquals(saved, { id: 31, status: "saved" });
  const linked = await (await admin.handler(
    new Request(
      "http://localhost/api/users/link",
      {
        method: "POST",
        headers: { ...admin.headers, "content-type": "application/json" },
        body: JSON.stringify({ user_id: 4, discord_user_id: "42" }),
      },
    ),
  )).json();
  assertEquals(linked.status, "linked");
  const noted = await (await admin.handler(
    new Request(
      "http://localhost/api/users/4/notes",
      {
        method: "POST",
        headers: { ...admin.headers, "content-type": "application/json" },
        body: JSON.stringify({ body: "Reviewed" }),
      },
    ),
  )).json();
  assertEquals(noted.status, "noted");
});

Deno.test("DF3 user detail, lifecycle export, and unlink preserve contracts", async () => {
  const admin = await fixture("admin");
  const page = await admin.handler(
    new Request("http://localhost/users/4", {
      headers: admin.headers,
    }),
  );
  assertEquals(page.status, 200);
  assertEquals(page.headers.get("content-type"), "text/html; charset=utf-8");
  const lifecycle = await admin.handler(
    new Request(
      "http://localhost/users/4/lifecycle.csv",
      { headers: admin.headers },
    ),
  );
  assertEquals(lifecycle.status, 200);
  assertEquals(
    lifecycle.headers.get("content-type"),
    "text/csv; charset=utf-8",
  );
  const unlink = await admin.handler(
    new Request("http://localhost/users/unlink", {
      method: "POST",
      headers: admin.headers,
      body: new URLSearchParams({
        user_id: "4",
        platform_account_id: "9",
        confirmation: "UNLINK",
      }),
    }),
  );
  assertEquals(unlink.status, 302);
  assertEquals(
    unlink.headers.get("location"),
    "/users/4?account_status=Platform%20account%20unlinked",
  );
});

Deno.test("DF3 legacy user linking form preserves normalization and redirect", async () => {
  const admin = await fixture("admin");
  const response = await admin.handler(
    new Request("http://localhost/users/link", {
      method: "POST",
      headers: admin.headers,
      body: new URLSearchParams({
        selected_user_id: "4",
        usernames: "Ada (unlinked), Missing",
        platform: "invalid",
        q: "ada",
        sort: "invalid",
        dir: "invalid",
      }),
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/users?q=ada&sort=score&dir=desc&link_user_id=4&link_status=Linked+1+username%28s%29%2C+2+account%28s%29.+Missing%3A+Missing",
  );
});

Deno.test("DF3 authenticated health compatibility endpoint", async () => {
  const admin = await fixture("admin");
  const response = await admin.handler(
    new Request("http://localhost/api/health", {
      headers: admin.headers,
    }),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "ready" });
  const denied = await admin.handler(
    new Request("http://localhost/api/health", {
      headers: { accept: "application/json" },
    }),
  );
  assertEquals(denied.status, 302);
});

Deno.test("DF3 identity suggestion review preserves API envelope", async () => {
  const admin = await fixture("admin");
  const response = await admin.handler(
    new Request(
      "http://localhost/api/identity-suggestions/12",
      {
        method: "POST",
        headers: { ...admin.headers, "content-type": "application/json" },
        body: JSON.stringify({ decision: "approved" }),
      },
    ),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "reviewed" });
});

Deno.test("DF3 tenant SLO endpoint preserves API envelope", async () => {
  const admin = await fixture("admin");
  const response = await admin.handler(
    new Request("http://localhost/api/slo", {
      headers: admin.headers,
    }),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), {
    community_id: 7,
    items: [{
      metric_name: "open_dead_letters",
      value: 0,
      target_value: 0,
      status: "met",
      evidence_count: 0,
    }],
  });
});
