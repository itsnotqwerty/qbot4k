import type { DatabaseConnection } from "../data/database.ts";
import { PermanentJobError, type ProcessingJob } from "./jobs.ts";

export const STREAM_SESSION_JOB_TYPES = [
  "analyze.stream.started",
  "analyze.stream.updated",
  "analyze.stream.ended",
] as const;

export class PostgresStreamSessionRepository {
  constructor(private readonly connection: DatabaseConnection) {}

  async handle(job: ProcessingJob): Promise<void> {
    if (job.observationId === null) {
      throw new PermanentJobError("stream session job has no observation_id");
    }
    const observation = (await this.connection.query(
      `SELECT id, event_type, context_id, attributes_json, occurred_at
         FROM observations WHERE id=$1 AND community_id=$2`,
      [job.observationId, job.communityId],
    ))[0];
    if (!observation) {
      throw new PermanentJobError(
        `Observation ${job.observationId} does not exist`,
      );
    }
    const eventType = String(observation.event_type);
    const attributes = parseObject(observation.attributes_json);
    const streamKey = String(
      attributes.channel_name ?? observation.context_id ?? "",
    ).trim();
    if (!streamKey) {
      throw new PermanentJobError("stream observation has no channel");
    }
    const streamId = String(attributes.stream_id ?? "");
    const occurredAt = String(observation.occurred_at);
    if (eventType === "stream.ended") {
      await this.connection.query(
        `UPDATE stream_sessions SET status='ended', ended_at=$3,
           closing_observation_id=$1, updated_at=CURRENT_TIMESTAMP
         WHERE community_id=$2 AND stream_key=$4 AND status='live'`,
        [job.observationId, job.communityId, occurredAt, streamKey],
      );
      return;
    }
    await this.connection.query(
      `INSERT INTO stream_sessions(
         community_id, platform, stream_key, external_stream_id, title,
         category, status, started_at, opening_observation_id
       ) VALUES ($1,'twitch',$2,$3,$4,$5,'live',$6,$7)
       ON CONFLICT(community_id, platform, stream_key, started_at)
       DO UPDATE SET title=EXCLUDED.title, category=EXCLUDED.category,
         external_stream_id=EXCLUDED.external_stream_id,
         status='live', ended_at=NULL, updated_at=CURRENT_TIMESTAMP`,
      [
        job.communityId,
        streamKey,
        streamId || null,
        String(attributes.title ?? "") || null,
        String(attributes.game_name ?? "") || null,
        occurredAt,
        job.observationId,
      ],
    );
  }
}

function parseObject(value: unknown): Readonly<Record<string, unknown>> {
  const parsed = typeof value === "string" ? JSON.parse(value || "{}") : value;
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : {};
}
