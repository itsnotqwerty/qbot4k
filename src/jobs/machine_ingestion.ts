import type { AppSettings } from "../core/config.ts";
import type { DatabaseConnection } from "../data/database.ts";
import type { CollectedObservation, Observation } from "../core/models.ts";
import { coerceTimestamp } from "../core/models.ts";
import {
  type ObservationCollector,
  TenantQuotaExceededError,
} from "../domain/observations.ts";
import { verifyEventsubSignature } from "../security/security.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import type { WebAuthController } from "../web/web_auth.ts";
import { roleAllows } from "../web/web_dashboard.ts";

const MAX_BODY_BYTES = 1_048_576;
const EVENT_TYPES = new Set([
  "message.edited",
  "message.deleted",
  "member.joined",
  "member.left",
  "reaction.added",
  "reaction.removed",
  "member.roles_changed",
  "moderation.action",
  "moderation.ban_added",
  "moderation.ban_removed",
  "stream.started",
  "stream.ended",
  "stream.updated",
  "account.updated",
  "channel.notice",
  "external.item",
  "channel.followed",
  "channel.subscribed",
  "channel.subscription_gifted",
  "channel.cheered",
  "channel.raided",
  "channel.reward_redeemed",
  "channel.warning",
  "channel.suspicious_user",
  "channel.shield_mode",
  "channel.shared_chat",
  "channel.ad_break",
  "channel.charity_donation",
]);
const EVENTSUB_TYPES: Readonly<Record<string, string>> = {
  "channel.chat.message": "message.created",
  "channel.chat.message_delete": "message.deleted",
  "channel.ban": "moderation.ban_added",
  "channel.unban": "moderation.ban_removed",
  "channel.moderate": "moderation.action",
  "stream.online": "stream.started",
  "stream.offline": "stream.ended",
  "channel.update": "stream.updated",
  "channel.follow": "channel.followed",
  "channel.subscribe": "channel.subscribed",
  "channel.subscription.gift": "channel.subscription_gifted",
  "channel.cheer": "channel.cheered",
  "channel.raid": "channel.raided",
  "channel.channel_points_custom_reward_redemption.add":
    "channel.reward_redeemed",
  "channel.warning.send": "channel.warning",
  "channel.warning.acknowledge": "channel.warning",
  "channel.suspicious_user.message": "channel.suspicious_user",
  "channel.suspicious_user.update": "channel.suspicious_user",
  "channel.shield_mode.begin": "channel.shield_mode",
  "channel.shield_mode.end": "channel.shield_mode",
  "channel.shared_chat.begin": "channel.shared_chat",
  "channel.shared_chat.update": "channel.shared_chat",
  "channel.shared_chat.end": "channel.shared_chat",
  "channel.ad_break.begin": "channel.ad_break",
  "channel.charity_campaign.donate": "channel.charity_donation",
};

export interface IngestionInstallation {
  readonly communityId: number;
  readonly installationId: number;
}

export interface MachineIngestionService {
  authorizeApiClient(
    plaintextKey: string,
    communityId: number,
  ): Promise<boolean>;
  resolveTwitchInstallation(
    broadcasterId: string,
  ): Promise<IngestionInstallation | null>;
  recordSubscription(
    communityId: number,
    subscription: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  markSubscription(subscriptionId: string, status?: string): Promise<void>;
  upsertExternalSource(input: {
    sourceKey: string;
    displayName: string;
    sourceType: string;
    trustWeight: number;
    occurredAt: string;
  }): Promise<void>;
}

export class PostgresMachineIngestionRepository
  implements MachineIngestionService {
  constructor(private readonly connection: DatabaseConnection) {}

  async authorizeApiClient(
    plaintextKey: string,
    communityId: number,
  ): Promise<boolean> {
    const keyHash = await sha256(plaintextKey);
    return await this.connection.transaction(async (connection) => {
      const client = (await connection.query(
        "SELECT id,scopes_json,rate_limit_per_minute FROM api_clients WHERE key_hash=$1 AND community_id=$2 AND status='active' FOR UPDATE",
        [keyHash, communityId],
      ))[0];
      if (!client) return false;
      const scopes = decodeArray(client.scopes_json);
      if (!scopes.includes("events.write") && !scopes.includes("*")) {
        return false;
      }
      const minuteBucket = new Date().toISOString().slice(0, 16) + ":00.000Z";
      const usage = Number(
        (await connection.query(
          `INSERT INTO api_request_usage(api_client_id,minute_bucket,request_count)
         VALUES ($1,$2,1)
         ON CONFLICT(api_client_id,minute_bucket)
         DO UPDATE SET request_count=api_request_usage.request_count+1
         RETURNING request_count`,
          [Number(client.id), minuteBucket],
        ))[0]?.request_count ?? 0,
      );
      return usage <= Number(client.rate_limit_per_minute);
    });
  }

  async resolveTwitchInstallation(
    broadcasterId: string,
  ): Promise<IngestionInstallation | null> {
    const row = (await this.connection.query(
      `SELECT id,community_id FROM community_installations
       WHERE platform='twitch' AND status='active'
         AND capabilities_json::jsonb ? 'events.ingest'
         AND (external_community_id=$1
           OR LOWER(metadata_json::jsonb->>'broadcaster_login')=LOWER($1)
           OR metadata_json::jsonb->>'broadcaster_id'=$1)
       ORDER BY id LIMIT 1`,
      [broadcasterId.trim()],
    ))[0];
    return row
      ? {
        communityId: Number(row.community_id),
        installationId: Number(row.id),
      }
      : null;
  }

  async recordSubscription(
    communityId: number,
    subscription: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const transport = objectValue(subscription.transport);
    await this.connection.query(
      `INSERT INTO twitch_eventsub_subscriptions(
         subscription_id,community_id,subscription_type,subscription_version,
         condition_json,transport_method,status,callback_url,cost
       ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       ON CONFLICT(subscription_id) DO UPDATE SET
         status=EXCLUDED.status,condition_json=EXCLUDED.condition_json,
         callback_url=EXCLUDED.callback_url,cost=EXCLUDED.cost,
         updated_at=CURRENT_TIMESTAMP`,
      [
        String(subscription.id ?? ""),
        communityId,
        String(subscription.type ?? "unknown"),
        String(subscription.version ?? "1"),
        JSON.stringify(objectValue(subscription.condition)),
        String(transport.method ?? "webhook"),
        String(subscription.status ?? "unknown"),
        String(transport.callback ?? "") || null,
        Number(subscription.cost ?? 0),
      ],
    );
  }

  async markSubscription(
    subscriptionId: string,
    status?: string,
  ): Promise<void> {
    await this.connection.query(
      "UPDATE twitch_eventsub_subscriptions SET last_event_at=CURRENT_TIMESTAMP,status=COALESCE($1,status),updated_at=CURRENT_TIMESTAMP WHERE subscription_id=$2",
      [status ?? null, subscriptionId],
    );
  }

  async upsertExternalSource(input: {
    sourceKey: string;
    displayName: string;
    sourceType: string;
    trustWeight: number;
    occurredAt: string;
  }): Promise<void> {
    await this.connection.query(
      `INSERT INTO external_feed_sources(source_key,display_name,source_type,trust_weight,last_observed_at)
       VALUES ($1,$2,$3,$4,$5)
       ON CONFLICT(source_key) DO UPDATE SET display_name=EXCLUDED.display_name,
         source_type=EXCLUDED.source_type,trust_weight=EXCLUDED.trust_weight,
         last_observed_at=EXCLUDED.last_observed_at,updated_at=CURRENT_TIMESTAMP`,
      [
        input.sourceKey,
        input.displayName,
        input.sourceType,
        input.trustWeight,
        input.occurredAt,
      ],
    );
  }
}

export class MachineIngestionController {
  private readonly eventsubMessages = new Map<string, number>();

  constructor(
    private readonly auth: WebAuthController,
    private readonly service: MachineIngestionService,
    private readonly collector: ObservationCollector,
    private readonly settings: Pick<AppSettings, "twitchEventsubSecret">,
  ) {}

  async eventsub(request: Request): Promise<Response> {
    const secret = this.settings.twitchEventsubSecret;
    if (!secret) return jsonError("eventsub_not_configured", 503);
    const body = await boundedBody(request, true);
    if (body instanceof Response) return body;
    const messageId =
      request.headers.get("Twitch-Eventsub-Message-Id")?.trim() ?? "";
    const timestamp =
      request.headers.get("Twitch-Eventsub-Message-Timestamp")?.trim() ?? "";
    const signature =
      request.headers.get("Twitch-Eventsub-Message-Signature")?.trim() ?? "";
    if (
      !await verifyEventsubSignature(secret, {
        messageId,
        timestamp,
        body,
        signature,
      })
    ) {
      return jsonError("invalid_eventsub_signature", 403);
    }
    if (!this.claimEventsubMessage(messageId)) {
      return new Response(null, { status: 204 });
    }
    const payload = parseObject(body);
    if (payload instanceof Response) return payload;
    const subscription = objectValue(payload.subscription);
    const event = objectValue(payload.event);
    const condition = objectValue(subscription.condition);
    const broadcasterId = String(
      event.broadcaster_user_id ?? condition.broadcaster_user_id ??
        event.to_broadcaster_user_id ?? "",
    ).trim();
    if (!broadcasterId) return jsonError("missing_broadcaster_id", 400);
    const installation = await this.service.resolveTwitchInstallation(
      broadcasterId,
    );
    if (!installation) return jsonError("unknown_installation", 404);
    if (Object.keys(subscription).length) {
      await this.service.recordSubscription(
        installation.communityId,
        subscription,
      );
    }
    const messageType =
      request.headers.get("Twitch-Eventsub-Message-Type")?.trim() ||
      "notification";
    if (messageType === "webhook_callback_verification") {
      return new Response(String(payload.challenge ?? ""), {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    const subscriptionId = String(subscription.id ?? "");
    if (messageType === "revocation") {
      await this.service.markSubscription(
        subscriptionId,
        String(subscription.status ?? "revoked"),
      );
      return new Response(null, { status: 204 });
    }
    const observation = eventsubObservation(
      payload,
      messageId,
      installation,
    );
    if (!observation) {
      return Response.json({ status: "unsupported_event" }, { status: 202 });
    }
    try {
      const result = await this.collector.collect(observation);
      await this.service.markSubscription(subscriptionId);
      return Response.json({
        status: result.status,
        observation_id: result.observationId,
      }, { status: 202 });
    } catch (error) {
      return ingestionError(error);
    }
  }

  private claimEventsubMessage(messageId: string, now = Date.now()): boolean {
    const cutoff = now - 600_000;
    for (const [seenId, seenAt] of this.eventsubMessages) {
      if (seenAt >= cutoff) break;
      this.eventsubMessages.delete(seenId);
    }
    if (this.eventsubMessages.has(messageId)) return false;
    this.eventsubMessages.set(messageId, now);
    if (this.eventsubMessages.size > 4096) {
      this.eventsubMessages.delete(this.eventsubMessages.keys().next().value!);
    }
    return true;
  }

  async event(request: Request): Promise<Response> {
    const parsed = await this.authorizedPayload(request);
    if (parsed instanceof Response) return parsed;
    const eventType = String(parsed.payload.event_type ?? "").trim()
      .toLocaleLowerCase();
    if (!EVENT_TYPES.has(eventType)) {
      return jsonError("unsupported event_type", 400);
    }
    const platform = String(parsed.payload.platform ?? "").trim();
    if (!platform) return jsonError("platform is required", 400);
    try {
      const result = await this.collector.collect({
        platform,
        eventType,
        occurredAt: coerceTimestamp(stringOrNull(parsed.payload.occurred_at)),
        communityId: parsed.communityId,
        installationId: optionalPositiveInteger(parsed.payload.installation_id),
        externalEventId: stringOrNull(parsed.payload.external_event_id),
        actorPlatformUserId: stringOrNull(
          parsed.payload.actor_platform_user_id,
        ),
        actorUsername: stringOrNull(parsed.payload.actor_username),
        targetPlatformUserId: stringOrNull(
          parsed.payload.target_platform_user_id,
        ),
        containerId: stringOrNull(parsed.payload.container_id),
        contextId: stringOrNull(parsed.payload.context_id),
        text: parsed.payload.text === undefined
          ? null
          : String(parsed.payload.text),
        attributes: objectValue(parsed.payload.attributes),
        rawPayload: parsed.payload,
        schemaVersion: 1,
      });
      return collectionResponse(result);
    } catch (error) {
      return ingestionError(error);
    }
  }

  async external(request: Request): Promise<Response> {
    const parsed = await this.authorizedPayload(request);
    if (parsed instanceof Response) return parsed;
    const sourceKey = String(parsed.payload.source_key ?? "").trim()
      .toLocaleLowerCase();
    const externalEventId = String(parsed.payload.external_event_id ?? "")
      .trim();
    if (!sourceKey || !externalEventId) {
      return jsonError("source_key and external_event_id are required", 400);
    }
    try {
      const occurredAt = coerceTimestamp(
        stringOrNull(parsed.payload.occurred_at),
      );
      await this.service.upsertExternalSource({
        sourceKey,
        displayName: String(parsed.payload.display_name ?? sourceKey).trim(),
        sourceType: String(parsed.payload.source_type ?? "api").trim(),
        trustWeight: Math.max(
          0,
          Math.min(1, Number(parsed.payload.trust_weight ?? 0.5)),
        ),
        occurredAt,
      });
      const result = await this.collector.collect({
        platform: `external:${sourceKey}`,
        eventType: "external.item",
        occurredAt,
        communityId: parsed.communityId,
        installationId: null,
        externalEventId,
        actorPlatformUserId: stringOrNull(parsed.payload.actor_id),
        actorUsername: stringOrNull(parsed.payload.actor_id),
        targetPlatformUserId: null,
        containerId: null,
        contextId: stringOrNull(parsed.payload.context_id) ?? sourceKey,
        text: String(parsed.payload.text ?? ""),
        attributes: objectValue(parsed.payload.attributes),
        rawPayload: parsed.payload,
        schemaVersion: 1,
      });
      return collectionResponse(result);
    } catch (error) {
      return ingestionError(error);
    }
  }

  private async authorizedPayload(request: Request): Promise<
    | { communityId: number; payload: Readonly<Record<string, unknown>> }
    | Response
  > {
    const origin = request.headers.get("origin");
    if (origin && origin !== "null" && !isAllowedSameSiteOrigin(request)) {
      return jsonError("origin_mismatch", 403);
    }
    const body = await boundedBody(request);
    if (body instanceof Response) return body;
    const payload = parseObject(body);
    if (payload instanceof Response) return payload;
    const communityId = Number(payload.community_id);
    if (!Number.isSafeInteger(communityId) || communityId <= 0) {
      return jsonError("community_id is required", 400);
    }
    const authorization = request.headers.get("authorization")?.trim() ?? "";
    if (authorization.toLocaleLowerCase().startsWith("bearer ")) {
      const valid = await this.service.authorizeApiClient(
        authorization.slice(7).trim(),
        communityId,
      );
      return valid
        ? { communityId, payload }
        : jsonError("invalid_bearer_token", 401);
    }
    const session = await this.auth.authorizedSession(request);
    if (!session) return Response.redirect(new URL("/login", request.url), 302);
    if (
      session.communityId !== communityId ||
      !roleAllows(session.role, "events.write")
    ) {
      return new Response("Forbidden", { status: 403 });
    }
    return { communityId, payload };
  }
}

function eventsubObservation(
  payload: Readonly<Record<string, unknown>>,
  messageId: string,
  installation: IngestionInstallation,
): Observation | null {
  const subscription = objectValue(payload.subscription);
  const event = objectValue(payload.event);
  const eventType = EVENTSUB_TYPES[String(subscription.type ?? "").trim()];
  if (!eventType || !Object.keys(event).length) return null;
  const broadcasterId = String(
    event.broadcaster_user_id ?? event.to_broadcaster_user_id ??
      event.from_broadcaster_user_id ?? "",
  ).trim();
  const actorId = stringOrNull(
    event.chatter_user_id ?? event.user_id ?? event.moderator_user_id ??
      event.from_broadcaster_user_id,
  );
  const actorName = stringOrNull(
    event.chatter_user_name ?? event.user_name ?? event.moderator_user_name ??
      event.from_broadcaster_user_name ?? actorId,
  );
  const message = objectValue(event.message);
  return Object.freeze({
    platform: "twitch",
    eventType,
    occurredAt: coerceTimestamp(
      stringOrNull(event.sent_at ?? event.started_at ?? event.ended_at),
    ),
    communityId: installation.communityId,
    installationId: installation.installationId,
    externalEventId: String(event.message_id ?? event.id ?? messageId).trim(),
    actorPlatformUserId: actorId,
    actorUsername: actorName,
    targetPlatformUserId: stringOrNull(
      event.target_user_id ?? event.to_broadcaster_user_id,
    ),
    containerId: broadcasterId || null,
    contextId: broadcasterId || null,
    text: stringOrNull(message.text ?? event.reason ?? event.title),
    attributes: Object.freeze({
      eventsub_type: String(subscription.type ?? ""),
      ...event,
    }),
    rawPayload: payload,
    schemaVersion: 1,
  });
}

async function boundedBody(
  request: Request,
  eventsub = false,
): Promise<Uint8Array | Response> {
  const declared = request.headers.get("content-length");
  if (declared !== null && !/^\d+$/u.test(declared)) {
    return jsonError("invalid_content_length", 400);
  }
  if (declared === null || Number(declared) <= 0) {
    return jsonError(eventsub ? "invalid_body_length" : "missing_body", 400);
  }
  if (Number(declared) > MAX_BODY_BYTES) {
    return jsonError(
      eventsub ? "invalid_body_length" : "body_too_large",
      eventsub ? 400 : 413,
    );
  }
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.length === 0) return jsonError("missing_body", 400);
  if (body.length > MAX_BODY_BYTES) return jsonError("body_too_large", 413);
  return body;
}

function parseObject(
  body: Uint8Array,
): Readonly<Record<string, unknown>> | Response {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return jsonError("invalid_json", 400);
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Readonly<Record<string, unknown>>
    : jsonError("invalid_payload", 400);
}

function collectionResponse(result: CollectedObservation): Response {
  return Response.json({
    status: result.status,
    observation_id: result.observationId,
    analysis_job_id: result.analysisJobId,
  }, { status: 201 });
}

function ingestionError(error: unknown): Response {
  if (error instanceof TenantQuotaExceededError) {
    return Response.json({
      error: "tenant_quota_exceeded",
      quota_type: error.quotaType,
      retry_after_seconds: error.retryAfterSeconds,
    }, {
      status: 429,
      headers: { "retry-after": String(error.retryAfterSeconds) },
    });
  }
  return jsonError(error instanceof Error ? error.message : String(error), 400);
}

function jsonError(error: string, status: number): Response {
  return Response.json({ error }, { status });
}

function objectValue(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {};
}

function decodeArray(value: unknown): readonly string[] {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function stringOrNull(value: unknown): string | null {
  const normalized = String(value ?? "").trim();
  return normalized || null;
}

function optionalPositiveInteger(value: unknown): number | null {
  if (value === undefined || value === null || value === "") return null;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new TypeError("installation_id must be a positive integer");
  }
  return parsed;
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
