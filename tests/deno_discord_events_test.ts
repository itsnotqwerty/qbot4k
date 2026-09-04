import { assertEquals } from "jsr:@std/assert@1.0.14";
import { observationFromDiscordEvent } from "../src/providers/discord/discord_events.ts";

Deno.test("Discord reactions map to deterministic scoped observations", async () => {
  const payload = {
    message_id: "m1",
    channel_id: "c1",
    guild_id: "g1",
    user_id: "actor-1",
    timestamp: "2026-09-04T12:00:00Z",
    emoji: { name: "eyes" },
  };
  const first = await observationFromDiscordEvent(
    "MESSAGE_REACTION_ADD",
    payload,
    { communityId: 4, installationId: 9 },
  );
  const second = await observationFromDiscordEvent(
    "MESSAGE_REACTION_ADD",
    payload,
    { communityId: 4, installationId: 9 },
  );
  assertEquals(first, second);
  assertEquals(first?.eventType, "reaction.added");
  assertEquals(first?.actorPlatformUserId, "actor-1");
  assertEquals(first?.targetPlatformUserId, null);
  assertEquals(first?.containerId, "c1");
  assertEquals(first?.contextId, "g1");
  assertEquals(first?.communityId, 4);
  assertEquals(first?.installationId, 9);
  assertEquals(
    first?.externalEventId?.startsWith("MESSAGE_REACTION_ADD:m1:c1:"),
    true,
  );
});

Deno.test("Discord member events attribute the member as target", async () => {
  const observation = await observationFromDiscordEvent(
    "GUILD_MEMBER_ADD",
    {
      guild_id: "guild-1",
      joined_at: "2026-09-04T12:00:00Z",
      user: { id: "member-1", username: "Member" },
    },
    { communityId: 2 },
  );
  assertEquals(observation?.eventType, "member.joined");
  assertEquals(observation?.actorPlatformUserId, null);
  assertEquals(observation?.targetPlatformUserId, "member-1");
  assertEquals(observation?.occurredAt, "2026-09-04T12:00:00.000+00:00");
});

Deno.test("Discord mapper ignores unsupported gateway events", async () => {
  assertEquals(
    await observationFromDiscordEvent("READY", {}, { communityId: 2 }),
    null,
  );
});
