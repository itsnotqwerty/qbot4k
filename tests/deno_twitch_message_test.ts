import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  normalizeTwitchMessage,
  parseTwitchIrcMessage,
  TwitchPayloadError,
} from "../src/providers/twitch/twitch_message.ts";

const line = "@badges=moderator/1,subscriber/6;display-name=Analyst;id=message-1;mod=1;tmi-sent-ts=1788523200000;user-id=user-1 :analyst!analyst@analyst.tmi.twitch.tv PRIVMSG #channel :Hello there";

Deno.test("Twitch IRC PRIVMSG parses provider tags", () => {
  assertEquals(parseTwitchIrcMessage(line), {
    message_id: "message-1",
    timestamp: "2026-09-04T12:00:00.000Z",
    channel: "channel",
    content: "Hello there",
    user_id: "user-1",
    username: "Analyst",
    display_name: "Analyst",
    badges: ["moderator", "subscriber"],
    is_moderator: true,
  });
  assertEquals(parseTwitchIrcMessage("PING :tmi.twitch.tv"), null);
});

Deno.test("Twitch messages normalize to the canonical model", () => {
  const message = normalizeTwitchMessage(parseTwitchIrcMessage(line)!);
  assertEquals(message.platform, "twitch");
  assertEquals(message.platformMessageId, "message-1");
  assertEquals(message.platformUserId, "user-1");
  assertEquals(message.channelId, "channel");
  assertEquals(message.contentNormalized, "hello there");
  assertEquals(message.roleNames, ["moderator", "subscriber"]);
  assertEquals(message.isModerator, true);
});

Deno.test("Twitch normalization rejects incomplete messages", () => {
  assertThrows(
    () => normalizeTwitchMessage({ username: "Analyst", channel: "channel", content: "hello" }),
    TwitchPayloadError,
    "user_id",
  );
  assertThrows(
    () => normalizeTwitchMessage({ user_id: "user-1", username: "Analyst", channel: "channel" }),
    TwitchPayloadError,
    "content",
  );
});
