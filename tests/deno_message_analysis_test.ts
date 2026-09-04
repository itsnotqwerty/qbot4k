import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { PermanentJobError, type ProcessingJob } from "../src/jobs/jobs.ts";
import {
  MESSAGE_ANALYSIS_JOB_TYPE,
  PostgresMessageAnalysisRepository,
  PostgresMessageAnalysisShadowRunner,
} from "../src/jobs/message_analysis.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]> = []) {}
  rollbacks = 0;
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
    return callback(this).catch((error) => {
      this.rollbacks += 1;
      throw error;
    });
  }
}

const job: ProcessingJob = {
  id: 8,
  communityId: 2,
  stage: "analysis",
  jobType: MESSAGE_ANALYSIS_JOB_TYPE,
  observationId: 14,
  payload: {},
  attempts: 1,
  maxAttempts: 5,
};

Deno.test("message analysis projects and understands an observation atomically", async () => {
  const connection = new FakeConnection([
    [{
      id: 14,
      community_id: 2,
      installation_id: null,
      event_type: "message.created",
      platform: "discord",
      external_event_id: "message-14",
      actor_platform_account_id: 6,
      actor_platform_user_id: "user-6",
      actor_username: "Analyst",
      actor_user_id: null,
      container_id: "channel-4",
      context_id: "guild-3",
      text_raw: "hello there",
      attributes_json: "{}",
      occurred_at: "2026-09-04T12:00:00+00:00",
    }],
    [{ moderation_shadow_mode: 0 }],
    [],
    [{ id: 11 }],
    [],
    [{ id: 12 }],
    [],
    [],
    [],
  ]);
  await new PostgresMessageAnalysisRepository(connection).handle(job);
  assertEquals(
    connection.calls.some((call) => call.sql.includes("INSERT INTO messages")),
    true,
  );
  assertEquals(
    connection.calls.some((call) =>
      call.sql.includes("INSERT INTO content_analysis")
    ),
    true,
  );
  assertEquals(
    connection.calls.some((call) =>
      call.sql.includes("DELETE FROM content_entities")
    ),
    true,
  );
  assertEquals(connection.calls[0].parameters, [14, 2]);
});

Deno.test("message analysis shadow rolls projections back and records evidence", async () => {
  const connection = new FakeConnection([
    [{
      id: 8,
      community_id: 2,
      stage: "analysis",
      job_type: MESSAGE_ANALYSIS_JOB_TYPE,
      observation_id: 14,
      payload_json: "{}",
      attempts: 0,
      max_attempts: 5,
    }],
    [{
      id: 14,
      community_id: 2,
      installation_id: null,
      event_type: "message.created",
      platform: "discord",
      actor_platform_account_id: 6,
      actor_user_id: 12,
      container_id: "channel-4",
      text_raw: "hello there",
      attributes_json: "{}",
      occurred_at: "2026-09-04T12:00:00+00:00",
    }],
    [{ moderation_shadow_mode: 0 }],
    [],
    [{ id: 11 }],
    [],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresMessageAnalysisShadowRunner(connection).runNext(),
    true,
  );
  assertEquals(connection.rollbacks, 1);
  const evidence = connection.calls.at(-1)!;
  assertEquals(evidence.sql.includes("processing_job_shadow_runs"), true);
  assertEquals(evidence.parameters[1], "matched");
  assertEquals(
    String(evidence.parameters[2]).includes('"rolled_back":true'),
    true,
  );
});

Deno.test("message analysis permanently rejects malformed job contracts", async () => {
  await assertRejects(
    () =>
      new PostgresMessageAnalysisRepository(new FakeConnection()).handle({
        ...job,
        observationId: null,
      }),
    PermanentJobError,
    "no observation_id",
  );
});
