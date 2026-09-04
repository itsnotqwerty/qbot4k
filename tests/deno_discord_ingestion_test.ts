import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  PostgresDiscordIngestionService,
  PostgresDiscordInstallationHealth,
} from "../src/providers/discord/discord_ingestion.ts";
import type { Observation } from "../src/core/models.ts";
import type { ObservationCollector } from "../src/domain/observations.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];
  constructor(private readonly rows: readonly DatabaseRow[]) {}
  query(
    _sql: string,
    _parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.calls.push({ sql: _sql, parameters: _parameters });
    return Promise.resolve(this.rows);
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
      observationId: 12,
      status: "persisted",
      analysisJobId: 14,
    });
  }
}

const installation = [{
  installation_id: 9,
  community_id: 4,
  allow_bot_messages: 0,
}];

Deno.test("Discord ingestion resolves installation and scopes messages", async () => {
  const collector = new RecordingCollector();
  const result = await new PostgresDiscordIngestionService(
    new FakeConnection(installation),
    collector,
  ).ingest("MESSAGE_CREATE", {
    id: "message-1",
    timestamp: "2026-09-04T12:00:00Z",
    guild_id: "guild-1",
    channel_id: "channel-1",
    content: "hello",
    author: { id: "user-1", username: "Analyst", bot: false },
  });
  assertEquals(result?.status, "persisted");
  assertEquals(collector.observations[0].communityId, 4);
  assertEquals(collector.observations[0].installationId, 9);
  assertEquals(collector.observations[0].eventType, "message.created");
});

Deno.test("Discord ingestion rejects disabled bots and unknown guilds", async () => {
  const collector = new RecordingCollector();
  const service = new PostgresDiscordIngestionService(
    new FakeConnection(installation),
    collector,
  );
  assertEquals(
    await service.ingest("MESSAGE_CREATE", {
      id: "message-1",
      timestamp: "2026-09-04T12:00:00Z",
      guild_id: "guild-1",
      channel_id: "channel-1",
      content: "hello",
      author: { id: "bot-1", username: "Bot", bot: true },
    }),
    null,
  );
  assertEquals(
    await new PostgresDiscordIngestionService(
      new FakeConnection([]),
      collector,
    ).ingest("GUILD_MEMBER_ADD", {
      guild_id: "unknown",
      user: { id: "user-1" },
    }),
    null,
  );
  assertEquals(collector.observations, []);
});

Deno.test("Discord gateway health persists READY and reconnect outcomes", async () => {
  const connection = new FakeConnection([{
    id: 9,
    community_id: 4,
    external_community_id: "guild-1",
  }]);
  const health = new PostgresDiscordInstallationHealth(connection);
  await health.ready(["guild-1", "guild-2"]);
  await health.failed("gateway closed");
  assertEquals(connection.calls[0].parameters, [
    '["guild-1","guild-2"]',
  ]);
  assertEquals(connection.calls[0].sql.includes("status='active'"), true);
  assertEquals(connection.calls[1].parameters, ['["guild-1","guild-2"]']);
  assertEquals(connection.calls[2].sql.includes("discord_verified"), true);
  assertEquals(connection.calls[3].parameters, ["gateway closed"]);
  assertEquals(connection.calls[3].sql.includes("reconnect_attempts+1"), true);
});

Deno.test("Discord ingestion collects supported gateway events", async () => {
  const collector = new RecordingCollector();
  await new PostgresDiscordIngestionService(
    new FakeConnection(installation),
    collector,
  ).ingest("GUILD_MEMBER_ADD", {
    guild_id: "guild-1",
    joined_at: "2026-09-04T12:00:00Z",
    user: { id: "user-1", username: "Member" },
  });
  assertEquals(collector.observations[0].eventType, "member.joined");
  assertEquals(collector.observations[0].targetPlatformUserId, "user-1");
});
