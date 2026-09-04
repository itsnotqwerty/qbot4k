import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  DISCORD_MESSAGE_JOB_TYPE,
  DISCORD_MODERATION_JOB_TYPE,
  type DiscordApi,
  DiscordApiError,
  FetchDiscordApi,
  PostgresDiscordActionRepository,
} from "../src/providers/discord/discord_actions.ts";
import type { ProcessingJob } from "../src/jobs/jobs.ts";

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

class FakeApi implements DiscordApi {
  readonly calls: Array<{ operation: string; values: unknown[] }> = [];
  sendMessage(channelId: string, payload: Readonly<Record<string, unknown>>) {
    this.calls.push({ operation: "send", values: [channelId, payload] });
    return Promise.resolve({ id: "sent-1" });
  }
  deleteMessage(channelId: string, messageId: string) {
    this.calls.push({ operation: "delete", values: [channelId, messageId] });
    return Promise.resolve({});
  }
  timeoutMember(
    guildId: string,
    userId: string,
    reason: string,
    durationSeconds: number,
  ) {
    this.calls.push({
      operation: "timeout",
      values: [guildId, userId, reason, durationSeconds],
    });
    return Promise.resolve({ status: "accepted" });
  }
  banMember(guildId: string, userId: string, reason: string) {
    this.calls.push({ operation: "ban", values: [guildId, userId, reason] });
    return Promise.resolve({ status: "accepted" });
  }
}

function job(
  jobType: string,
  payload: Readonly<Record<string, unknown>>,
): ProcessingJob {
  return {
    id: 5,
    communityId: 4,
    stage: "action",
    jobType,
    observationId: 7,
    payload,
    attempts: 1,
    maxAttempts: 5,
  };
}

Deno.test("Discord API honors rate-limit retry metadata", async () => {
  const requests: Request[] = [];
  const delays: number[] = [];
  const responses = [
    new Response('{"retry_after":0.25}', { status: 429 }),
    new Response('{"id":"sent-1"}', { status: 200 }),
  ];
  const api = new FetchDiscordApi(
    "Bot secret",
    (input, init) => {
      requests.push(new Request(input, init));
      return Promise.resolve(responses.shift()!);
    },
    (milliseconds) => {
      delays.push(milliseconds);
      return Promise.resolve();
    },
  );
  assertEquals(await api.sendMessage("channel-1", { content: "hello" }), {
    id: "sent-1",
  });
  assertEquals(requests.length, 2);
  assertEquals(requests[0].headers.get("Authorization"), "Bot secret");
  assertEquals(delays, [250]);
});

Deno.test("Discord API classifies revoked tokens as permanent", async () => {
  let requests = 0;
  const api = new FetchDiscordApi("Bot revoked", () => {
    requests += 1;
    return Promise.resolve(new Response("unauthorized", { status: 401 }));
  });
  const error = await assertRejects(
    () => api.sendMessage("channel-1", { content: "hello" }),
    DiscordApiError,
    "HTTP 401",
  );
  assertEquals(error.retryable, false);
  assertEquals(requests, 1);
});

Deno.test("Discord message action sends the rendered reply", async () => {
  const api = new FakeApi();
  await new PostgresDiscordActionRepository(
    new FakeConnection([[{ capabilities_json: '["announcements"]' }]]),
    api,
  ).sendMessage(job(DISCORD_MESSAGE_JOB_TYPE, {
    channel_id: "channel-1",
    rendered_reply: { content: "hello" },
  }));
  assertEquals(api.calls, [{
    operation: "send",
    values: ["channel-1", { content: "hello" }],
  }]);
});

Deno.test("Discord message action refuses unscoped installations", async () => {
  const api = new FakeApi();
  await assertRejects(
    () =>
      new PostgresDiscordActionRepository(new FakeConnection([[]]), api)
        .sendMessage(job(DISCORD_MESSAGE_JOB_TYPE, {
          channel_id: "channel-1",
          rendered_reply: { content: "hello" },
        })),
    Error,
    "not active for the job tenant",
  );
  assertEquals(api.calls, []);
});

Deno.test("Discord moderation executes and persists provider acceptance", async () => {
  const connection = new FakeConnection([[{
    id: 8,
    action_type: "timeout",
    reason: "spam",
    duration_seconds: 600,
    platform_message_id: "message-1",
    channel_id: "channel-1",
    platform_user_id: "user-1",
    external_community_id: "guild-1",
    capabilities_json: '["moderation_actions"]',
  }], []]);
  const api = new FakeApi();
  await new PostgresDiscordActionRepository(connection, api).moderate(
    job(DISCORD_MODERATION_JOB_TYPE, { message_id: 12 }),
  );
  assertEquals(api.calls, [
    { operation: "delete", values: ["channel-1", "message-1"] },
    {
      operation: "timeout",
      values: ["guild-1", "user-1", "spam", 600],
    },
  ]);
  assertEquals(connection.calls[1].sql.includes("status='completed'"), true);
  assertEquals(connection.calls[1].parameters[0], 8);
});

Deno.test("Discord moderation refuses installations without capability", async () => {
  const connection = new FakeConnection([[{
    id: 8,
    action_type: "ban",
    capabilities_json: "[]",
  }], []]);
  const api = new FakeApi();
  await new PostgresDiscordActionRepository(connection, api).moderate(
    job(DISCORD_MODERATION_JOB_TYPE, { message_id: 12 }),
  );
  assertEquals(api.calls, []);
  assertEquals(connection.calls[1].sql.includes("status='failed'"), true);
  assertEquals(connection.calls[1].parameters, [
    8,
    "moderation capability is disabled",
  ]);
});
