import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  type NotificationGateway,
  PostgresNotificationRepository,
} from "../src/domain/notifications.ts";

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

class FakeGateway implements NotificationGateway {
  readonly posts: Array<{
    target: string;
    payload: Readonly<Record<string, unknown>>;
  }> = [];
  constructor(private readonly statuses: number[]) {}
  post(
    target: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    this.posts.push({ target, payload });
    return Promise.resolve(this.statuses.shift() ?? 204);
  }
}

Deno.test("incident notifications enforce severity and deduplicate atomically", async () => {
  const connection = new FakeConnection([
    [],
    [{
      id: 7,
      community_id: 2,
      severity: "high",
      title: "Raid detected",
      summary: "Coordinated joins",
      status: "active",
      escalation_level: 2,
    }],
    [
      { id: 10, minimum_severity: "medium" },
      { id: 11, minimum_severity: "critical" },
    ],
    [{ id: 20 }],
  ]);
  assertEquals(
    await new PostgresNotificationRepository(connection).queueIncident(7),
    1,
  );
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
  assertEquals(connection.calls[2].parameters, [2]);
  assertEquals(connection.calls[3].parameters, [
    10,
    7,
    '{"escalation_level":2,"incident_id":7,"severity":"high","status":"active","summary":"Coordinated joins","title":"Raid detected"}',
  ]);
  assertEquals(
    connection.calls[3].sql.includes("status IN ('pending','delivered')"),
    true,
  );
});

Deno.test("pending notifications are tenant locked and map provider payloads", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 21,
        payload_json:
          '{"incident_id":7,"severity":"high","title":"Raid","summary":"Details"}',
        destination_type: "discord_webhook",
        target: "https://discord.test/hook",
      },
      {
        id: 22,
        payload_json:
          '{"severity":"critical","title":"Outage","summary":"Offline"}',
        destination_type: "slack_webhook",
        target: "https://slack.test/hook",
      },
    ],
    [],
    [],
  ]);
  const gateway = new FakeGateway([204, 503]);
  assertEquals(
    await new PostgresNotificationRepository(connection, gateway)
      .dispatchPending(2, 25),
    1,
  );
  assertEquals(connection.calls[0].parameters, [2, 25]);
  assertEquals(
    connection.calls[0].sql.includes("FOR UPDATE OF d SKIP LOCKED"),
    true,
  );
  assertEquals(gateway.posts[0], {
    target: "https://discord.test/hook",
    payload: {
      content: "[HIGH] Raid",
      embeds: [{
        description: "Details",
        fields: [{ name: "Incident", value: "7" }],
      }],
    },
  });
  assertEquals(gateway.posts[1].payload, {
    text: "[CRITICAL] Outage\nOffline",
  });
  assertEquals(connection.calls[1].sql.includes("status='delivered'"), true);
  assertEquals(connection.calls[2].sql.includes("status='retry'"), true);
  assertEquals(connection.calls[2].parameters, [
    "notification provider returned HTTP 503",
    22,
  ]);
});
