import { TenantContext } from "./contexts.ts";
import { caseFold } from "unicode-case-folding";

export type ModelAttributes = Readonly<Record<string, unknown>>;

export function normalizeMessageContent(content: string): string {
  return caseFold(content.trim().split(/\s+/u).filter(Boolean).join(" "));
}

export function coerceTimestamp(value?: string | Date | null): string {
  if (
    value === undefined || value === null ||
    (typeof value === "string" && !value.trim())
  ) {
    return new Date().toISOString().replace("Z", "+00:00");
  }
  const timestamp = value instanceof Date ? value : new Date(value.trim());
  if (Number.isNaN(timestamp.valueOf())) {
    throw new TypeError("Invalid timestamp");
  }
  return timestamp.toISOString().replace("Z", "+00:00");
}

export interface NormalizedMessageInput {
  platform: string;
  platformUserId: string;
  username: string;
  channelId: string;
  contentRaw: string;
  sentAt: string;
  platformMessageId?: string | null;
  guildOrChannelContext?: string | null;
  roleNames?: readonly string[];
  isModerator?: boolean;
  metadata?: ModelAttributes;
}

export class NormalizedMessage {
  readonly platform: string;
  readonly platformUserId: string;
  readonly username: string;
  readonly channelId: string;
  readonly contentRaw: string;
  readonly sentAt: string;
  readonly platformMessageId: string | null;
  readonly guildOrChannelContext: string | null;
  readonly contentNormalized: string;
  readonly roleNames: readonly string[];
  readonly isModerator: boolean;
  readonly metadata: ModelAttributes;

  constructor(input: NormalizedMessageInput) {
    this.platform = input.platform;
    this.platformUserId = input.platformUserId;
    this.username = input.username;
    this.channelId = input.channelId;
    this.contentRaw = input.contentRaw;
    this.sentAt = input.sentAt;
    this.platformMessageId = input.platformMessageId ?? null;
    this.guildOrChannelContext = input.guildOrChannelContext ?? null;
    this.contentNormalized = normalizeMessageContent(input.contentRaw);
    this.roleNames = Object.freeze([...(input.roleNames ?? [])]);
    this.isModerator = input.isModerator ?? false;
    this.metadata = Object.freeze({ ...(input.metadata ?? {}) });
    Object.freeze(this);
  }
}

export function normalizedMessageFromObservation(
  row: Readonly<Record<string, unknown>>,
): NormalizedMessage {
  const eventType = String(row.event_type);
  if (eventType !== "message.created") {
    throw new TypeError(`Cannot construct a message from ${eventType}`);
  }

  const decoded = typeof row.attributes_json === "string"
    ? JSON.parse(row.attributes_json || "{}")
    : row.attributes_json ?? {};
  if (
    decoded === null || typeof decoded !== "object" || Array.isArray(decoded)
  ) {
    throw new TypeError("Observation attributes must be a JSON object");
  }

  const attributes = { ...decoded } as Record<string, unknown>;
  const rawRoleNames = attributes.role_names;
  const roleNames = Array.isArray(rawRoleNames) ? rawRoleNames.map(String) : [];
  const isModerator = pythonTruthiness(attributes.is_moderator);
  delete attributes.role_names;
  delete attributes.is_moderator;

  return new NormalizedMessage({
    platform: String(row.platform),
    platformMessageId: optionalString(row.external_event_id),
    platformUserId: String(row.actor_platform_user_id),
    username: String(row.actor_username),
    channelId: String(row.container_id),
    guildOrChannelContext: optionalString(row.context_id),
    contentRaw: row.text_raw === null || row.text_raw === undefined
      ? ""
      : String(row.text_raw),
    sentAt: String(row.occurred_at),
    roleNames,
    isModerator,
    metadata: attributes,
  });
}

function optionalString(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
}

function pythonTruthiness(value: unknown): boolean {
  if (
    value === null || value === undefined || value === false || value === 0 ||
    value === ""
  ) {
    return false;
  }
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

export interface Observation {
  readonly platform: string;
  readonly eventType: string;
  readonly occurredAt: string;
  readonly communityId: number;
  readonly installationId: number | null;
  readonly externalEventId: string | null;
  readonly actorPlatformUserId: string | null;
  readonly actorUsername: string | null;
  readonly targetPlatformUserId: string | null;
  readonly containerId: string | null;
  readonly contextId: string | null;
  readonly text: string | null;
  readonly attributes: ModelAttributes;
  readonly rawPayload: ModelAttributes;
  readonly schemaVersion: number;
}

export function observationFromMessage(
  message: NormalizedMessage,
): Observation {
  const tenant = TenantContext.require(message.metadata.community_id, {
    installationId: message.metadata.installation_id,
  });
  return Object.freeze({
    platform: message.platform,
    eventType: "message.created",
    occurredAt: message.sentAt,
    communityId: tenant.communityId,
    installationId: tenant.installationId,
    externalEventId: message.platformMessageId,
    actorPlatformUserId: message.platformUserId,
    actorUsername: message.username,
    targetPlatformUserId: null,
    containerId: message.channelId,
    contextId: message.guildOrChannelContext,
    text: message.contentRaw,
    attributes: Object.freeze({
      ...message.metadata,
      role_names: [...message.roleNames],
      is_moderator: message.isModerator,
    }),
    rawPayload: Object.freeze({ ...message.metadata }),
    schemaVersion: 1,
  });
}

export interface ObservationResult {
  readonly status: string;
  readonly observationId: number | null;
  readonly actorPlatformAccountId: number | null;
  readonly targetPlatformAccountId: number | null;
}

export interface IngestionResult {
  readonly status: string;
  readonly platform: string;
  readonly platformAccountId: number | null;
  readonly messageId: number | null;
  readonly reason: string | null;
}

export interface ConnectorHealth {
  readonly name: string;
  readonly status: string;
  readonly details: ModelAttributes;
}

export interface CollectedObservation {
  readonly observationId: number | null;
  readonly status: string;
  readonly analysisJobId: number | null;
}

export interface CollectionResult {
  readonly status: string;
  readonly platform: string;
  readonly observationId: number | null;
  readonly analysisJobId: number | null;
  readonly reason: string | null;
}

export interface ProcessingJob {
  readonly id: number;
  readonly communityId: number;
  readonly stage: string;
  readonly jobType: string;
  readonly observationId: number | null;
  readonly payload: ModelAttributes;
  readonly attempts: number;
  readonly maxAttempts: number;
}

export function processingJobFromRow(
  row: Readonly<Record<string, unknown>>,
): ProcessingJob {
  const decoded = typeof row.payload_json === "string"
    ? JSON.parse(row.payload_json)
    : row.payload_json;
  if (
    decoded === null || typeof decoded !== "object" || Array.isArray(decoded)
  ) {
    throw new TypeError("Processing job payload must be a JSON object");
  }
  const tenant = TenantContext.require(row.community_id);
  return Object.freeze({
    id: Number(row.id),
    communityId: tenant.communityId,
    stage: String(row.stage),
    jobType: String(row.job_type),
    observationId:
      row.observation_id === null || row.observation_id === undefined
        ? null
        : Number(row.observation_id),
    payload: Object.freeze({ ...decoded }),
    attempts: Number(row.attempts),
    maxAttempts: Number(row.max_attempts),
  });
}
