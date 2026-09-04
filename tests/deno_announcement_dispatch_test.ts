import { assertEquals } from "jsr:@std/assert@1.0.14";
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
  ]);
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
