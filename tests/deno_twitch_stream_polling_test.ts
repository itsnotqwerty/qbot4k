import { assertEquals, assertExists } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { CollectedObservation, Observation } from "../src/core/models.ts";
import type { ObservationCollector } from "../src/domain/observations.ts";
import type { TwitchTokenManager } from "../src/providers/twitch/twitch_auth.ts";
import { PostgresTwitchStreamPoller } from "../src/providers/twitch/twitch_stream_polling.ts";

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

class RecordingCollector implements ObservationCollector {
  readonly observations: Observation[] = [];
  collect(observation: Observation): Promise<CollectedObservation> {
    this.observations.push(observation);
    return Promise.resolve({
      observationId: this.observations.length,
      analysisJobId: null,
      status: "persisted",
    });
  }
}

const tokens = {
  validateToken: () =>
    Promise.resolve({
      accessToken: "access-1",
      login: "bot",
      clientId: "client-1",
      userId: "bot-1",
    }),
} as TwitchTokenManager;

const installation = {
  id: 9,
  community_id: 4,
  external_community_id: "broadcaster-1",
  display_name: "Channel",
  capabilities_json: '["events"]',
};

Deno.test("Twitch stream polling emits tenant-scoped start transitions with retry", async () => {
  const connection = new FakeConnection([[installation], []]);
  const collector = new RecordingCollector();
  let attempts = 0;
  const poller = new PostgresTwitchStreamPoller(
    connection,
    collector,
    tokens,
    () => {
      attempts += 1;
      return Promise.resolve(
        attempts === 1 ? new Response("busy", { status: 503 }) : Response.json({
          data: [{
            id: "stream-1",
            title: "Launch",
            game_name: "Science",
            started_at: "2026-09-04T12:00:00Z",
          }],
        }),
      );
    },
    () => Promise.resolve(),
  );
  assertEquals(await poller.poll(new Date("2026-09-04T12:05:00Z")), {
    checked: 1,
    transitions: 1,
  });
  assertEquals(attempts, 2);
  assertEquals(collector.observations[0].eventType, "stream.started");
  assertEquals(collector.observations[0].communityId, 4);
  assertEquals(collector.observations[0].installationId, 9);
  assertEquals(collector.observations[0].contextId, "Channel");
  assertExists(collector.observations[0].externalEventId);
  assertEquals(
    collector.observations[0].externalEventId.startsWith(
      "poll:Channel:stream-1:stream.started:",
    ),
    true,
  );
});

Deno.test("Twitch stream polling suppresses unchanged state and emits offline end", async () => {
  const liveAttributes = JSON.stringify({
    stream_id: "stream-1",
    title: "Launch",
    game_name: "Science",
  });
  const unchangedConnection = new FakeConnection([
    [installation],
    [{ event_type: "stream.started", attributes_json: liveAttributes }],
  ]);
  const unchangedCollector = new RecordingCollector();
  assertEquals(
    await new PostgresTwitchStreamPoller(
      unchangedConnection,
      unchangedCollector,
      tokens,
      () =>
        Promise.resolve(Response.json({
          data: [{
            id: "stream-1",
            title: "Launch",
            game_name: "Science",
            started_at: "2026-09-04T12:00:00Z",
          }],
        })),
    ).poll(),
    { checked: 1, transitions: 0 },
  );

  const endedConnection = new FakeConnection([
    [installation],
    [{ event_type: "stream.started", attributes_json: liveAttributes }],
  ]);
  const endedCollector = new RecordingCollector();
  assertEquals(
    await new PostgresTwitchStreamPoller(
      endedConnection,
      endedCollector,
      tokens,
      () => Promise.resolve(Response.json({ data: [] })),
    ).poll(new Date("2026-09-04T13:00:00Z")),
    { checked: 1, transitions: 1 },
  );
  assertEquals(endedCollector.observations[0].eventType, "stream.ended");
  assertEquals(endedCollector.observations[0].attributes.stream_id, "stream-1");
});
