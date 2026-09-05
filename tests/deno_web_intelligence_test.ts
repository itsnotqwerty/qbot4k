import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import {
  type OperatorAuthStore,
  WebAuthController,
} from "../src/web/web_auth.ts";
import {
  type IntelligenceCaseDetail,
  type IntelligenceService,
  type IntelligenceSnapshot,
  WebIntelligenceController,
} from "../src/web/web_intelligence.ts";

const secret = "intelligence-test-secret";
const detail: IntelligenceCaseDetail = {
  case: { id: 5, title: "Coordinated activity", status: "open" },
  entities: [{ user_id: 4 }],
  evidence: [{ id: 8, alert_id: 3 }],
  activity: [{ id: 9, activity_type: "note.added" }],
};
const snapshot: IntelligenceSnapshot = {
  summary: { open_alerts: 1, open_cases: 1, relationships: 0, reports: 1 },
  sort: {
    alerts: { by: "created", dir: "desc" },
    cases: { by: "updated", dir: "desc" },
    relationships: { by: "strength", dir: "desc" },
  },
  alerts: [{ id: 3, severity: "high", title: "Coordinated activity" }],
  cases: [detail.case],
  relationships: [],
  reports: [{ id: 11, title: "Daily Intelligence Summary" }],
};

class FakeIntelligence implements IntelligenceService {
  readonly communities: number[] = [];
  readonly actions: string[] = [];
  snapshot(communityId: number): Promise<IntelligenceSnapshot> {
    this.communities.push(communityId);
    return Promise.resolve(snapshot);
  }
  caseDetail(communityId: number): Promise<IntelligenceCaseDetail> {
    this.communities.push(communityId);
    return Promise.resolve(detail);
  }
  caseAction(
    communityId: number,
    _operatorId: number,
    _caseId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    this.communities.push(communityId);
    this.actions.push(String(input.action));
    return Promise.resolve();
  }
  caseFromAlert(communityId: number): Promise<number> {
    this.communities.push(communityId);
    this.actions.push("case_from_alert");
    return Promise.resolve(5);
  }
  disposeAlert(communityId: number): Promise<void> {
    this.communities.push(communityId);
    this.actions.push("dispose");
    return Promise.resolve();
  }
  updateAlert(communityId: number): Promise<void> {
    this.communities.push(communityId);
    this.actions.push("workflow");
    return Promise.resolve();
  }
  generateReport(communityId: number): Promise<number> {
    this.communities.push(communityId);
    this.actions.push("report");
    return Promise.resolve(11);
  }
  report(communityId: number): Promise<Readonly<Record<string, unknown>>> {
    this.communities.push(communityId);
    return Promise.resolve({ id: 11, content: { alerts: [] }, evidence: [] });
  }
}

async function fixture(role = "analyst") {
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
  const service = new FakeIntelligence();
  const controller = new WebIntelligenceController(auth, service);
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  return {
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
      undefined,
      undefined,
      controller,
    ).handler(),
    headers: { cookie: `qbot4k_session=${cookie}`, origin: "http://localhost" },
    service,
  };
}

Deno.test("DF3 intelligence HTML and API routes bind the active tenant", async () => {
  const { handler, headers, service } = await fixture();
  const page = await handler(
    new Request("http://localhost/intelligence", { headers }),
  );
  assertEquals(page.status, 200);
  assertEquals(page.headers.get("content-type"), "text/html; charset=utf-8");
  const html = await page.text();
  assertEquals(html.includes("Intelligence"), true);
  assertEquals(html.includes("Alert queue"), true);
  assertEquals(html.includes('href="/styles.css"'), true);
  assertEquals(html.includes('href="/commands"'), true);
  const api = await handler(
    new Request("http://localhost/api/intelligence", { headers }),
  );
  assertEquals(api.status, 200);
  assertEquals((await api.json()).summary.open_alerts, 1);
  const casePage = await handler(
    new Request("http://localhost/intelligence/cases/5", { headers }),
  );
  assertEquals(casePage.status, 200);
  const caseApi = await handler(
    new Request("http://localhost/api/intelligence/cases/5", { headers }),
  );
  assertEquals((await caseApi.json()).case.id, 5);
  assertEquals(service.communities, [7, 7, 7, 7]);
});

Deno.test("DF3 intelligence form mutations preserve redirects", async () => {
  const { handler, headers, service } = await fixture();
  const post = (path: string, body: URLSearchParams) =>
    handler(
      new Request(
        `http://localhost${path}`,
        { method: "POST", headers, body },
      ),
    );
  assertEquals(
    (await post(
      "/intelligence/cases/5/action",
      new URLSearchParams({
        action: "add_note",
        body: "Reviewed",
      }),
    )).headers.get("location"),
    "/intelligence/cases/5",
  );
  assertEquals(
    (await post("/intelligence/alerts/3/case", new URLSearchParams())).headers
      .get("location"),
    "/intelligence/cases/5",
  );
  assertEquals(
    (await post(
      "/intelligence/alerts/3/disposition",
      new URLSearchParams({
        disposition: "confirmed",
      }),
    )).headers.get("location"),
    "/intelligence?status=Alert+resolved",
  );
  assertEquals(
    (await post(
      "/intelligence/alerts/3/workflow",
      new URLSearchParams({
        status: "acknowledged",
      }),
    )).headers.get("location"),
    "/intelligence?status=Alert+updated",
  );
  assertEquals(
    (await post(
      "/intelligence/reports/generate",
      new URLSearchParams({
        report_type: "daily_summary",
      }),
    )).headers.get("location"),
    "/api/intelligence/reports/11",
  );
  assertEquals(service.actions, [
    "add_note",
    "case_from_alert",
    "dispose",
    "workflow",
    "report",
  ]);
});

Deno.test("DF3 intelligence API mutations and exports preserve envelopes", async () => {
  const { handler, headers } = await fixture();
  const apiHeaders = { ...headers, "content-type": "application/json" };
  const updated = await handler(
    new Request("http://localhost/api/intelligence/cases/5", {
      method: "POST",
      headers: apiHeaders,
      body: JSON.stringify({ action: "update", status: "active" }),
    }),
  );
  assertEquals((await updated.json()).case.status, "open");
  const workflow = await handler(
    new Request("http://localhost/api/intelligence/alerts/3", {
      method: "POST",
      headers: apiHeaders,
      body: JSON.stringify({ status: "acknowledged" }),
    }),
  );
  assertEquals(await workflow.json(), { status: "updated", alert_id: 3 });
  const report = await handler(
    new Request("http://localhost/api/intelligence/reports/11", { headers }),
  );
  assertEquals((await report.json()).id, 11);
  const exported = await handler(
    new Request("http://localhost/api/intelligence/cases/5/export", {
      headers,
    }),
  );
  assertEquals(exported.headers.get("content-type"), "application/json");
  assertEquals(
    exported.headers.get("content-disposition"),
    'attachment; filename="qbot4k-case-5.json"',
  );
});

Deno.test("DF3 intelligence mutations reject cross-origin requests", async () => {
  const { handler, headers, service } = await fixture();
  const response = await handler(
    new Request("http://localhost/api/intelligence/alerts/3", {
      method: "POST",
      headers: {
        ...headers,
        origin: "https://attacker.example",
        "content-type": "application/json",
      },
      body: JSON.stringify({ status: "acknowledged" }),
    }),
  );
  assertEquals(response.status, 403);
  assertEquals(await response.json(), { error: "origin_mismatch" });
  assertEquals(service.actions, []);
});
