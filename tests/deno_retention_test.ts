import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { PostgresRetentionRepository } from "../src/ops/retention.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];

  constructor(private readonly responses: Array<readonly DatabaseRow[]>) {}

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

Deno.test("retention purges tenant data and global operational history", async () => {
  const connection = new FakeConnection([
    [
      { community_id: 2, message_retention_days: 30 },
      { community_id: 3, message_retention_days: 7 },
    ],
    [{ id: 1 }, { id: 2 }],
    [{ id: 3 }],
    [{ id: 4 }],
    [],
    [{ id: 5 }, { id: 6 }, { id: 7 }],
    [{ id: 8 }],
    [{ id: 9 }, { id: 10 }],
    [{ id: 11 }],
  ]);
  const report = await new PostgresRetentionRepository(connection).purge(
    new Date("2026-09-04T12:00:00Z"),
    90,
  );

  assertEquals(report, {
    deletedMessages: 3,
    deletedObservations: 1,
    deletedAuditLogRows: 3,
    deletedSignalRuns: 2,
    deletedScoreRuns: 1,
    deletedProcessingJobs: 1,
  });
  assertEquals(connection.calls[1].parameters, [
    2,
    "2026-08-05T12:00:00.000Z",
  ]);
  assertEquals(connection.calls[3].parameters, [
    3,
    "2026-08-28T12:00:00.000Z",
  ]);
  assertEquals(connection.calls[5].parameters, [
    "2026-06-06T12:00:00.000Z",
  ]);
  assertEquals(connection.calls[1].sql.includes("legal_holds"), true);
  assertEquals(
    connection.calls[2].sql.includes("messages.observation_id"),
    true,
  );
  assertEquals(connection.calls[6].sql.includes("SELECT MAX(id)"), true);
  assertEquals(
    connection.calls[8].sql.includes("'completed','failed','cancelled'"),
    true,
  );
});

Deno.test("retention rejects invalid execution inputs", async () => {
  const repository = new PostgresRetentionRepository(new FakeConnection([]));
  await assertRejects(
    () => repository.purge(new Date("invalid"), 30),
    TypeError,
    "now is invalid",
  );
  await assertRejects(
    () => repository.purge(new Date(), 0),
    TypeError,
    "positive integer",
  );
});
