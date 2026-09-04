import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { Observation } from "../src/core/models.ts";
import {
  PostgresObservationRepository,
  TenantQuotaExceededError,
} from "../src/domain/observations.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
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

const observation: Observation = {
  platform: "discord",
  eventType: "message.created",
  occurredAt: "2026-09-03T10:00:00+00:00",
  communityId: 7,
  installationId: null,
  externalEventId: "message-1",
  actorPlatformUserId: "user-1",
  actorUsername: "Sam",
  targetPlatformUserId: null,
  containerId: "channel-1",
  contextId: "guild-1",
  text: "hello",
  attributes: { z: 2, a: 1 },
  rawPayload: { nested: { z: true, a: false }, id: "message-1" },
  schemaVersion: 1,
};

Deno.test("collection atomically binds tenant identities job and archive hash", async () => {
  const connection = new FakeConnection([
    [],
    [{ usage_count: 1 }],
    [{ id: 3 }],
    [{ id: 11 }],
    [{ id: 17 }],
    [],
    [{ usage_count: 1 }],
    [],
  ]);
  const result = await new PostgresObservationRepository(connection).collect(
    observation,
  );
  assertEquals(result, {
    observationId: 11,
    status: "persisted",
    analysisJobId: 17,
  });
  assertEquals(connection.calls[3].parameters[1], 7);
  assertEquals(connection.calls[4].parameters, [
    7,
    "analyze.message.created",
    11,
    "observation:11:message.created:v1",
  ]);
  const archive = connection.calls[7];
  assertEquals(archive.parameters[0], 7);
  assertEquals(
    archive.parameters[6],
    "700a61753b3caec1361d54665b3cc3144a5f79741f87cfc4535faab09ee8f4e9",
  );
  assertEquals(
    archive.parameters[7],
    '{"id":"message-1","nested":{"a":false,"z":true}}',
  );
});

Deno.test("duplicate observations consume ingestion quota but create no job or archive", async () => {
  const connection = new FakeConnection([
    [],
    [{ usage_count: 2 }],
    [{ id: 3 }],
    [],
  ]);
  assertEquals(
    await new PostgresObservationRepository(connection).collect(observation),
    {
      observationId: null,
      status: "duplicate",
      analysisJobId: null,
    },
  );
  assertEquals(connection.calls.length, 4);
});

Deno.test("collection rejects cross-tenant installations before quota or persistence", async () => {
  const connection = new FakeConnection([[]]);
  await assertRejects(
    () =>
      new PostgresObservationRepository(connection).collect({
        ...observation,
        installationId: 91,
      }),
    TypeError,
    "not owned",
  );
  assertEquals(connection.calls.length, 1);
});

Deno.test("ingestion quota failure stops observation persistence", async () => {
  const connection = new FakeConnection([[{
    limit_count: 1,
    window_seconds: 60,
  }], [{ usage_count: 2 }]]);
  await assertRejects(
    () => new PostgresObservationRepository(connection).collect(observation),
    TenantQuotaExceededError,
    "ingestion quota exceeded",
  );
  assertEquals(connection.calls.length, 2);
});
