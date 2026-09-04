import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { PostgresProviderOwnershipLease } from "../src/providers/provider_ownership.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]>) {}
  query(sql: string, parameters: readonly DatabaseParameter[] = []) {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.responses.shift() ?? []);
  }
  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

Deno.test("provider lease acquires only runtime-owned installations", async () => {
  const connection = new FakeConnection([[], [{ installation_id: 9 }]]);
  assertEquals(
    await new PostgresProviderOwnershipLease(connection).acquire(
      9,
      "twitch-1",
      120,
    ),
    true,
  );
  assertEquals(connection.calls[0].sql.includes("'python'"), true);
  assertEquals(connection.calls[1].sql.includes("owner_runtime=$4"), true);
  assertEquals(connection.calls[1].parameters, [9, "twitch-1", 120, "deno"]);
});

Deno.test("provider lease renewal ownership and release bind exact holder", async () => {
  const connection = new FakeConnection([
    [{ installation_id: 9 }],
    [{ one: 1 }],
    [{ installation_id: 9 }],
  ]);
  const lease = new PostgresProviderOwnershipLease(connection);
  assertEquals(await lease.renew(9, "worker-1", 60), true);
  assertEquals(await lease.owns(9, "worker-1"), true);
  assertEquals(await lease.release(9, "worker-1"), true);
  assertEquals(connection.calls[0].parameters, [9, "worker-1", 60, "deno"]);
  assertEquals(connection.calls[1].sql.includes("lease_expires_at"), true);
  assertEquals(connection.calls[2].parameters, [9, "worker-1", "deno"]);
});

Deno.test("provider installations have independent lease keys", async () => {
  const connection = new FakeConnection([[], [], [], [{
    installation_id: 10,
  }]]);
  const lease = new PostgresProviderOwnershipLease(connection);
  assertEquals(await lease.acquire(9, "worker-1"), false);
  assertEquals(await lease.acquire(10, "worker-1"), true);
  assertEquals(connection.calls[1].parameters[0], 9);
  assertEquals(connection.calls[3].parameters[0], 10);
});
