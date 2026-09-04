import { coerceTimestamp, NormalizedMessage } from "../../core/models.ts";

export class TwitchPayloadError extends TypeError {}

export function parseTwitchIrcMessage(
  rawLine: string,
): Readonly<Record<string, unknown>> | null {
  const line = rawLine.trim();
  if (!line || !line.includes(" PRIVMSG ")) return null;
  const tags: Record<string, string> = {};
  let remainder = line;
  if (remainder.startsWith("@")) {
    const separator = remainder.indexOf(" ");
    if (separator < 0) return null;
    for (const item of remainder.slice(1, separator).split(";")) {
      const equals = item.indexOf("=");
      if (equals > 0) tags[item.slice(0, equals)] = item.slice(equals + 1);
    }
    remainder = remainder.slice(separator + 1);
  }
  if (!remainder.startsWith(":")) return null;
  const marker = " PRIVMSG ";
  const command = remainder.indexOf(marker);
  const contentMarker = remainder.indexOf(" :", command + marker.length);
  if (command < 0 || contentMarker < 0) return null;
  const prefix = remainder.slice(1, command);
  const username = prefix.split("!", 1)[0].trim();
  const channel = remainder.slice(command + marker.length, contentMarker)
    .trim().replace(/^#/u, "");
  if (!username || !channel) return null;
  const badges = tags.badges?.split(",").map((badge) =>
    badge.split("/", 1)[0].trim()
  ).filter(Boolean) ?? [];
  const milliseconds = tags["tmi-sent-ts"];
  const timestamp = milliseconds && /^\d+$/u.test(milliseconds)
    ? new Date(Number(milliseconds)).toISOString()
    : null;
  return Object.freeze({
    message_id: tags.id || null,
    timestamp,
    channel,
    content: remainder.slice(contentMarker + 2),
    user_id: tags["user-id"] || "",
    username: tags["display-name"] || username,
    display_name: tags["display-name"] || username,
    badges: Object.freeze(badges),
    is_moderator: tags.mod === "1",
  });
}

export function normalizeTwitchMessage(
  payload: Readonly<Record<string, unknown>>,
): NormalizedMessage {
  const userId = text(payload.user_id);
  const username = text(payload.username) || text(payload.display_name);
  const channel = text(payload.channel) || text(payload.channel_id);
  const content = payload.content ?? payload.message ?? "";
  if (!userId) throw new TwitchPayloadError("Twitch payload requires user_id");
  if (!username) throw new TwitchPayloadError("Twitch payload requires username");
  if (!channel) throw new TwitchPayloadError("Twitch payload requires channel");
  if (!String(content).trim()) {
    throw new TwitchPayloadError("Twitch payload requires content");
  }
  const badges = Array.isArray(payload.badges)
    ? Object.freeze(payload.badges.map(text).filter(Boolean))
    : Object.freeze([]);
  const moderatorBadges = new Set(["moderator", "broadcaster", "vip"]);
  const isModerator = Boolean(payload.is_moderator) || badges.some((badge) =>
    moderatorBadges.has(badge.toLocaleLowerCase())
  );
  return new NormalizedMessage({
    platform: "twitch",
    platformMessageId: payload.message_id === null ||
        payload.message_id === undefined
      ? null
      : String(payload.message_id),
    platformUserId: userId,
    username,
    channelId: channel,
    guildOrChannelContext: channel,
    contentRaw: String(content),
    sentAt: coerceTimestamp(timestamp(payload.sent_at ?? payload.timestamp)),
    roleNames: badges,
    isModerator,
    metadata: { badges },
  });
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function timestamp(value: unknown): string | Date | null {
  return typeof value === "string" || value instanceof Date ? value : null;
}
