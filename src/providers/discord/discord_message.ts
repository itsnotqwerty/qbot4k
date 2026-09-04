import { coerceTimestamp, NormalizedMessage } from "../../core/models.ts";

export class DiscordPayloadError extends TypeError {}

export function normalizeDiscordMessage(
  payload: Readonly<Record<string, unknown>>,
): NormalizedMessage {
  const author = record(payload.author);
  if (!author) {
    throw new DiscordPayloadError("Discord payload requires an author object");
  }
  const authorId = text(author.id);
  const username = text(author.username) || text(author.global_name);
  const channelId = text(payload.channel_id);
  if (!authorId) {
    throw new DiscordPayloadError("Discord payload requires author.id");
  }
  if (!username) {
    throw new DiscordPayloadError("Discord payload requires author.username");
  }
  if (!channelId) {
    throw new DiscordPayloadError("Discord payload requires channel_id");
  }

  const roleNames = strings(payload.role_names);
  const moderationRoles = new Set([
    "mod",
    "moderator",
    "admin",
    "administrator",
  ]);
  const isModerator = Boolean(payload.author_is_moderator) ||
    roleNames.some((role) => moderationRoles.has(role.toLocaleLowerCase()));
  const guildId = payload.guild_id === null || payload.guild_id === undefined
    ? null
    : String(payload.guild_id);
  const interaction = interactionDetails(payload);

  return new NormalizedMessage({
    platform: "discord",
    platformMessageId: payload.id === null || payload.id === undefined
      ? null
      : String(payload.id),
    platformUserId: authorId,
    username,
    channelId,
    guildOrChannelContext: guildId ?? channelId,
    contentRaw: payload.content === null || payload.content === undefined
      ? ""
      : String(payload.content),
    sentAt: coerceTimestamp(timestamp(payload.timestamp)),
    roleNames,
    isModerator,
    metadata: {
      guild_id: guildId,
      author_is_bot: Boolean(author.bot),
      mentioned_user_ids: objectStrings(payload.mentions, "id"),
      attachment_urls: objectStrings(payload.attachments, "url"),
      reply_to_author_is_bot: replyAuthorIsBot(payload),
      interaction_user_id: interaction.userId || null,
      interaction_username: interaction.username || null,
      interaction_command_name: interaction.commandName || null,
      embed_text: text(payload.embed_text) ||
        extractDiscordEmbedText(payload) ||
        null,
    },
  });
}

export function buildDiscordMessagePayload(
  payload: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const author = record(payload.author);
  if (!author) {
    throw new DiscordPayloadError("Discord payload requires an author object");
  }
  const member = record(payload.member);
  const interaction = interactionDetails(payload);
  const resolvedRoles = Array.isArray(payload.resolved_role_names)
    ? strings(payload.resolved_role_names)
    : strings(member?.roles);
  return Object.freeze({
    id: payload.id,
    timestamp: payload.timestamp,
    guild_id: payload.guild_id,
    channel_id: payload.channel_id,
    content: payload.content,
    author: Object.freeze({
      id: author.id,
      username: author.username,
      global_name: author.global_name,
      bot: Boolean(author.bot),
    }),
    mentions: objectStrings(payload.mentions, "id"),
    attachments: objectStrings(payload.attachments, "url"),
    reply_to_author_is_bot: replyAuthorIsBot(payload),
    interaction_user_id: interaction.userId,
    interaction_username: interaction.username,
    interaction_command_name: interaction.commandName,
    embed_text: extractDiscordEmbedText(payload),
    role_names: resolvedRoles,
    author_is_moderator: Boolean(payload.author_is_moderator),
  });
}

export function extractDiscordEmbedText(
  payload: Readonly<Record<string, unknown>>,
): string {
  if (!Array.isArray(payload.embeds)) return "";
  const parts: string[] = [];
  for (const value of payload.embeds) {
    const embed = record(value);
    if (!embed) continue;
    append(parts, embed.title, embed.description);
    if (Array.isArray(embed.fields)) {
      for (const fieldValue of embed.fields) {
        const field = record(fieldValue);
        if (field) append(parts, field.name, field.value);
      }
    }
    append(parts, record(embed.footer)?.text, record(embed.author)?.name);
  }
  return parts.join("\n");
}

function interactionDetails(payload: Readonly<Record<string, unknown>>): {
  userId: string;
  username: string;
  commandName: string;
} {
  let userId = text(payload.interaction_user_id);
  let username = text(payload.interaction_username);
  let commandName = text(payload.interaction_command_name);
  for (
    const source of [
      record(payload.interaction),
      record(payload.interaction_metadata),
    ]
  ) {
    if (!source) continue;
    const user = record(source.user);
    if (!userId && user) {
      userId = text(user.id);
      username = text(user.username) || text(user.global_name);
    }
    if (!commandName) commandName = text(source.name);
  }
  if (!userId) {
    const user = record(payload.interaction_user);
    if (user) {
      userId = text(user.id);
      username = text(user.username) || text(user.global_name);
    }
  }
  return { userId, username, commandName };
}

function replyAuthorIsBot(
  payload: Readonly<Record<string, unknown>>,
): boolean | null {
  const author = record(record(payload.referenced_message)?.author);
  return author && Object.hasOwn(author, "bot") ? Boolean(author.bot) : null;
}

function objectStrings(value: unknown, key: string): readonly string[] {
  if (!Array.isArray(value)) return Object.freeze([]);
  return Object.freeze(
    value.map((item) => {
      const source = record(item);
      return text(source ? source[key] : item);
    }).filter(Boolean),
  );
}

function strings(value: unknown): readonly string[] {
  return Array.isArray(value)
    ? Object.freeze(value.map((item) => text(item)).filter(Boolean))
    : Object.freeze([]);
}

function append(parts: string[], ...values: unknown[]): void {
  for (const value of values) {
    const normalized = text(value);
    if (normalized) parts.push(normalized);
  }
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
