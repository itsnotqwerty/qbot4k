import { assertEquals } from "jsr:@std/assert@1.0.14";
import type { DatabaseConnection, DatabaseRow } from "../src/data/database.ts";
import { PostgresCommandExecutionRepository } from "../src/domain/command_execution.ts";

class FakeConnection implements DatabaseConnection {
  readonly queries: { sql: string; parameters: readonly unknown[] }[] = [];
  constructor(
    private readonly rows: DatabaseRow[] = [],
    private readonly router?: (sql: string) => DatabaseRow[] | undefined,
  ) {}

  query(sql: string, parameters: readonly unknown[] = []) {
    this.queries.push({ sql, parameters });
    return Promise.resolve(this.router?.(sql) ?? this.rows);
  }

  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

const message = {
  observationId: 44,
  communityId: 7,
  platform: "discord",
  channelId: "channel-1",
  username: "operator",
  isModerator: true,
  roleNames: [],
};

Deno.test("command execution renders built-in templates and queues Discord reply", async () => {
  const connection = new FakeConnection([{
    title: "Roll",
    description_template: "${query} rolled ${1..6}",
    footer_template: null,
  }]);
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "!roll 20" });
  assertEquals(result.actionJobType, "discord.message.send");
  const enqueue = connection.queries.at(-1)!;
  assertEquals(enqueue.sql.includes("INSERT INTO processing_jobs"), true);
  assertEquals(enqueue.parameters[1], "discord.message.send");
  const payload = JSON.parse(String(enqueue.parameters[3]));
  assertEquals(payload.channel_id, "channel-1");
  assertEquals(payload.rendered_reply.embeds[0].title, "Roll");
});

Deno.test("simple commands render as plain content so links preview", async () => {
  const connection = new FakeConnection([], (sql) => {
    if (sql.includes("FROM command_definitions")) return [];
    if (sql.includes("FROM simple_command_definitions")) {
      return [{ response_template: "https://example.com/docs" }];
    }
    return undefined;
  });
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "!docs" });
  assertEquals(result.actionJobType, "discord.message.send");
  const payload = JSON.parse(String(connection.queries.at(-1)!.parameters[3]));
  assertEquals(payload.rendered_reply.content, "https://example.com/docs");
  assertEquals(payload.rendered_reply.embeds, undefined);
});

Deno.test("moderator addcom persists custom command before queueing response", async () => {
  const connection = new FakeConnection();
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "!addcom docs Read the docs" });
  assertEquals(result.actionJobType, "discord.message.send");
  const upsert = connection.queries.find((query) =>
    query.sql.includes("INSERT INTO simple_command_definitions")
  )!;
  assertEquals(upsert.parameters, ["docs", "Read the docs"]);
});

Deno.test("commands excluded from normalization resolve nothing", async () => {
  const connection = new FakeConnection();
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "ordinary chat" });
  assertEquals(result.commandName, null);
  assertEquals(connection.queries.length, 0);
});

Deno.test("moderator alias creates a pointer to an existing command", async () => {
  const connection = new FakeConnection([], (sql) => {
    // aliasTarget(aliasName) -> not an alias; existingAlias check -> none
    if (sql.includes("FROM simple_command_definitions")) return [];
    // resolveAnyCommand(target) builtin lookup -> found
    if (sql.includes("FROM command_definitions")) {
      return [{ description_template: "https://example.com/docs" }];
    }
    return undefined;
  });
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "!alias d docs" });
  assertEquals(result.actionJobType, "discord.message.send");
  const upsert = connection.queries.find((query) =>
    query.sql.includes("INSERT INTO simple_command_definitions")
  )!;
  assertEquals(upsert.parameters, ["d", "alias:docs"]);
  const payload = JSON.parse(String(connection.queries.at(-1)!.parameters[3]));
  assertEquals(
    payload.rendered_reply.embeds[0].description.includes(
      "Aliased !d to !docs.",
    ),
    true,
  );
});

Deno.test("alias resolution renders the target command output", async () => {
  // simpleCommand call sequence for "!d":
  //   1. aliasTarget(d)    -> "alias:docs"  (d is an alias)
  //   2. aliasTarget(docs) -> plain text    (docs is not an alias, stop)
  //   3. simpleCommand(docs) render -> plain text
  const simpleResponses = [
    [{ response_template: "alias:docs" }],
    [{ response_template: "https://example.com/docs" }],
    [{ response_template: "https://example.com/docs" }],
  ];
  let simpleCalls = 0;
  const connection = new FakeConnection([], (sql) => {
    if (sql.includes("FROM simple_command_definitions")) {
      return simpleResponses[
        Math.min(simpleCalls++, simpleResponses.length - 1)
      ];
    }
    if (sql.includes("FROM command_definitions")) return [];
    return undefined;
  });
  const result = await new PostgresCommandExecutionRepository(connection)
    .execute({ ...message, contentRaw: "!d" });
  assertEquals(result.actionJobType, "discord.message.send");
  const payload = JSON.parse(String(connection.queries.at(-1)!.parameters[3]));
  assertEquals(payload.rendered_reply.content, "https://example.com/docs");
});
