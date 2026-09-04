import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { PostgresAnalyticsRepository } from "../src/domain/analytics_persistence.ts";

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

Deno.test("emerging-topic refresh replaces tenant state and appends history", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 1,
        text_raw: "Launch launch",
        context_id: "stream-a",
        container_id: "channel-a",
        occurred_at: "2026-09-04T10:00:00.000Z",
      },
      {
        id: 2,
        text_raw: "Launch launch",
        context_id: "stream-a",
        container_id: "channel-a",
        occurred_at: "2026-09-04T11:00:00.000Z",
      },
    ],
    [],
    [],
    [],
    [],
    [],
    [],
    [],
  ]);
  const count = await new PostgresAnalyticsRepository(connection)
    .refreshEmergingTopics(2, new Date("2026-09-04T12:00:00Z"));

  assertEquals(count, 2);
  assertEquals(connection.calls[0].parameters, [
    2,
    "2026-08-27T12:00:00.000Z",
  ]);
  assertEquals(connection.calls[1].parameters, [2]);
  assertEquals(connection.calls[2].parameters, [2]);
  assertEquals(connection.calls[3].sql.includes("emerging_topics"), true);
  assertEquals(connection.calls[4].sql.includes("topic_history"), true);
  assertEquals(connection.calls[5].sql.includes("ON CONFLICT"), true);
  assertEquals(
    String(connection.calls[3].parameters[1]).startsWith("2:"),
    true,
  );
});

Deno.test("empty emerging-topic refresh clears only current tenant state", async () => {
  const connection = new FakeConnection([[], [], []]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection).refreshEmergingTopics(
      3,
      new Date("2026-09-04T12:00:00Z"),
    ),
    0,
  );
  assertEquals(connection.calls.slice(1).map((call) => call.parameters), [
    [3],
    [3],
  ]);
});

Deno.test("graph refresh replaces tenant metrics and appends history", async () => {
  const connection = new FakeConnection([
    [
      {
        source_user_id: 4,
        target_user_id: 5,
        strength: 2,
        last_observed_at: "2026-09-04T11:00:00.000Z",
      },
    ],
    [],
    [],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection).refreshGraphAnalytics(
      7,
      new Date("2026-09-04T12:00:00Z"),
    ),
    2,
  );
  assertEquals(connection.calls[0].parameters, [7]);
  assertEquals(connection.calls[1].parameters, [7]);
  assertEquals(
    connection.calls[2].sql.includes("community_graph_metrics"),
    true,
  );
  assertEquals(
    connection.calls[3].sql.includes("community_graph_metric_history"),
    true,
  );
});

Deno.test("identity refresh upserts only tenant accounts", async () => {
  const connection = new FakeConnection([[
    {
      id: 10,
      platform: "discord",
      platform_user_id: "sam-one",
      username: "SamOne",
      user_id: null,
      guild_or_channel_context: "shared",
    },
    {
      id: 11,
      platform: "twitch",
      platform_user_id: "sam_one",
      username: "sam_one",
      user_id: null,
      guild_or_channel_context: "shared",
    },
  ], [{ id: 21 }]]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection)
      .refreshIdentitySuggestions(8),
    1,
  );
  assertEquals(connection.calls[0].parameters, [8]);
  assertEquals(connection.calls[1].parameters[0], 8);
  assertEquals(connection.calls[1].sql.includes("status='pending'"), true);
});

Deno.test("community cohort refresh replaces tenant baselines and anomalies", async () => {
  const signals = Array.from({ length: 6 }, (_, index) => ({
    user_id: index + 1,
    signal_key: "risk.composite",
    value_real: index === 5 ? 100 : index,
    confidence: 0.9,
  }));
  const memberships = Array.from({ length: 6 }, (_, index) => ({
    user_id: index + 1,
    cohort_type: "platform",
    cohort_key: "discord",
  }));
  const connection = new FakeConnection([
    signals,
    memberships,
    [],
    [],
    [],
    [],
    [],
    [],
    [],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection).refreshCommunityCohorts(
      9,
      new Date("2026-09-04T12:00:00Z"),
    ),
    1,
  );
  assertEquals(connection.calls.slice(0, 3).map((call) => call.parameters), [
    [9],
    [9],
    [9],
  ]);
  assertEquals(connection.calls[3].parameters, [9]);
  assertEquals(connection.calls[4].parameters, [9]);
  assertEquals(connection.calls[5].sql.includes("cohort_baselines"), true);
  assertEquals(
    connection.calls.some((call) => call.sql.includes("cohort_anomalies")),
    true,
  );
});

Deno.test("model evaluation persists one run and three threshold backtests", async () => {
  const connection = new FakeConnection([
    [
      { label_value: "positive", score: 80, alert_type: "risk" },
      { label_value: "negative", score: 60, alert_type: "risk" },
    ],
    [{ id: 42 }],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection).runModelEvaluation(
      new Date("2026-09-04T12:00:00Z"),
    ),
    42,
  );
  assertEquals(connection.calls[1].sql.includes("model_evaluation_runs"), true);
  assertEquals(
    connection.calls.slice(2).filter((call) =>
      call.sql.includes("threshold_backtests")
    ).length,
    3,
  );
});

Deno.test("derived signal refresh writes all reducer outputs", async () => {
  const connection = new FakeConnection([[
    {
      user_id: 4,
      message_count: 2,
      channel_count: 1,
      platform_count: 1,
      account_count: 1,
      eligible_message_count: 2,
      positive_count: 1,
      negative_count: 0,
      negative_points: 0,
      reply_count: 1,
      welcome_positive_count: 0,
      welcome_count: 0,
      welcome_duplicate_count: 0,
      finding_count: 0,
      severity_points: 0,
      moderation_penalty_points: 0,
      window_start: "2026-09-04T10:00:00Z",
      window_end: "2026-09-04T11:00:00Z",
    },
  ], ...Array.from({ length: 15 }, () => [])]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection).refreshDerivedSignals(
      new Date("2026-09-04T12:00:00Z"),
    ),
    1,
  );
  assertEquals(connection.calls.length, 16);
  assertEquals(connection.calls[1].parameters[1], "activity.message_count");
  assertEquals(connection.calls[15].parameters[1], "risk.composite");
});

Deno.test("live stream cohorts upsert all stable cohort keys", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 5,
        community_id: 2,
        started_at: "2026-09-04T10:00:00Z",
        ended_at: "2026-09-04T12:00:00Z",
      },
    ],
    [{
      id: 8,
      message_count: 3,
      returning: true,
      subscriber: true,
      vip: false,
      moderator: false,
    }],
    [],
    [],
    [],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresAnalyticsRepository(connection)
      .refreshLiveStreamCohorts(),
    1,
  );
  assertEquals(connection.calls.slice(2).map((call) => call.parameters[1]), [
    "unique",
    "new",
    "returning",
    "subscriber",
    "vip",
    "moderator",
  ]);
  assertEquals(connection.calls[4].parameters.slice(2), [1, 3]);
});
