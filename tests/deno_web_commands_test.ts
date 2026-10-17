import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import {
  type OperatorAuthStore,
  WebAuthController,
} from "../src/web/web_auth.ts";
import {
  type CommandRegistry,
  WebCommandsController,
} from "../src/web/web_commands.ts";

const secret = "commands-test-secret";

class FakeCommands implements CommandRegistry {
  inputs: Readonly<Record<string, string | boolean>>[] = [];
  communities: number[] = [];
  list(communityId: number) {
    this.communities.push(communityId);
    return Promise.resolve({
      builtins: [{
        command_name: "help",
        title: "Help",
        description_template: "Hello",
        footer_template: null,
        enabled: true,
      }],
      simple: [],
    });
  }
  update(
    communityId: number,
    input: Readonly<Record<string, string | boolean>>,
  ) {
    this.communities.push(communityId);
    this.inputs.push(input);
    if (String(input.command_name).toLocaleLowerCase() === "alias") {
      throw new TypeError("alias is reserved");
    }
    return Promise.resolve(
      `Saved simple command ${String(input.command_name).toLocaleLowerCase()}`,
    );
  }
}

async function fixture(role: string, platformAdmin = false) {
  const store: OperatorAuthStore = {
    isPlatformAdmin: () => Promise.resolve(platformAdmin),
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
  const registry = new FakeCommands();
  const controller = new WebCommandsController(auth, registry);
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
      controller,
    ).handler(),
    headers: { cookie: `qbot4k_session=${cookie}` },
    registry,
  };
}

Deno.test("commands page is admin-only and renders no-JavaScript forms", async () => {
  const admin = await fixture("admin");
  const response = await admin.handler(
    new Request("http://localhost/commands", { headers: admin.headers }),
  );
  assertEquals(response.status, 200);
  const html = await response.text();
  assertEquals(html.includes("Command menu"), true);
  assertEquals(html.includes('action="/commands"'), true);
  assertEquals(html.includes('href="/styles.css"'), true);
  assertEquals(html.includes('href="/integrations"'), true);
  assertEquals(html.includes('value="builtin"'), false);
  assertEquals(admin.registry.communities, [7]);
  const viewer = await fixture("viewer");
  assertEquals(
    (await viewer.handler(
      new Request("http://localhost/commands", { headers: viewer.headers }),
    )).status,
    403,
  );
});

Deno.test("commands form saves normalized simple commands and preserves redirect", async () => {
  const { handler, headers, registry } = await fixture("admin");
  const response = await handler(
    new Request("http://localhost/commands", {
      method: "POST",
      headers: {
        ...headers,
        "content-type": "application/x-www-form-urlencoded",
        origin: "http://localhost",
      },
      body:
        "record_type=simple&command_name=Website&response_template=Visit+us&enabled=1&community_id=999",
    }),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/commands?status=Saved%20simple%20command%20website",
  );
  assertEquals(registry.inputs[0].enabled, true);
  assertEquals(registry.communities, [7]);
});

Deno.test("community owners cannot edit shared builtins but platform admins can", async () => {
  for (const platformAdmin of [false, true]) {
    const { handler, headers, registry } = await fixture(
      "owner",
      platformAdmin,
    );
    const response = await handler(
      new Request("http://localhost/commands", {
        method: "POST",
        headers: {
          ...headers,
          origin: "http://localhost",
          "content-type": "application/x-www-form-urlencoded",
        },
        body:
          "record_type=builtin&command_name=help&title=Help&description_template=Hello&enabled=1",
      }),
    );
    assertEquals(response.status, platformAdmin ? 302 : 403);
    assertEquals(registry.inputs.length, platformAdmin ? 1 : 0);
  }
});
