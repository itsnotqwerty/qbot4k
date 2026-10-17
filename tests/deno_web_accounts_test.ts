import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { OrganizationSignupConflict } from "../src/data/billing_repository.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import { WebAccountsController } from "../src/web/web_accounts.ts";

async function fixture(
  enabled = true,
  accountStatus = "active",
  communityId: number | null = null,
) {
  const calls: unknown[][] = [];
  const settings = {
    dashboardSessionSecret: "account-test",
    discordOauthClientId: "client",
    discordOauthClientSecret: "secret",
    discordOauthRedirectUri: "https://qbot.example/callback",
    operatorGuildIds: [],
  };
  const auth = new WebAuthController(settings, {
    authenticate: () => {
      throw new Error("unused");
    },
  }, {
    completeLogin: () => {
      throw new Error("unused");
    },
    switchCommunity: () => Promise.resolve(null),
    auditLogout: () => Promise.resolve(),
    resolveSession: () =>
      Promise.resolve({
        status: accountStatus,
        sessionVersion: 3,
        memberships: communityId === null ? [] : [{
          id: communityId,
          name: "Existing",
          slug: "existing",
          role: "owner",
        }],
      }),
  }, () => new Date("2026-10-17T12:00:00.000Z"));
  const cookie = await createSessionCookie(settings.dashboardSessionSecret, {
    userId: "42",
    username: "Customer <script>",
    role: communityId === null ? "viewer" : "owner",
    communityId,
    sessionVersion: 3,
    expiresAt: "2026-10-18T00:00:00.000Z",
  });
  const service = {
    createOrganization: (...args: [number, string, string, number]) => {
      calls.push(args);
      return Promise.resolve({ organizationId: 7, communityId: 9 });
    },
  };
  const controller = new WebAccountsController(auth, service, enabled);
  const request = (
    body = "name=Example&slug=example",
    origin = "https://qbot.example",
  ) =>
    new Request("https://qbot.example/account/organizations", {
      method: "POST",
      body,
      headers: {
        cookie: `qbot4k_session=${cookie}`,
        origin,
        "content-type": "application/x-www-form-urlencoded",
      },
    });
  return { controller, service, calls, request, cookie, auth };
}

Deno.test("account page requires a current account and escapes identity text", async () => {
  const { controller, cookie } = await fixture();
  assertEquals(
    (await controller.page(new Request("https://qbot.example/account"))).headers
      .get("location"),
    "/login",
  );
  const response = await controller.page(
    new Request("https://qbot.example/account", {
      headers: { cookie: `qbot4k_session=${cookie}` },
    }),
  );
  assertEquals(response.status, 200);
  assertEquals(response.headers.get("cache-control"), "no-store");
  const html = await response.text();
  assertEquals(html.includes('action="/account/organizations"'), true);
  assertEquals(html.includes("Customer &lt;script"), true);
  assertEquals(html.includes("Customer <script>"), false);
});

Deno.test("application routes expose account page and signup controller", async () => {
  const { controller, auth, request, cookie } = await fixture();
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
    undefined,
    undefined,
    controller,
  ).handler();
  const page = await handler(
    new Request("https://qbot.example/account", {
      headers: { cookie: `qbot4k_session=${cookie}` },
    }),
  );
  assertEquals(page.status, 200);
  assertEquals((await handler(request())).status, 303);
});

Deno.test("signup uses signed identity and session version, never submitted IDs", async () => {
  const { controller, calls, request } = await fixture();
  const response = await controller.create(
    request(
      "name=Example&slug=example&operator_id=999&session_version=100&organization_id=1&role=admin",
    ),
  );
  assertEquals(response.status, 303);
  assertEquals(response.headers.get("location"), "/login");
  assertEquals(calls, [[42, "Example", "example", 3]]);
});

Deno.test("signup rejects closed registration, existing membership and disabled accounts", async () => {
  for (
    const [enabled, accountStatus, communityId, expected] of [
      [false, "active", null, 403],
      [true, "disabled", null, 303],
      [true, "active", 7, 409],
    ] as const
  ) {
    const { controller, calls, request } = await fixture(
      enabled,
      accountStatus,
      communityId,
    );
    assertEquals((await controller.create(request())).status, expected);
    assertEquals(calls, []);
  }
});

Deno.test("signup validates origin, values and request body size before writes", async () => {
  const { controller, calls, request } = await fixture();
  assertEquals(
    (await controller.create(
      request("name=Example&slug=example", "https://attacker.example"),
    )).status,
    403,
  );
  for (
    const body of [
      "name=&slug=example",
      "name=Example&slug=../other",
      "name=Example&slug=EXAMPLE",
    ]
  ) {
    assertEquals((await controller.create(request(body))).status, 400);
  }
  assertEquals(
    (await controller.create(request("x".repeat(4_097)))).status,
    413,
  );
  assertEquals(calls, []);
});

Deno.test("signup conflicts are recoverable and provider errors do not leak details", async () => {
  const { controller, service, request } = await fixture();
  service.createOrganization = () =>
    Promise.reject(new OrganizationSignupConflict("Sign in again."));
  assertEquals((await controller.create(request())).status, 409);
  service.createOrganization = () =>
    Promise.reject({ code: "23505", detail: "private row" });
  const duplicate = await controller.create(request());
  assertEquals(duplicate.status, 409);
  assertEquals((await duplicate.text()).includes("private row"), false);
  service.createOrganization = () =>
    Promise.reject(new Error("secret connection string"));
  const failed = await controller.create(request());
  assertEquals(failed.status, 503);
  assertEquals(
    (await failed.text()).includes("secret connection string"),
    false,
  );
});
