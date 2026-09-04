import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { TwitchTokenManager } from "../src/providers/twitch/twitch_auth.ts";
import { PostgresTwitchEventSubReconciler } from "../src/providers/twitch/twitch_eventsub_control.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];

  constructor(
    private readonly responses: Array<readonly DatabaseRow[]> = [[{ id: 9 }]],
  ) {}

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

const tokens = {
  validateToken: () =>
    Promise.resolve({
      accessToken: "access-1",
      login: "broadcaster",
      clientId: "client-1",
      userId: "broadcaster-1",
    }),
} as TwitchTokenManager;

Deno.test("EventSub reconciliation records inventory and creates only missing subscriptions", async () => {
  const connection = new FakeConnection();
  const requests: Request[] = [];
  const responses = [
    Response.json({
      data: [{
        id: "sub-online",
        type: "stream.online",
        version: "1",
        condition: { broadcaster_user_id: "broadcaster-1" },
        status: "enabled",
        transport: {
          method: "webhook",
          callback: "https://example.test/eventsub",
        },
        cost: 0,
      }],
      pagination: { cursor: "next" },
    }),
    Response.json({ data: [], pagination: {} }),
    Response.json({
      data: [{
        id: "sub-offline",
        type: "stream.offline",
        version: "1",
        condition: { broadcaster_user_id: "broadcaster-1" },
        status: "webhook_callback_verification_pending",
        transport: {
          method: "webhook",
          callback: "https://example.test/eventsub",
        },
        cost: 0,
      }],
    }),
  ];
  const reconciler = new PostgresTwitchEventSubReconciler(
    connection,
    tokens,
    "https://example.test/eventsub",
    "0123456789abcdef",
    (input, init) => {
      requests.push(new Request(input, init));
      return Promise.resolve(responses.shift()!);
    },
  );
  assertEquals(
    await reconciler.reconcile(4, 9, [
      {
        type: "stream.online",
        condition: { broadcaster_user_id: "broadcaster-1" },
      },
      {
        type: "stream.offline",
        condition: { broadcaster_user_id: "broadcaster-1" },
      },
    ]),
    { existing: 1, created: 1, desired: 2 },
  );
  assertEquals(requests.length, 3);
  assertEquals(new URL(requests[1].url).searchParams.get("after"), "next");
  assertEquals(requests[2].method, "POST");
  const created = JSON.parse(await requests[2].text());
  assertEquals(created.type, "stream.offline");
  assertEquals(created.transport.secret, "0123456789abcdef");
  assertEquals(
    connection.calls.filter((call) =>
      call.sql.includes("INSERT INTO twitch_eventsub_subscriptions")
    ).length,
    2,
  );
  const health = connection.calls.at(-1)!;
  assertEquals(health.sql.includes("health_status='ready'"), true);
  assertEquals(health.parameters, [9, 4]);
});

Deno.test("EventSub reconciliation retries transient inventory failures", async () => {
  const connection = new FakeConnection();
  let attempts = 0;
  const reconciler = new PostgresTwitchEventSubReconciler(
    connection,
    tokens,
    "https://example.test/eventsub",
    "0123456789abcdef",
    () => {
      attempts += 1;
      return Promise.resolve(
        attempts === 1
          ? new Response("busy", { status: 503 })
          : Response.json({ data: [], pagination: {} }),
      );
    },
    () => Promise.resolve(),
  );
  assertEquals(await reconciler.reconcile(4, 9, []), {
    existing: 0,
    created: 0,
    desired: 0,
  });
  assertEquals(attempts, 2);
});

Deno.test("EventSub reconciliation refuses an unscoped installation", async () => {
  const connection = new FakeConnection([[], []]);
  let requests = 0;
  await assertRejects(
    () =>
      new PostgresTwitchEventSubReconciler(
        connection,
        tokens,
        "https://example.test/eventsub",
        "0123456789abcdef",
        () => {
          requests += 1;
          return Promise.resolve(Response.json({ data: [] }));
        },
      ).reconcile(4, 9, []),
    TypeError,
    "not active or capable for the tenant",
  );
  assertEquals(requests, 0);
  assertEquals(connection.calls.at(-1)?.parameters.slice(0, 2), [9, 4]);
});
