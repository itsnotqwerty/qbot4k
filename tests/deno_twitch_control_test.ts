import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import type { TwitchTokenManager } from "../src/providers/twitch/twitch_auth.ts";
import { PostgresTwitchControlGateway } from "../src/providers/twitch/twitch_control.ts";

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

const tokens = {
  validateToken: () =>
    Promise.resolve({
      accessToken: "access-1",
      login: "moderator",
      clientId: "client-1",
      userId: "moderator-1",
    }),
} as TwitchTokenManager;

Deno.test("Twitch shield control persists provider confirmation and audit", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 9,
        external_community_id: "broadcaster-1",
        capabilities_json: '["live_controls"]',
      },
    ],
    [{ id: 12 }],
    [],
    [],
  ]);
  let request: Request | undefined;
  const gateway = new PostgresTwitchControlGateway(
    connection,
    tokens,
    (input, init) => {
      request = new Request(input, init);
      return Promise.resolve(new Response("{}", { status: 200 }));
    },
  );
  assertEquals(await gateway.shield(4, 7, "broadcaster-1", true), {
    action_id: 12,
    status: "confirmed",
    provider_status: 200,
    response: {},
  });
  assertEquals(request?.url.includes("broadcaster_id=broadcaster-1"), true);
  assertEquals(request?.headers.get("Authorization"), "Bearer access-1");
  assertEquals(connection.calls[2].sql.includes("status='confirmed'"), true);
  assertEquals(
    connection.calls[3].sql.includes("twitch.control_confirmed"),
    true,
  );
});

Deno.test("Twitch controls reject installations without capability", async () => {
  const connection = new FakeConnection([[
    { id: 9, external_community_id: "broadcaster-1", capabilities_json: "[]" },
  ]]);
  await assertRejects(
    () =>
      new PostgresTwitchControlGateway(connection, tokens).shield(
        4,
        7,
        "broadcaster-1",
        true,
      ),
    TypeError,
    "capability is disabled",
  );
  assertEquals(connection.calls.length, 1);
});

Deno.test("Twitch controls persist provider failure after action creation", async () => {
  const connection = new FakeConnection([
    [
      {
        id: 9,
        external_community_id: "broadcaster-1",
        capabilities_json: '["live_controls"]',
      },
    ],
    [{ id: 12 }],
    [],
  ]);
  const gateway = new PostgresTwitchControlGateway(
    connection,
    tokens,
    () => Promise.resolve(new Response("denied", { status: 403 })),
  );
  await assertRejects(
    () => gateway.shield(4, 7, "broadcaster-1", true),
    TypeError,
    "HTTP 403",
  );
  assertEquals(connection.calls[2].sql.includes("status='failed'"), true);
  assertEquals(connection.calls[2].parameters[0], 12);
});
