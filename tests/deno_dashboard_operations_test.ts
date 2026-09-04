import { assertEquals } from "jsr:@std/assert@1.0.14";
import { PostgresDashboardOperations } from "../src/web/dashboard_operations.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    readonly sql: string;
    readonly parameters: readonly DatabaseParameter[];
  }> = [];

  constructor(private readonly responses: Array<readonly DatabaseRow[]> = []) {}

  query(
    sql: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.responses.shift() ?? []);
  }

  transaction<T>(
    operation: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return operation(this);
  }
}

Deno.test("manual go-live queues tenant and installation scoped Discord actions", async () => {
  const connection = new FakeConnection([
    [{
      stream_id: 8,
      stream_key: "FixtureCaster",
      title: "Fixture stream",
      installation_id: 12,
      channel_id: "channel-7",
    }],
    [{ id: 31 }],
    [],
    [],
  ]);
  const operations = new PostgresDashboardOperations(
    connection,
    "qbot4k.service",
  );
  assertEquals(await operations.goLive(7, 42), 1);
  assertEquals(connection.calls[0].parameters, [7]);
  assertEquals(
    connection.calls[0].sql.includes("installation_runtime_leases"),
    true,
  );
  assertEquals(connection.calls[1].parameters[0], 7);
  assertEquals(connection.calls[1].parameters[1], 12);
  assertEquals(connection.calls[2].parameters[0], 7);
  assertEquals(connection.calls[2].parameters[1], "discord.message.send");
  assertEquals(connection.calls[3].parameters[0], 42);
});

Deno.test("dashboard restart is audited before host execution", async () => {
  const connection = new FakeConnection([[]]);
  const restarted: string[] = [];
  const operations = new PostgresDashboardOperations(
    connection,
    "qbot4k.service",
    (service) => restarted.push(service),
  );
  assertEquals(await operations.restart(42), "qbot4k.service");
  assertEquals(connection.calls[0].parameters[0], 42);
  assertEquals(
    connection.calls[0].parameters[1],
    "dashboard.restart_requested",
  );
  assertEquals(restarted, ["qbot4k.service"]);
});

Deno.test("dashboard reset locks, counts, truncates, restores seeds, and audits", async () => {
  const connection = new FakeConnection([
    [],
    [{ table_name: "audit_log" }, { table_name: "communities" }],
    [{ count: 3 }],
    [{ count: 2 }],
    [],
    [],
    [],
  ]);
  const report = await new PostgresDashboardOperations(
    connection,
    "qbot4k.service",
  ).resetDatabase(42);
  assertEquals(report, { rowsDeleted: 5 });
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
  assertEquals(connection.calls[4].sql.includes("TRUNCATE TABLE"), true);
  assertEquals(
    connection.calls[4].sql.includes("RESTART IDENTITY CASCADE"),
    true,
  );
  assertEquals(connection.calls[5].sql.includes("Default Organization"), true);
  assertEquals(connection.calls[6].parameters[0], 42);
});
