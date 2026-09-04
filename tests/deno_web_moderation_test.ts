import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createApp } from "../main.ts";
import type {
  BulkModerationInput,
  BulkModerationResult,
  ModerationService,
  ModerationSnapshot,
  ModerationWorkResult,
  ReviewResolution,
  UserModerationInput,
} from "../src/domain/moderation.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { type OperatorAuthStore, WebAuthController } from "../src/web/web_auth.ts";
import { WebModerationController } from "../src/web/web_moderation.ts";

const secret = "moderation-test-secret";
const snapshot: ModerationSnapshot = {
  reviews: [{
    review_id: 5,
    target_username: "Ada",
    content: "message",
    severity: "high",
    reason_code: "rule",
  }],
  actions: [{ action_id: 9, status: "pending", provider_status: null }],
  rules: [{ rule_id: 3, name: "Links" }],
  reports: [],
  appeals: [],
  ruleVersions: [],
  savedFilters: [],
};

class FakeModeration implements ModerationService {
  readonly communities: number[] = [];
  resolutions: ReviewResolution[] = [];
  bulkInputs: BulkModerationInput[] = [];
  userActions: UserModerationInput[] = [];
  snapshot(communityId: number): Promise<ModerationSnapshot> {
    this.communities.push(communityId);
    return Promise.resolve(snapshot);
  }
  resolveReview(input: ReviewResolution): Promise<number | null> {
    this.resolutions.push(input);
    return Promise.resolve(9);
  }
  bulk(input: BulkModerationInput): Promise<BulkModerationResult> {
    this.bulkInputs.push(input);
    return Promise.resolve({
      dry_run: input.dryRun,
      action_type: input.actionType,
      requested: input.targetPlatformAccountIds.length,
      results: [],
    });
  }
  recordUserAction(input: UserModerationInput): Promise<boolean> {
    this.userActions.push(input);
    return Promise.resolve(true);
  }
  assign(): Promise<void> {
    return Promise.resolve();
  }
  resolveMember(): Promise<void> {
    return Promise.resolve();
  }
  createRuleDraft(): Promise<number> {
    return Promise.resolve(1);
  }
  saveRule(): Promise<number> {
    return Promise.resolve(6);
  }
  previewRule(): Promise<Readonly<Record<string, unknown>>> {
    return Promise.resolve({ sample_count: 1, match_count: 1 });
  }
  publishRule(): Promise<number> {
    return Promise.resolve(1);
  }
  rollbackRule(): Promise<number> {
    return Promise.resolve(2);
  }
  addRuleExemption(): Promise<number> {
    return Promise.resolve(3);
  }
  saveFilter(): Promise<number> {
    return Promise.resolve(4);
  }
  listWork(): Promise<ModerationWorkResult> {
    return Promise.resolve({ items: [], total: 0, page: 1 });
  }
}

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
  const service = new FakeModeration();
  const controller = new WebModerationController(auth, service);
  const cookie = await createSessionCookie(secret, {
    userId: "42",
    username: "Operator",
    role,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    communityId: 7,
    sessionVersion: 2,
  });
  return {
    handler: createApp(undefined, auth, undefined, undefined, controller)
      .handler(),
    headers: { cookie: `qbot4k_session=${cookie}` },
    service,
  };
}

Deno.test("moderation workspace and APIs bind the active tenant", async () => {
  const { handler, headers, service } = await fixture("analyst");
  const page = await handler(
    new Request("http://localhost/moderation", { headers }),
  );
  assertEquals(page.status, 200);
  const html = await page.text();
  assertEquals(html.includes("Open reviews"), true);
  assertEquals(html.includes("Work queue"), true);
  assertEquals(html.includes('action="/moderation/bulk"'), true);
  assertEquals(html.includes("Create draft"), true);
  const api = await handler(
    new Request("http://localhost/api/actions", { headers }),
  );
  assertEquals(api.status, 200);
  assertEquals((await api.json()).items[0].status, "pending");
  assertEquals(service.communities, [7, 7]);
});

Deno.test("no-JavaScript review form requires manage capability and permanent ban phrase", async () => {
  const analyst = await fixture("analyst");
  const forbidden = await analyst.handler(
    new Request("http://localhost/moderation/reviews/5/resolve", {
      method: "POST",
      headers: {
        ...analyst.headers,
        "content-type": "application/x-www-form-urlencoded",
      },
      body: "resolution=dismissed",
    }),
  );
  assertEquals(forbidden.status, 403);

  const moderator = await fixture("moderator");
  const rejected = await moderator.handler(
    new Request("http://localhost/moderation/reviews/5/resolve", {
      method: "POST",
      headers: {
        ...moderator.headers,
        "content-type": "application/x-www-form-urlencoded",
        origin: "http://localhost",
      },
      body: "resolution=confirmed&action_type=ban&note=evidence",
    }),
  );
  assertEquals(rejected.status, 302);
  assertEquals(
    rejected.headers.get("location")?.includes(
      "Permanent%20ban%20confirmation%20required",
    ),
    true,
  );
  assertEquals(moderator.service.resolutions.length, 0);

  const accepted = await moderator.handler(
    new Request("http://localhost/moderation/reviews/5/resolve", {
      method: "POST",
      headers: {
        ...moderator.headers,
        "content-type": "application/x-www-form-urlencoded",
        origin: "http://localhost",
      },
      body:
        "resolution=confirmed&action_type=ban&confirmation=PERMANENT+BAN&duration_seconds=30&note=evidence",
    }),
  );
  assertEquals(accepted.status, 302);
  assertEquals(moderator.service.resolutions[0].communityId, 7);
  assertEquals(moderator.service.resolutions[0].operatorId, 42);
});

Deno.test("bulk moderation requires exact execution confirmation", async () => {
  const { handler, headers, service } = await fixture("admin");
  const request = (confirmation: string) =>
    handler(
      new Request("http://localhost/api/moderation/bulk", {
        method: "POST",
        headers: {
          ...headers,
          "content-type": "application/json",
          origin: "http://localhost",
        },
        body: JSON.stringify({
          target_platform_account_ids: [8, 8, 9],
          action_type: "ban",
          reason: "raid",
          dry_run: false,
          confirmation,
        }),
      }),
    );
  assertEquals((await request("BULK BAN 2")).status, 409);
  assertEquals(service.bulkInputs.length, 0);
  assertEquals((await request("BULK PERMANENT BAN 2")).status, 200);
  assertEquals(service.bulkInputs[0].communityId, 7);
});

Deno.test("moderation rule API preserves saved response envelope", async () => {
  const { handler, headers } = await fixture("admin");
  const response = await handler(
    new Request("http://localhost/api/moderation/rules", {
      method: "POST",
      headers: {
        ...headers,
        "content-type": "application/json",
        origin: "http://localhost",
      },
      body: JSON.stringify({
        name: "Links",
        rule_type: "link_restriction",
        pattern: "http",
        severity: "high",
        platform_scope: ["discord"],
      }),
    }),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "saved", rule_id: 6 });
});

Deno.test("legacy user moderation form records a scoped completed action", async () => {
  const moderator = await fixture("moderator");
  const response = await moderator.handler(
    new Request(
      "http://localhost/users/4/moderation",
      {
        method: "POST",
        headers: {
          ...moderator.headers,
          "content-type": "application/x-www-form-urlencoded",
          origin: "http://localhost",
        },
        body: "target_platform_account_id=8&action_type=warn&reason=evidence",
      },
    ),
  );
  assertEquals(response.status, 302);
  assertEquals(
    response.headers.get("location"),
    "/users/4?mod_status=Moderation%20action%20warn%20recorded",
  );
  assertEquals(moderator.service.userActions[0].communityId, 7);
  assertEquals(moderator.service.userActions[0].userId, 4);
});
