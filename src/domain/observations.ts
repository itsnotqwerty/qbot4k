import type { DatabaseConnection } from "../data/database.ts";
import type { CollectedObservation, Observation } from "../core/models.ts";
import { consumeTenantQuota } from "./quota.ts";
export { TenantQuotaExceededError } from "./quota.ts";

export interface ObservationCollector {
  collect(observation: Observation): Promise<CollectedObservation>;
}

export class PostgresObservationRepository implements ObservationCollector {
  constructor(private readonly connection: DatabaseConnection) {}

  async collect(observation: Observation): Promise<CollectedObservation> {
    validateObservation(observation);
    return await this.connection.transaction(async (connection) => {
      if (observation.installationId !== null) {
        const installation = (await connection.query(
          "SELECT 1 FROM community_installations WHERE id=$1 AND community_id=$2",
          [observation.installationId, observation.communityId],
        ))[0];
        if (!installation) {
          throw new TypeError(
            "observation installation is not owned by community",
          );
        }
      }

      await consumeTenantQuota(
        connection,
        observation.communityId,
        "ingestion",
      );
      const actorAccountId = observation.actorPlatformUserId
        ? await ensurePlatformAccount(
          connection,
          observation.platform,
          observation.actorPlatformUserId,
          observation.actorUsername ?? observation.actorPlatformUserId,
          observation.contextId,
        )
        : null;
      const targetAccountId = observation.targetPlatformUserId
        ? await ensurePlatformAccount(
          connection,
          observation.platform,
          observation.targetPlatformUserId,
          observation.targetPlatformUserId,
          observation.contextId,
        )
        : null;
      const payloadJson = canonicalJson(
        Object.keys(observation.rawPayload).length > 0
          ? observation.rawPayload
          : observation.attributes,
      );
      const inserted = (await connection.query(
        `INSERT INTO observations(
           platform,community_id,installation_id,event_type,external_event_id,
           actor_platform_account_id,target_platform_account_id,container_id,
           context_id,text_raw,attributes_json,raw_payload_json,occurred_at,schema_version
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
         ON CONFLICT(platform,event_type,external_event_id) DO NOTHING
         RETURNING id`,
        [
          observation.platform,
          observation.communityId,
          observation.installationId,
          observation.eventType,
          observation.externalEventId,
          actorAccountId,
          targetAccountId,
          observation.containerId,
          observation.contextId,
          observation.text,
          canonicalJson(observation.attributes),
          payloadJson,
          observation.occurredAt,
          observation.schemaVersion,
        ],
      ))[0];
      if (!inserted) {
        return Object.freeze({
          observationId: null,
          status: "duplicate",
          analysisJobId: null,
        });
      }

      const observationId = integer(inserted.id, "observation_id");
      const analysisJobId = await enqueueAnalysis(
        connection,
        observation.communityId,
        observationId,
        observation.eventType,
      );
      const payloadSha256 = await sha256(payloadJson);
      await connection.query(
        `INSERT INTO raw_event_archive(
           community_id,installation_id,observation_id,platform,event_type,
           external_event_id,payload_sha256,payload_json
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
         ON CONFLICT(community_id,platform,event_type,external_event_id,payload_sha256)
         DO NOTHING`,
        [
          observation.communityId,
          observation.installationId,
          observationId,
          observation.platform,
          observation.eventType,
          observation.externalEventId,
          payloadSha256,
          payloadJson,
        ],
      );
      return Object.freeze({
        observationId,
        status: "persisted",
        analysisJobId,
      });
    });
  }
}

async function ensurePlatformAccount(
  connection: DatabaseConnection,
  platform: string,
  platformUserId: string,
  username: string,
  contextId: string | null,
): Promise<number> {
  const row = (await connection.query(
    `INSERT INTO platform_accounts(
       platform,platform_user_id,username,guild_or_channel_context
     ) VALUES ($1,$2,$3,$4)
     ON CONFLICT(platform,platform_user_id) DO UPDATE SET
       username=EXCLUDED.username,
       guild_or_channel_context=COALESCE(
         EXCLUDED.guild_or_channel_context,
         platform_accounts.guild_or_channel_context
       ),updated_at=CURRENT_TIMESTAMP
     RETURNING id`,
    [platform, platformUserId, username, contextId],
  ))[0];
  if (!row) throw new TypeError("failed to resolve platform account");
  return integer(row.id, "platform_account_id");
}

async function enqueueAnalysis(
  connection: DatabaseConnection,
  communityId: number,
  observationId: number,
  eventType: string,
): Promise<number | null> {
  const inserted = (await connection.query(
    `INSERT INTO processing_jobs(
       community_id,stage,job_type,observation_id,idempotency_key
     ) VALUES ($1,'analysis',$2,$3,$4)
     ON CONFLICT(idempotency_key) DO NOTHING RETURNING id`,
    [
      communityId,
      `analyze.${eventType}`,
      observationId,
      `observation:${observationId}:${eventType}:v1`,
    ],
  ))[0];
  if (!inserted) return null;
  await consumeTenantQuota(connection, communityId, "jobs");
  return integer(inserted.id, "analysis_job_id");
}

function validateObservation(observation: Observation): void {
  if (
    !Number.isSafeInteger(observation.communityId) ||
    observation.communityId <= 0
  ) {
    throw new TypeError("observation community_id must be positive");
  }
  if (!observation.platform.trim() || !observation.eventType.trim()) {
    throw new TypeError("observation platform and event_type are required");
  }
  if (Number.isNaN(new Date(observation.occurredAt).valueOf())) {
    throw new TypeError("observation occurred_at is invalid");
  }
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value: unknown): unknown {
  if (
    value === null || typeof value === "string" || typeof value === "boolean"
  ) return value;
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : String(value);
  }
  if (value instanceof Date) return value.toString();
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalValue(item)]),
    );
  }
  return String(value);
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

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new TypeError(`${name} must be an integer`);
  }
  return parsed;
}
