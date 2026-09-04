import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  decodeDiscordGatewayFrame,
  decodeTwitchEventsubFrame,
} from "../src/providers/provider_protocol.ts";

Deno.test("recorded Discord Gateway lifecycle frames decode", () => {
  const frames = JSON.parse(Deno.readTextFileSync(
    "tests/fixtures/providers/discord_gateway.json",
  ));
  assertEquals(frames.map(decodeDiscordGatewayFrame), [
    { kind: "hello", heartbeatIntervalMs: 41_250 },
    {
      kind: "dispatch",
      eventName: "MESSAGE_CREATE",
      sequence: 42,
      data: { id: "event-1", guild_id: "guild-1" },
    },
    { kind: "heartbeat_ack" },
    { kind: "reconnect" },
    { kind: "invalid_session", resumable: true },
  ]);
});

Deno.test("recorded Twitch EventSub WebSocket lifecycle frames decode", () => {
  const frames = JSON.parse(Deno.readTextFileSync(
    "tests/fixtures/providers/twitch_eventsub_websocket.json",
  ));
  assertEquals(frames.map(decodeTwitchEventsubFrame), [
    { kind: "welcome", sessionId: "session-1", keepaliveTimeoutSeconds: 10 },
    { kind: "keepalive" },
    {
      kind: "reconnect",
      reconnectUrl: "wss://eventsub.wss.twitch.tv/ws?session=reconnect",
    },
    {
      kind: "notification",
      subscriptionType: "stream.online",
      payload: { broadcaster_user_id: "broadcaster-1" },
    },
    {
      kind: "revocation",
      subscriptionType: "stream.online",
      status: "authorization_revoked",
    },
  ]);
});

Deno.test("provider protocol decoders fail closed on unknown frames", () => {
  assertThrows(() => decodeDiscordGatewayFrame({ op: 99 }), TypeError);
  assertThrows(
    () =>
      decodeTwitchEventsubFrame({
        metadata: { message_type: "unknown" },
        payload: {},
      }),
    TypeError,
  );
});
