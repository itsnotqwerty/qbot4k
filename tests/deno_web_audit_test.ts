import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import type { AuditService } from "../src/web/web_audit.ts";
import { WebAuditController } from "../src/web/web_audit.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { type OperatorAuthStore, WebAuthController } from "../src/web/web_auth.ts";

const secret = "audit-test-secret";

async function fixture(role: string) {
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
  const audit: AuditService = {
    list: () =>
      Promise.resolve([{
        id: 1,
        created_at: "2026-01-01",
        actor_type: "operator",
        actor_id: 42,
        action_type: "moderation.rule_saved",
        entity_type: "rule",
        entity_id: 3,
        payload_json: "{}",
      }]),
  };
  const controller = new WebAuditController(auth, audit);
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
      controller,
    ).handler(),
    headers: { cookie: `qbot4k_session=${cookie}` },
  };
}

Deno.test("audit HTML and JSON are admin-only and preserve envelopes", async () => {
  const admin = await fixture("admin");
  const page = await admin.handler(
    new Request("http://localhost/audit?action_type=moderation.rule_saved", {
      headers: admin.headers,
    }),
  );
  assertEquals(page.status, 200);
  assertEquals((await page.text()).includes("Audit trail"), true);
  const api = await admin.handler(
    new Request("http://localhost/api/audit", { headers: admin.headers }),
  );
  assertEquals(
    (await api.json()).items[0].action_type,
    "moderation.rule_saved",
  );
  const viewer = await fixture("viewer");
  assertEquals(
    (await viewer.handler(
      new Request("http://localhost/audit", { headers: viewer.headers }),
    )).status,
    403,
  );
});
