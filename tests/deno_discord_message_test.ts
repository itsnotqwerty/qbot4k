import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  buildDiscordMessagePayload,
  DiscordPayloadError,
  normalizeDiscordMessage,
} from "../src/providers/discord/discord_message.ts";

const payload = {
  id: "message-1",
  timestamp: "2026-09-04T12:00:00Z",
  guild_id: "guild-1",
  channel_id: "channel-1",
  content: "  Hello   THERE  ",
  author: {
    id: "user-1",
    username: "Analyst",
    global_name: "Display",
    bot: false,
  },
  resolved_role_names: ["member", "Moderator"],
  mentions: [{ id: "user-2" }, { id: "" }],
  attachments: [{ url: "https://cdn.example/evidence.png" }],
  referenced_message: { author: { bot: true } },
  interaction_metadata: {
    user: { id: "user-3", global_name: "Commander" },
    name: "inspect",
  },
  embeds: [{
    title: "Alert",
    description: "Review required",
    fields: [{ name: "Severity", value: "High" }],
    footer: { text: "Automated" },
    author: { name: "QBot4K" },
  }],
};

Deno.test("Discord message payload preserves provider context", () => {
  assertEquals(buildDiscordMessagePayload(payload), {
    id: "message-1",
    timestamp: "2026-09-04T12:00:00Z",
    guild_id: "guild-1",
    channel_id: "channel-1",
    content: "  Hello   THERE  ",
    author: {
      id: "user-1",
      username: "Analyst",
      global_name: "Display",
      bot: false,
    },
    mentions: ["user-2"],
    attachments: ["https://cdn.example/evidence.png"],
    reply_to_author_is_bot: true,
    interaction_user_id: "user-3",
    interaction_username: "Commander",
    interaction_command_name: "inspect",
    embed_text: "Alert\nReview required\nSeverity\nHigh\nAutomated\nQBot4K",
    role_names: ["member", "Moderator"],
    author_is_moderator: false,
  });
});

Deno.test("Discord message normalization matches the canonical model", () => {
  const message = normalizeDiscordMessage({
    ...buildDiscordMessagePayload(payload),
    role_names: ["member", "Moderator"],
  });
  assertEquals(message.platform, "discord");
  assertEquals(message.platformMessageId, "message-1");
  assertEquals(message.platformUserId, "user-1");
  assertEquals(message.channelId, "channel-1");
  assertEquals(message.guildOrChannelContext, "guild-1");
  assertEquals(message.contentNormalized, "hello there");
  assertEquals(message.isModerator, true);
  assertEquals(message.metadata, {
    guild_id: "guild-1",
    author_is_bot: false,
    mentioned_user_ids: ["user-2"],
    attachment_urls: ["https://cdn.example/evidence.png"],
    reply_to_author_is_bot: null,
    interaction_user_id: "user-3",
    interaction_username: "Commander",
    interaction_command_name: "inspect",
    embed_text: "Alert\nReview required\nSeverity\nHigh\nAutomated\nQBot4K",
  });
});

Deno.test("Discord message normalization rejects missing ownership fields", () => {
  assertThrows(
    () => normalizeDiscordMessage({ channel_id: "channel-1" }),
    DiscordPayloadError,
    "author object",
  );
  assertThrows(
    () => normalizeDiscordMessage({ author: { id: "user-1" } }),
    DiscordPayloadError,
    "author.username",
  );
});
