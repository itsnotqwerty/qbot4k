import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { ProcessingJobStore } from "../src/jobs/jobs.ts";
import {
  MaintenanceOrchestrator,
  PostgresMetricsRollupRepository,
} from "../src/jobs/maintenance.ts";

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

Deno.test("metrics rollups preserve Python metric names and UTC day buckets", async () => {
  const connection = new FakeConnection([Array.from(
    { length: 5 },
    (_, index) => ({ metric_name: `metric-${index}` }),
  )]);
  assertEquals(
    await new PostgresMetricsRollupRepository(connection).refresh(
      new Date("2026-09-04T18:30:00Z"),
    ),
    5,
  );
  assertEquals(connection.calls[0].parameters, [
    "2026-09-04T00:00:00.000Z",
    "2026-09-04T18:30:00.000Z",
  ]);
  for (
    const metric of [
      "messages_total",
      "open_reviews",
      "pending_actions",
      "observations_total",
      "observations_24h",
    ]
  ) assertEquals(connection.calls[0].sql.includes(`'${metric}'`), true);
});

Deno.test("maintenance orchestrates only completed Deno operations in order", async () => {
  const calls: string[] = [];
  const jobs = {
    recoverExpired: () => {
      calls.push("recover");
      return Promise.resolve(2);
    },
  } as unknown as ProcessingJobStore;
  const orchestrator = new MaintenanceOrchestrator(
    jobs,
    {
      purge: () => {
        calls.push("retention");
        return Promise.resolve({
          deletedMessages: 3,
          deletedObservations: 4,
          deletedAuditLogRows: 5,
          deletedSignalRuns: 6,
          deletedScoreRuns: 7,
          deletedProcessingJobs: 8,
        });
      },
    },
    {
      flush: () => {
        calls.push("archive");
        return Promise.resolve(9);
      },
    },
    {
      refresh: () => {
        calls.push("rollups");
        return Promise.resolve(5);
      },
    },
    {
      queueCheckpointReminders: () => {
        calls.push("reminders");
        return Promise.resolve(4);
      },
    },
  );

  assertEquals(
    await orchestrator.run(new Date("2026-09-04T18:30:00Z"), 90, "/archive"),
    {
      deletedMessages: 3,
      deletedObservations: 4,
      deletedAuditLogRows: 5,
      deletedSignalRuns: 6,
      deletedScoreRuns: 7,
      deletedProcessingJobs: 8,
      recoveredProcessingJobs: 2,
      rawEventsArchived: 9,
      rollupRows: 5,
      checkpointRemindersQueued: 4,
    },
  );
  assertEquals(calls, [
    "recover",
    "archive",
    "retention",
    "rollups",
    "reminders",
  ]);
});
