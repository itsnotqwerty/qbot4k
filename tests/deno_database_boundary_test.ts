import {
  assertEquals,
  assertRejects,
  assertThrows,
} from "jsr:@std/assert@1.0.14";
import { TenantContext } from "../src/core/contexts.ts";
import {
  type DatabaseConnection,
  type DatabaseParameter,
  type DatabaseRow,
  withTestTransaction,
} from "../src/data/database.ts";
import {
  decodeInstallation,
  OperatorAuthRepository,
  ScopedRepository,
} from "../src/data/repository.ts";
import { DashboardQueryRepository } from "../src/web/web_queries.ts";
import { PostgresModerationRepository } from "../src/domain/moderation.ts";

class FakeConnection implements DatabaseConnection {
  readonly queries: {
    sql: string;
    parameters: readonly DatabaseParameter[];
  }[] = [];
  rows: readonly DatabaseRow[] = [];
  rowQueue: (readonly DatabaseRow[])[] = [];
  transactionOutcome: "none" | "committed" | "rolled_back" = "none";

  async query(
    sql: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.queries.push({ sql, parameters });
    return await Promise.resolve(this.rowQueue.shift() ?? this.rows);
  }

  async transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    try {
      const result = await callback(this);
      this.transactionOutcome = "committed";
      return result;
    } catch (error) {
      this.transactionOutcome = "rolled_back";
      throw error;
    }
  }
}

const installationRow = (communityId = 2): DatabaseRow => ({
  id: 4,
  community_id: communityId,
  platform: "discord",
  external_community_id: "guild-9",
  display_name: "Fixture Guild",
  status: "active",
  scopes_json: '["bot"]',
  metadata_json: '{"region":"test"}',
  capabilities_json: '["moderation_actions"]',
  health_status: "healthy",
  token_reference: null,
});

Deno.test("installation lookup always binds compound tenant scope", async () => {
  const connection = new FakeConnection();
  connection.rows = [installationRow()];
  const repository = new ScopedRepository(connection, new TenantContext(2, 4));
  const installation = await repository.findInstallation("discord", "guild-9");

  assertEquals(installation?.communityId, 2);
  assertEquals(installation?.metadata, { region: "test" });
  assertEquals(connection.queries[0].parameters, [2, "discord", "guild-9", 4]);
  assertEquals(connection.queries[0].sql.includes("community_id = $1"), true);
  assertEquals(connection.queries[0].sql.includes("id = $4"), true);
});

Deno.test("typed decoders reject cross-tenant rows", () => {
  assertThrows(
    () => decodeInstallation(installationRow(9), new TenantContext(2)),
    TypeError,
    "community_id does not match tenant context",
  );
  assertThrows(
    () => decodeInstallation(installationRow(), new TenantContext(2, 5)),
    TypeError,
    "installation_id does not match tenant context",
  );
});

Deno.test("repository transactions commit and propagate failures", async () => {
  const connection = new FakeConnection();
  const repository = new ScopedRepository(connection, new TenantContext(2));
  assertEquals(
    await repository.transaction(() => Promise.resolve("committed")),
    "committed",
  );
  assertEquals(connection.transactionOutcome, "committed");
  await assertRejects(
    () => repository.transaction(() => Promise.reject(new Error("rollback"))),
    Error,
    "rollback",
  );
  assertEquals(connection.transactionOutcome, "rolled_back");
});

Deno.test("operator community switching binds membership and audit scope atomically", async () => {
  const connection = new FakeConnection();
  connection.rowQueue = [[{ role: "moderator" }], []];
  const repository = new OperatorAuthRepository(connection);
  const role = await repository.switchCommunity(42, 8, 7);

  assertEquals(role, "moderator");
  assertEquals(connection.transactionOutcome, "committed");
  assertEquals(connection.queries[0].parameters, [42, 8]);
  assertEquals(connection.queries[0].sql.includes("operator_id = $1"), true);
  assertEquals(connection.queries[0].sql.includes("community_id = $2"), true);
  assertEquals(connection.queries[1].parameters.slice(0, 4), [
    42,
    "community.switched",
    "community",
    8,
  ]);
  assertEquals(
    String(connection.queries[1].parameters[4]),
    '{"from_community_id":7}',
  );
});

Deno.test("dashboard user queries bind tenant input and whitelist sorting", async () => {
  const connection = new FakeConnection();
  connection.rows = [{
    user_id: "5",
    primary_display_name: "Ada",
    current_reputation_score: 700,
    candidate_flag: false,
    account_count: "1",
    message_count: "4",
  }];
  const repository = new DashboardQueryRepository(connection);
  const query = new URLSearchParams({
    q: "Ada",
    sort: "score; DROP TABLE users",
    dir: "desc",
  });
  const users = await repository.users(7, query);

  assertEquals(users[0].primary_display_name, "Ada");
  assertEquals(users[0].user_id, 5);
  assertEquals(users[0].message_count, 4);
  assertEquals(users[0].candidate_flag, false);
  assertEquals(connection.queries[0].parameters, [7, "%Ada%", 25, 0]);
  assertEquals(connection.queries[0].sql.includes("m.community_id = $1"), true);
  assertEquals(connection.queries[0].sql.includes("DROP TABLE"), false);
});

Deno.test("review resolution creates one bounded provider action and audit atomically", async () => {
  const connection = new FakeConnection();
  connection.rowQueue = [
    [{
      status: "open",
      message_id: 11,
      platform: "discord",
      observation_id: 13,
      platform_account_id: 17,
    }],
    [{ id: 23 }],
    [],
    [],
    [],
  ];
  const repository = new PostgresModerationRepository(connection);
  const actionId = await repository.resolveReview({
    communityId: 7,
    operatorId: 42,
    reviewId: 5,
    resolution: "confirmed",
    actionType: "timeout",
    durationSeconds: 999_999_999,
    note: "confirmed evidence",
  });

  assertEquals(actionId, 23);
  assertEquals(connection.transactionOutcome, "committed");
  assertEquals(connection.queries[0].parameters, [5, 7]);
  assertEquals(connection.queries[1].parameters[7], 2_419_200);
  assertEquals(
    connection.queries[2].parameters[4],
    "review:5:moderation:timeout",
  );
  assertEquals(
    connection.queries[2].sql.includes(
      "ON CONFLICT(idempotency_key) DO NOTHING",
    ),
    true,
  );
  assertEquals(connection.queries[4].parameters[0], 42);
  assertEquals(
    String(connection.queries[4].parameters[2]).includes('"community_id":7'),
    true,
  );
});

Deno.test("assigned appeals require an independent sanction reviewer", async () => {
  const connection = new FakeConnection();
  connection.rowQueue = [
    [{ status: "open", assigned_operator_id: 42, moderation_action_id: 9 }],
    [{ actor_type: "operator", actor_id: 42 }],
  ];
  const repository = new PostgresModerationRepository(connection);

  await assertRejects(
    () => repository.resolveMember(7, 42, "appeal", 5, "upheld", "reviewed"),
    TypeError,
    "different reviewer",
  );
  assertEquals(connection.transactionOutcome, "rolled_back");
  assertEquals(connection.queries[0].parameters, [5, 7]);
  assertEquals(connection.queries.length, 2);
});

Deno.test("enforced rule publication requires an independent approver", async () => {
  const connection = new FakeConnection();
  connection.rowQueue = [[{
    id: 12,
    moderation_rule_id: 3,
    created_by_operator_id: 42,
    config_json:
      '{"name":"Links","rule_type":"link_restriction","pattern":"http","severity":"high","auto_enforce_action":"timeout","action_duration_seconds":600,"platform_scope":["discord"]}',
  }]];
  const repository = new PostgresModerationRepository(connection);

  await assertRejects(
    () => repository.publishRule(7, 42, 12, "enforce"),
    TypeError,
    "different operator",
  );
  assertEquals(connection.transactionOutcome, "rolled_back");
  assertEquals(connection.queries[0].parameters, [12, 7]);
});

Deno.test("moderation work filters bind tenant operator and pagination", async () => {
  const connection = new FakeConnection();
  connection.rows = [{ item_id: 2, total_count: "26", work_type: "review" }];
  const result = await new PostgresModerationRepository(connection).listWork(
    7,
    42,
    {
      queue: "mine",
      search: "raid",
      severity: "HIGH",
      platform: "Discord",
      page: 2,
    },
  );
  assertEquals(result.total, 26);
  assertEquals(result.page, 2);
  assertEquals(connection.queries[0].parameters, [
    7,
    42,
    "%raid%",
    "high",
    "discord",
    25,
  ]);
  assertEquals(connection.queries[0].sql.includes("UNION ALL"), true);
});

Deno.test("test transaction helper always rolls back", async () => {
  const connection = new FakeConnection();
  const result = await withTestTransaction(
    connection,
    new TenantContext(2),
    (repository) => Promise.resolve(repository.tenant.communityId),
  );
  assertEquals(result, 2);
  assertEquals(connection.transactionOutcome, "rolled_back");
});

Deno.test("Deno loads Python-encrypted installation credentials in tenant scope", async () => {
  const connection = new FakeConnection();
  connection.rows = [{
    access_token_ciphertext: new TextEncoder().encode(
      "gAAAAABqmhnih5i9oieQHZyW3kwJhIQClJVIM2uXNyZpS8Udhu9THLWwNJ3ItrBPuw2xULIonBUdzBoOmUyQ7zhmL6_pGe6p99Yl0Hj4VtTeoLKjWqiCYEM=",
    ),
    refresh_token_ciphertext: null,
    scopes_json: '["moderator:read:followers"]',
    key_version: 1,
    rotation_count: 1,
  }];
  const repository = new ScopedRepository(connection, new TenantContext(2, 4));
  const credentials = await repository.loadInstallationCredentials(
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
  );

  assertEquals(credentials.accessToken, "python-access-token");
  assertEquals(credentials.scopes, ["moderator:read:followers"]);
  assertEquals(connection.queries[0].parameters, [4, 2]);
});

Deno.test("credential rotation is scoped, encrypted, and audited transactionally", async () => {
  const connection = new FakeConnection();
  connection.rowQueue = [
    [{ status: "active" }],
    [{ id: 12, rotation_count: 3 }],
    [],
    [],
  ];
  const repository = new ScopedRepository(connection, new TenantContext(2, 4));
  const reference = await repository.storeInstallationCredentials({
    accessToken: "new-access",
    refreshToken: "new-refresh",
    scopes: ["scope:b", "scope:a", "scope:a"],
    encryptionKey: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    keyVersion: 2,
    actorOperatorId: 7,
  });

  assertEquals(reference, "installation-credential:12");
  assertEquals(connection.transactionOutcome, "committed");
  assertEquals(connection.queries[0].parameters, [4, 2]);
  assertEquals(connection.queries[2].parameters, [
    reference,
    '["scope:a","scope:b"]',
    4,
    2,
  ]);
  assertEquals(connection.queries[3].parameters[0], 7);
  assertEquals(
    String(connection.queries[3].parameters[2]).includes('"rotation_count":3'),
    true,
  );
});
