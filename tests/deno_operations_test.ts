import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import { parseArguments } from "../cli.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { MIGRATION_NAMES, OperationalService } from "../src/data/operations.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]> = []) {}
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

Deno.test("operational CLI parses migration and invitation commands", () => {
  assertEquals(parseArguments(["migrate", "--env-file=/tmp/qbot.env"]), {
    command: "migrate",
    expiresHours: 72,
    envFile: "/tmp/qbot.env",
  });
  assertEquals(
    parseArguments([
      "issue-pilot-invite",
      "7",
      "--expires-hours=24",
      "--operator-id=3",
    ]),
    {
      command: "issue-pilot-invite",
      expiresHours: 24,
      communityId: 7,
      operatorId: 3,
    },
  );
  assertEquals(
    parseArguments([
      "transfer-job-ownership",
      "analyze.message.created",
      "deno",
      "--shadow-runtime=none",
      "--operator-id=3",
    ]),
    {
      command: "transfer-job-ownership",
      expiresHours: 72,
      jobType: "analyze.message.created",
      ownerRuntime: "deno",
      shadowRuntime: null,
      operatorId: 3,
    },
  );
  assertEquals(
    parseArguments([
      "transfer-installation-ownership",
      "12",
      "deno",
      "--operator-id=3",
    ]),
    {
      command: "transfer-installation-ownership",
      expiresHours: 72,
      installationId: 12,
      ownerRuntime: "deno",
      operatorId: 3,
    },
  );
});

Deno.test("job ownership transfer drains leases and records an audit", async () => {
  const connection = new FakeConnection([
    [],
    [{ count: 0 }],
    [{ owner_runtime: "python", shadow_runtime: "deno" }],
    [],
    [],
  ]);
  await new OperationalService(connection, "").transferJobOwnership(
    "analyze.message.created",
    "deno",
    null,
    9,
  );
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
  assertEquals(connection.calls[3].parameters, [
    "analyze.message.created",
    "deno",
    null,
  ]);
  assertEquals(connection.calls[4].parameters[0], "operator");
  assertEquals(connection.calls[4].sql.includes("ownership_transferred"), true);
});

Deno.test("job ownership audit rejects missing and Python owners", async () => {
  const connection = new FakeConnection([[
    { job_type: "analyze.message.created", owner_runtime: "python" },
    { job_type: "fixture.unregistered", owner_runtime: "missing" },
  ]]);
  assertEquals(
    await new OperationalService(connection, "").auditJobOwnership(),
    {
      key: "job_ownership",
      status: "fail",
      detail:
        "not Deno-owned: analyze.message.created=python, fixture.unregistered=missing",
    },
  );
  assertEquals(
    connection.calls[0].sql.includes("IS DISTINCT FROM 'deno'"),
    true,
  );
});

Deno.test("installation ownership transfer drains leases and records an audit", async () => {
  const connection = new FakeConnection([
    [],
    [{ platform: "twitch", owner_runtime: "python", lease_holder: null }],
    [],
    [],
  ]);
  await new OperationalService(connection, "")
    .transferInstallationOwnership(12, "deno", 9);
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
  assertEquals(connection.calls[2].parameters, [12, "deno"]);
  assertEquals(connection.calls[3].parameters[0], "operator");
  assertEquals(
    connection.calls[3].sql.includes(
      "provider_installation.ownership_transferred",
    ),
    true,
  );
});

Deno.test("migration rejects an unmanaged PostgreSQL schema", async () => {
  const connection = new FakeConnection([[], [{ owns_schema: true }], [{
    registry: null,
  }], [{ count: 1 }]]);
  await assertRejects(
    () =>
      new OperationalService(
        connection,
        "CREATE TABLE IF NOT EXISTS fixture(id BIGINT);",
      ).migrate(),
    TypeError,
    "contains unmanaged tables",
  );
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
});

Deno.test("migration applies schema and ordered markers transactionally", async () => {
  const responses: Array<readonly DatabaseRow[]> = [
    [],
    [{ owns_schema: true }],
    [{ registry: null }],
    [{ count: 0 }],
    [],
    ...MIGRATION_NAMES.map(() => []),
    [],
    [],
    [],
    [],
    [],
    [],
    [],
    [{ table_name: "schema_migrations" }],
  ];
  const connection = new FakeConnection(responses);
  const tables = await new OperationalService(
    connection,
    "CREATE TABLE IF NOT EXISTS schema_migrations(version BIGINT);",
  ).migrate();
  assertEquals(tables, ["schema_migrations"]);
  assertEquals(connection.calls[5].parameters, [1, MIGRATION_NAMES[0]]);
  assertEquals(connection.calls[31].parameters, [27, MIGRATION_NAMES[26]]);
  assertEquals(connection.calls[32].parameters, [28, MIGRATION_NAMES[27]]);
});

Deno.test("pilot invitation stores only a hash and writes its audit event", async () => {
  const connection = new FakeConnection([[{ id: 19 }], []]);
  const code = await new OperationalService(connection, "").issuePilotInvite(
    4,
    24,
    9,
    new Date("2026-09-04T12:00:00Z"),
  );
  assertEquals(code.length, 32);
  assertEquals(connection.calls[0].parameters[0], 4);
  assertEquals(String(connection.calls[0].parameters[1]).length, 64);
  assertEquals(connection.calls[0].parameters.includes(code), false);
  assertEquals(connection.calls[1].parameters[0], "operator");
  assertEquals(
    connection.calls[1].sql.includes("pilot.invitation_issued"),
    true,
  );
});
