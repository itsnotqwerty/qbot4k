import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type { DiscordIngestionService } from "../src/providers/discord/discord_ingestion.ts";
import {
  DISCORD_GATEWAY_INTENTS,
  DiscordGatewayClient,
  type DiscordGatewaySocket,
  type DiscordGatewayTransport,
} from "../src/providers/discord/discord_gateway.ts";

class ScriptedSocket implements DiscordGatewaySocket {
  readonly sent: Array<Readonly<Record<string, unknown>>> = [];
  closed = false;
  constructor(private readonly frames: unknown[]) {}
  send(payload: Readonly<Record<string, unknown>>): void {
    this.sent.push(payload);
  }
  receive(signal: AbortSignal): Promise<unknown> {
    if (this.frames.length === 0) {
      return new Promise((_, reject) => {
        signal.addEventListener(
          "abort",
          () =>
            reject(signal.reason ?? new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    }
    const frame = this.frames.shift();
    return frame instanceof Error
      ? Promise.reject(frame)
      : Promise.resolve(frame);
  }
  close(): void {
    this.closed = true;
  }
}

class ScriptedTransport implements DiscordGatewayTransport {
  readonly connectedUrls: string[] = [];
  constructor(private readonly sockets: ScriptedSocket[]) {}
  gatewayUrl(_botToken: string): Promise<string> {
    return Promise.resolve("wss://gateway.discord.test");
  }
  connect(url: string): Promise<DiscordGatewaySocket> {
    this.connectedUrls.push(url);
    return Promise.resolve(this.sockets.shift()!);
  }
}

class RecordingIngestion implements DiscordIngestionService {
  readonly events: Array<{ name: string; data: unknown }> = [];
  ingest(name: string, data: unknown) {
    this.events.push({ name, data });
    return Promise.resolve(null);
  }
}

Deno.test("Discord gateway identifies, dispatches, reconnects, and resumes", async () => {
  const first = new ScriptedSocket([
    { op: 10, d: { heartbeat_interval: 60_000 } },
    {
      op: 0,
      s: 4,
      t: "READY",
      d: {
        session_id: "session-1",
        resume_gateway_url: "wss://resume.discord.test",
      },
    },
    {
      op: 0,
      s: 5,
      t: "MESSAGE_CREATE",
      d: { guild_id: "guild-1", id: "message-1" },
    },
    { op: 7, d: null },
  ]);
  const second = new ScriptedSocket([
    { op: 10, d: { heartbeat_interval: 60_000 } },
    { op: 9, d: false },
  ]);
  const transport = new ScriptedTransport([first, second]);
  const ingestion = new RecordingIngestion();
  const client = new DiscordGatewayClient(
    "Bot secret-token",
    transport,
    ingestion,
  );

  await assertRejects(
    () => client.connectOnce(new AbortController().signal),
    TypeError,
    "requested reconnect",
  );
  assertEquals(first.sent[0], {
    op: 2,
    d: {
      token: "secret-token",
      intents: DISCORD_GATEWAY_INTENTS,
      properties: { os: "linux", browser: "qbot4k", device: "qbot4k" },
    },
  });
  assertEquals(ingestion.events, [{
    name: "MESSAGE_CREATE",
    data: { guild_id: "guild-1", id: "message-1" },
  }]);
  assertEquals(client.health().status, "ready");

  await assertRejects(
    () => client.connectOnce(new AbortController().signal),
    TypeError,
    "invalidated",
  );
  assertEquals(second.sent[0], {
    op: 6,
    d: { token: "secret-token", session_id: "session-1", seq: 5 },
  });
  assertEquals(
    transport.connectedUrls[1],
    "wss://resume.discord.test/?v=10&encoding=json",
  );
  assertEquals(client.health().sessionId, null);
  assertEquals(client.health().sequence, null);
  assertEquals(first.closed, true);
  assertEquals(second.closed, true);
});

Deno.test("Discord gateway reconnects when heartbeat is not acknowledged", async () => {
  const socket = new ScriptedSocket([
    { op: 10, d: { heartbeat_interval: 0 } },
  ]);
  const client = new DiscordGatewayClient(
    "secret-token",
    new ScriptedTransport([socket]),
    new RecordingIngestion(),
  );
  await assertRejects(
    () => client.connectOnce(new AbortController().signal),
    TypeError,
    "not acknowledged",
  );
  assertEquals(socket.sent[1], { op: 1, d: null });
});
