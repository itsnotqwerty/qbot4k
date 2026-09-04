import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import type { OperatorAuthStore } from "../src/web/web_auth.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import type { OnboardingService } from "../src/web/web_onboarding.ts";
import { WebOnboardingController } from "../src/web/web_onboarding.ts";

const secret = "onboarding-test-secret";

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
  const service: OnboardingService = {
    snapshot: () =>
      Promise.resolve({
        installations: [{ id: 3, display_name: "Discord" }],
        settings: {
          discord_installation_id: 3,
          verification_evidence_required: 1,
        },
        members: [{
          platform_user_id: "99",
          username: "New member",
          status: "newcomer",
          role_assignment_status: "pending",
        }],
        resources: [],
      }),
    configure: (...args) => {
      calls.push(["configure", ...args]);
      return Promise.resolve();
    },
    saveResource: (...args) => {
      calls.push(["resource", ...args]);
      return Promise.resolve(1);
    },
    deleteResource: (...args) => {
      calls.push(["delete", ...args]);
      return Promise.resolve();
    },
    verify: (...args) => {
      calls.push(["verify", ...args]);
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
  const controller = new WebOnboardingController(auth, service);
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
      controller,
    ).handler(),
  };
}

Deno.test("onboarding renders no-JavaScript management forms", async () => {
  const app = await fixture();
  const response = await app.handler(
    new Request("http://localhost/onboarding", { headers: app.headers }),
  );
  const html = await response.text();
  assertEquals(response.status, 200);
  assertEquals(html.includes('action="/onboarding"'), true);
  assertEquals(html.includes('action="/onboarding/verify"'), true);
  assertEquals(html.includes('name="verification_evidence"'), true);
});

Deno.test("onboarding verification is tenant-bound and admin-only", async () => {
  const admin = await fixture();
  const response = await admin.handler(
    new Request("http://localhost/onboarding/verify", {
      method: "POST",
      headers: {
        ...admin.headers,
        "content-type": "application/x-www-form-urlencoded",
        origin: "http://localhost",
      },
      body: "platform_user_id=99&verification_evidence=Reviewed",
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/onboarding?status=Member%20verified",
  );
  assertEquals(admin.calls[0], ["verify", 7, 42, "99", "Reviewed"]);
  const viewer = await fixture("viewer");
  assertEquals(
    (await viewer.handler(
      new Request("http://localhost/onboarding/verify", {
        method: "POST",
        headers: { ...viewer.headers, origin: "http://localhost" },
      }),
    )).status,
    403,
  );
  assertEquals(viewer.calls.length, 0);
});
