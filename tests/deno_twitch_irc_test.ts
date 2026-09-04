import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type { TwitchTokenManager } from "../src/providers/twitch/twitch_auth.ts";
import type { TwitchIngestionService } from "../src/providers/twitch/twitch_ingestion.ts";
import {
  TwitchIrcClient,
  type TwitchIrcConnection,
  type TwitchIrcTransport,
} from "../src/providers/twitch/twitch_irc.ts";

class ScriptedConnection implements TwitchIrcConnection {
  readonly writes: string[] = [];
  closed = false;
  constructor(private readonly lines: Array<string | null>) {}
  writeLine(line: string): Promise<void> {
    this.writes.push(line);
    return Promise.resolve();
  }
  readLine(): Promise<string | null> {
    return Promise.resolve(this.lines.shift() ?? null);
  }
  close(): void {
    this.closed = true;
  }
}

class ScriptedTransport implements TwitchIrcTransport {
  readonly connections: ScriptedConnection[];
  connectCount = 0;
  constructor(connection: ScriptedConnection | ScriptedConnection[]) {
    this.connections = Array.isArray(connection) ? connection : [connection];
  }
  connect(): Promise<TwitchIrcConnection> {
    const connection = this.connections[this.connectCount++];
    if (!connection) throw new TypeError("no scripted Twitch connection");
    return Promise.resolve(connection);
  }
}

class RecordingIngestion implements TwitchIngestionService {
  readonly payloads: Readonly<Record<string, unknown>>[] = [];
  channels(): Promise<readonly string[]> {
    return Promise.resolve(["#Alpha", "beta", "alpha"]);
  }
  ingest(payload: Readonly<Record<string, unknown>>) {
    this.payloads.push(payload);
    return Promise.resolve(null);
  }
}

Deno.test("Twitch IRC authenticates, joins, pongs, and ingests messages", async () => {
  const message =
    "@badges=;display-name=Analyst;id=m1;mod=0;tmi-sent-ts=1788523200000;user-id=u1 :analyst!analyst@analyst.tmi.twitch.tv PRIVMSG #alpha :hello";
  const connection = new ScriptedConnection([
    "PING :tmi.twitch.tv",
    message,
    null,
  ]);
  const ingestion = new RecordingIngestion();
  const tokens = {
    validateToken: () =>
      Promise.resolve({
        accessToken: "access-1",
        login: "qbot4k",
        clientId: "client-1",
        userId: "bot-1",
      }),
  } as TwitchTokenManager;
  const client = new TwitchIrcClient(
    tokens,
    new ScriptedTransport(connection),
    ingestion,
  );
  await assertRejects(
    () => client.connectOnce(new AbortController().signal),
    TypeError,
    "connection closed",
  );
  assertEquals(connection.writes, [
    "PASS oauth:access-1",
    "NICK qbot4k",
    "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership",
    "JOIN #alpha",
    "JOIN #beta",
    "PONG :tmi.twitch.tv",
  ]);
  assertEquals(ingestion.payloads.length, 1);
  assertEquals(ingestion.payloads[0].message_id, "m1");
  assertEquals(client.health().channels, ["alpha", "beta"]);
  assertEquals(connection.closed, true);
});

Deno.test("Twitch IRC sends messages only to joined channels while ready", async () => {
  const blocked = Promise.withResolvers<string | null>();
  const connection = new ScriptedConnection([]);
  connection.readLine = () => blocked.promise;
  const client = new TwitchIrcClient(
    {
      validateToken: () =>
        Promise.resolve({
          accessToken: "access-1",
          login: "qbot4k",
          clientId: "client-1",
          userId: "bot-1",
        }),
    } as TwitchTokenManager,
    new ScriptedTransport(connection),
    new RecordingIngestion(),
  );
  const running = client.connectOnce(new AbortController().signal);
  while (client.health().status !== "ready") await Promise.resolve();
  await client.sendMessage("#ALPHA", "announcement");
  await assertRejects(
    () => client.sendMessage("missing", "announcement"),
    TypeError,
    "not joined",
  );
  assertEquals(connection.writes.at(-1), "PRIVMSG #alpha :announcement");
  blocked.resolve(null);
  await assertRejects(() => running, TypeError, "connection closed");
});

Deno.test("Twitch IRC run loop reconnects after a disconnect", async () => {
  const first = new ScriptedConnection([null]);
  const blocked = Promise.withResolvers<string | null>();
  const second = new ScriptedConnection([]);
  second.readLine = () => blocked.promise;
  const transport = new ScriptedTransport([first, second]);
  const controller = new AbortController();
  const failures: string[] = [];
  const client = new TwitchIrcClient(
    {
      validateToken: () =>
        Promise.resolve({
          accessToken: "access-1",
          login: "qbot4k",
          clientId: "client-1",
          userId: "bot-1",
        }),
    } as TwitchTokenManager,
    transport,
    new RecordingIngestion(),
    1,
    {
      ready: () => Promise.resolve(),
      failed: (error) => {
        failures.push(error);
        return Promise.resolve();
      },
    },
  );
  const running = client.run(controller.signal);
  while (transport.connectCount < 2 || client.health().status !== "ready") {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  controller.abort();
  blocked.resolve(null);
  await running;
  assertEquals(transport.connectCount, 2);
  assertEquals(failures, ["Twitch IRC connection closed"]);
  assertEquals(client.health().status, "down");
});
