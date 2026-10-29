import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import { DiscordAnnouncementSender } from "../src/providers/discord/discord_announcements.ts";
import { FetchDiscordApi } from "../src/providers/discord/discord_actions.ts";
import type { AnnouncementSender } from "../src/jobs/announcement_dispatch.ts";
import { PostgresAnnouncementDispatcher } from "../src/jobs/announcement_dispatch.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";

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

Deno.test("Discord announcements verify the server and record the sent message ID", async () => {
  const requests: Array<{ url: string; method: string; body: unknown }> = [];
  const sender = new DiscordAnnouncementSender(
    new FetchDiscordApi(
      "test-token",
      (input, init) => {
        requests.push({
          url: String(input),
          method: String(init?.method),
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        return Promise.resolve(Response.json(
          init?.method === "GET"
            ? { id: "channel-1", guild_id: "guild-1" }
            : { id: "message-1" },
        ));
      },
    ),
  );
  assertEquals(sender.platform, "discord");
  assertEquals(
    await sender.send("discord", "guild-1", "channel-1", "Update", {}),
    "message-1",
  );
  assertEquals(requests, [
    {
      url: "https://discord.com/api/v10/channels/channel-1",
      method: "GET",
      body: null,
    },
    {
      url: "https://discord.com/api/v10/channels/channel-1/messages",
      method: "POST",
      body: { content: "Update", allowed_mentions: { parse: [] } },
    },
  ]);
});

Deno.test("Discord announcements reject other platforms and servers without sending", async () => {
  const methods: string[] = [];
  const sender = new DiscordAnnouncementSender(
    new FetchDiscordApi(
      "test-token",
      (_input, init) => {
        methods.push(String(init?.method));
        return Promise.resolve(Response.json({ guild_id: "another-guild" }));
      },
    ),
  );
  await assertRejects(
    () => sender.send("twitch", "guild-1", "channel-1", "Update", {}),
    TypeError,
    "unsupported Discord announcement platform",
  );
  assertEquals(methods, []);
  await assertRejects(
    () => sender.send("discord", "guild-1", "channel-1", "Update", {}),
    TypeError,
    "does not belong to the target server",
  );
  assertEquals(methods, ["GET"]);
});

Deno.test("scheduled announcements select fairly and record provider success", async () => {
  const row = {
    id: 7,
    community_id: 2,
    platform: "discord",
    target_external_id: "channel-1",
    body: "Scheduled update",
    installation_id: 3,
    external_community_id: "guild-1",
    source_json: '{"type":"system"}',
    capabilities_json: '["announcements"]',
  };
  const connection = new FakeConnection([
    [row],
    [{ id: 7 }],
    [{ attempt: 1 }],
    [{ id: 9 }],
    [],
    [],
    [],
  ]);
  const calls: unknown[][] = [];
  const sender: AnnouncementSender = {
    platform: "discord",
    send: (...args) => {
      calls.push(args);
      return Promise.resolve("message-1");
    },
  };
  assertEquals(
    await new PostgresAnnouncementDispatcher(connection, sender).dispatch(
      new Date("2026-09-04T12:00:00Z"),
      50,
      5,
    ),
    1,
  );
  assertEquals(connection.calls[0].parameters, [
    "2026-09-04T12:00:00.000Z",
    5,
    50,
    "discord",
  ]);
  assertEquals(connection.calls[0].sql.includes("AND a.platform=$4"), true);
  assertEquals(connection.calls[0].sql.includes("ROW_NUMBER() OVER"), true);
  assertEquals(calls, [["discord", "guild-1", "channel-1", "Scheduled update", {
    type: "system",
  }]]);
  assertEquals(connection.calls[4].sql.includes("status='delivered'"), true);
  assertEquals(connection.calls[6].parameters[0], "announcement.delivered");
});

Deno.test("announcement capability failures are persisted without provider calls", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 8,
        community_id: 2,
        platform: "discord",
        target_external_id: "channel-1",
        body: "Blocked",
        installation_id: 4,
        external_community_id: "guild-1",
        source_json: "{}",
        capabilities_json: '["events"]',
      },
    ],
    [{ id: 8 }],
    [{ attempt: 1 }],
    [{ id: 10 }],
    [],
    [],
    [],
  ]);
  let sends = 0;
  assertEquals(
    await new PostgresAnnouncementDispatcher(connection, {
      platform: "discord",
      send: () => {
        sends += 1;
        return Promise.resolve("unused");
      },
    }).dispatch(),
    0,
  );
  assertEquals(sends, 0);
  assertEquals(connection.calls[4].sql.includes("status='failed'"), true);
  assertEquals(
    String(connection.calls[4].parameters[0]).includes(
      "does not support announcements",
    ),
    true,
  );
  assertEquals(
    connection.calls[6].parameters[0],
    "announcement.delivery_failed",
  );
});

Deno.test("Twitch announcement selection is restricted before claiming deliveries", async () => {
  const connection = new FakeConnection([[]]);
  assertEquals(
    await new PostgresAnnouncementDispatcher(connection, {
      platform: "twitch",
      send: () => {
        throw new Error("No announcements should be sent");
      },
    }).dispatch(new Date("2026-10-28T12:00:00Z")),
    0,
  );
  assertEquals(connection.calls.length, 1);
  assertEquals(connection.calls[0].sql.includes("AND a.platform=$4"), true);
  assertEquals(connection.calls[0].parameters[3], "twitch");
});
