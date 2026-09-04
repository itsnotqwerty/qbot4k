import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { ProcessingJob } from "../src/jobs/jobs.ts";
import type { TwitchTokenManager } from "../src/providers/twitch/twitch_auth.ts";
import {
  FetchTwitchModerationApi,
  PostgresTwitchActionRepository,
  TWITCH_MODERATION_JOB_TYPE,
} from "../src/providers/twitch/twitch_actions.ts";

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

Deno.test("Twitch moderation retries and bounds timeout duration", async () => {
  const requests: Request[] = [];
  const api = new FetchTwitchModerationApi(
    {
      validateToken: () =>
        Promise.resolve({
          accessToken: "token",
          login: "mod",
          clientId: "client",
          userId: "mod-1",
        }),
    } as TwitchTokenManager,
    (input, init) => {
      requests.push(new Request(input, init));
      return Promise.resolve(
        requests.length === 1
          ? new Response("busy", { status: 503 })
          : Response.json({ data: [{ id: "ban-1" }] }),
      );
    },
    () => Promise.resolve(),
  );
  assertEquals(
    await api.moderate(
      "broadcaster-1",
      "user-1",
      "timeout",
      "reason",
      2_000_000,
    ),
    {
      data: [{ id: "ban-1" }],
    },
  );
  assertEquals(requests.length, 2);
  assertEquals(JSON.parse(await requests[1].text()).data.duration, 1_209_600);
});

Deno.test("Twitch moderation repository completes tenant-scoped capable actions", async () => {
  const connection = new FakeConnection([[
    {
      id: 5,
      action_type: "ban",
      reason: "abuse",
      duration_seconds: null,
      platform_user_id: "user-1",
      external_community_id: "broadcaster-1",
      capabilities_json: '["moderation_actions"]',
    },
  ], []]);
  const calls: unknown[][] = [];
  const repository = new PostgresTwitchActionRepository(connection, {
    moderate: (...args) => {
      calls.push(args);
      return Promise.resolve({ data: [{ id: "ban-1" }] });
    },
  });
  await repository.moderate(
    {
      id: 1,
      communityId: 4,
      stage: "action",
      jobType: TWITCH_MODERATION_JOB_TYPE,
      observationId: null,
      payload: { message_id: 12 },
      attempts: 0,
      maxAttempts: 3,
    } satisfies ProcessingJob,
  );
  assertEquals(calls, [["broadcaster-1", "user-1", "ban", "abuse", 0]]);
  assertEquals(connection.calls[0].parameters, [4, 12]);
  assertEquals(connection.calls[1].sql.includes("status='completed'"), true);
});
