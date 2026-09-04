import { coerceTimestamp, type Observation } from "../../core/models.ts";

const DISCORD_EVENT_TYPES = Object.freeze(
  {
    MESSAGE_UPDATE: "message.edited",
    MESSAGE_DELETE: "message.deleted",
    GUILD_MEMBER_ADD: "member.joined",
    GUILD_MEMBER_REMOVE: "member.left",
    MESSAGE_REACTION_ADD: "reaction.added",
    MESSAGE_REACTION_REMOVE: "reaction.removed",
    GUILD_MEMBER_UPDATE: "member.roles_changed",
    GUILD_BAN_ADD: "moderation.ban_added",
    GUILD_BAN_REMOVE: "moderation.ban_removed",
    USER_UPDATE: "account.updated",
  } as const,
);

export async function observationFromDiscordEvent(
  gatewayEvent: string,
  data: Readonly<Record<string, unknown>>,
  context: { communityId: number; installationId?: number | null },
): Promise<Observation | null> {
  const normalizedEvent = gatewayEvent.trim().toLocaleUpperCase();
  const eventType = DISCORD_EVENT_TYPES[
    normalizedEvent as keyof typeof DISCORD_EVENT_TYPES
  ];
  if (!eventType) return null;

  const user = record(data.user) ?? {};
  const author = record(data.author) ?? {};
  const memberUser = record(record(data.member)?.user) ?? {};
  let actorId = text(data.user_id) || text(author.id) || null;
  let actorUsername = text(author.username) || text(author.global_name) ||
    actorId;
  let targetId: string | null = null;
  if (
    [
      "member.joined",
      "member.left",
      "member.roles_changed",
      "moderation.ban_added",
      "moderation.ban_removed",
      "account.updated",
    ].includes(eventType)
  ) {
    targetId = text(user.id) || text(memberUser.id) || text(data.id) || null;
    if (eventType === "account.updated") {
      actorId = targetId;
      targetId = null;
      actorUsername = text(user.username) || text(data.username) || actorId;
    }
  }

  const payloadDigest = (await sha256(pythonJson(data))).slice(0, 16);
  const subjectId = text(data.id) || text(data.message_id) || targetId ||
    actorId || "";
  const containerId = text(data.channel_id);
  const guildId = text(data.guild_id);
  const externalEventId = [
    gatewayEvent,
    subjectId,
    containerId || guildId,
    payloadDigest,
  ].join(":");
  const occurred = eventType === "member.joined"
    ? timestamp(data.joined_at)
    : timestamp(data.timestamp);

  return Object.freeze({
    platform: "discord",
    eventType,
    externalEventId,
    actorPlatformUserId: actorId,
    actorUsername,
    targetPlatformUserId: targetId,
    containerId: containerId || null,
    contextId: guildId || containerId || null,
    text: text(data.content) || null,
    occurredAt: coerceTimestamp(occurred),
    attributes: Object.freeze({
      gateway_event: gatewayEvent,
      ...data,
    }),
    rawPayload: Object.freeze({ ...data }),
    schemaVersion: 1,
    communityId: context.communityId,
    installationId: context.installationId ?? null,
  });
}

function pythonJson(value: unknown): string {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return asciiJsonString(value);
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(", ")}]`;
  const source = record(value);
  if (source) {
    return `{${
      Object.keys(source).sort().map((key) =>
        `${asciiJsonString(key)}: ${pythonJson(source[key])}`
      ).join(", ")
    }}`;
  }
  return asciiJsonString(String(value));
}

function asciiJsonString(value: string): string {
  return JSON.stringify(value).replace(
    /[\u0080-\uffff]/gu,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function timestamp(value: unknown): string | Date | null {
  return typeof value === "string" || value instanceof Date ? value : null;
}
