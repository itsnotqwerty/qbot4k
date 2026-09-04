import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { Observation } from "../src/core/models.ts";
import type { ObservationCollector } from "../src/domain/observations.ts";
import {
  PostgresTwitchIngestionService,
  PostgresTwitchInstallationHealth,
} from "../src/providers/twitch/twitch_ingestion.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
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

class RecordingCollector implements ObservationCollector {
  readonly observations: Observation[] = [];
  collect(observation: Observation) {
    this.observations.push(observation);
    return Promise.resolve({
      observationId: 7,
      status: "persisted",
      analysisJobId: 8,
    });
  }
}

const payload = {
  message_id: "message-1",
  timestamp: "2026-09-04T12:00:00Z",
  channel: "Broadcaster",
  content: "hello",
  user_id: "user-1",
  username: "Analyst",
  badges: ["moderator"],
};

Deno.test("Twitch ingestion discovers active installation channels", async () => {
  const connection = new FakeConnection([[
    { external_community_id: "alpha" },
    { external_community_id: "beta" },
  ]]);
  assertEquals(
    await new PostgresTwitchIngestionService(
      connection,
      new RecordingCollector(),
    ).channels(),
    ["alpha", "beta"],
  );
  assertEquals(connection.calls[0].sql.includes("+        WHERE"), false);
  assertEquals(connection.calls[0].sql.includes("status='active'"), true);
});

Deno.test("Twitch ingestion scopes messages to an active installation", async () => {
  const collector = new RecordingCollector();
  const connection = new FakeConnection([[
    { installation_id: 9, community_id: 4, allow_bot_messages: 0 },
  ]]);
  const result = await new PostgresTwitchIngestionService(connection, collector)
    .ingest(payload);
  assertEquals(result?.status, "persisted");
  assertEquals(collector.observations[0].communityId, 4);
  assertEquals(collector.observations[0].installationId, 9);
  assertEquals(collector.observations[0].eventType, "message.created");
  assertEquals(connection.calls[0].parameters, ["Broadcaster"]);
});

Deno.test("Twitch ingestion ignores unknown channels", async () => {
  const collector = new RecordingCollector();
  assertEquals(
    await new PostgresTwitchIngestionService(
      new FakeConnection([[]]),
      collector,
    ).ingest(payload),
    null,
  );
  assertEquals(collector.observations, []);
});

Deno.test("Twitch installation health persists joins and reconnect failures", async () => {
  const connection = new FakeConnection([
    [
      { id: 9, community_id: 4, external_community_id: "broadcaster-1" },
    ],
    [],
    [],
    [],
  ]);
  const health = new PostgresTwitchInstallationHealth(connection);
  await health.ready(["#Alpha"]);
  await health.failed("connection reset");
  assertEquals(connection.calls[0].parameters, ['["alpha"]']);
  assertEquals(connection.calls[0].sql.includes("health_status='ready'"), true);
  assertEquals(
    connection.calls[2].sql.includes("integration.twitch_verified"),
    true,
  );
  assertEquals(
    connection.calls[3].sql.includes("reconnect_attempts=reconnect_attempts+1"),
    true,
  );
  assertEquals(connection.calls[3].parameters, ["connection reset"]);
});
