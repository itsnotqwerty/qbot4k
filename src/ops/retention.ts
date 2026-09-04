import type { DatabaseConnection } from "../data/database.ts";

export interface RetentionReport {
  readonly deletedMessages: number;
  readonly deletedObservations: number;
  readonly deletedAuditLogRows: number;
  readonly deletedSignalRuns: number;
  readonly deletedScoreRuns: number;
  readonly deletedProcessingJobs: number;
}

export interface RetentionService {
  purge(now: Date, auditRetentionDays: number): Promise<RetentionReport>;
}

export class PostgresRetentionRepository implements RetentionService {
  constructor(private readonly connection: DatabaseConnection) {}

  async purge(now: Date, auditRetentionDays: number): Promise<RetentionReport> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    if (!Number.isSafeInteger(auditRetentionDays) || auditRetentionDays < 1) {
      throw new TypeError("audit_retention_days must be a positive integer");
    }
    return await this.connection.transaction(async (connection) => {
      const policies = await connection.query(
        `SELECT community_id,message_retention_days
           FROM community_policy_settings
          ORDER BY community_id`,
      );
      let deletedMessages = 0;
      let deletedObservations = 0;
      for (const policy of policies) {
        const communityId = positiveInteger(
          policy.community_id,
          "community_id",
        );
        const retentionDays = positiveInteger(
          policy.message_retention_days,
          "message_retention_days",
        );
        const cutoff = cutoffTimestamp(now, retentionDays);
        deletedMessages += (await connection.query(
          `DELETE FROM messages
            WHERE community_id=$1 AND sent_at::timestamptz<$2::timestamptz
              AND NOT EXISTS (
                SELECT 1 FROM legal_holds
                 WHERE legal_holds.community_id=messages.community_id
                   AND legal_holds.status='active'
              )
          RETURNING id`,
          [communityId, cutoff],
        )).length;
        deletedObservations += (await connection.query(
          `DELETE FROM observations
            WHERE community_id=$1 AND occurred_at::timestamptz<$2::timestamptz
              AND NOT EXISTS (
                SELECT 1 FROM legal_holds
                 WHERE legal_holds.community_id=observations.community_id
                   AND legal_holds.status='active'
              )
              AND NOT EXISTS (
                SELECT 1 FROM messages
                 WHERE messages.observation_id=observations.id
              )
          RETURNING id`,
          [communityId, cutoff],
        )).length;
      }
      const auditCutoff = cutoffTimestamp(now, auditRetentionDays);
      const deletedAuditLogRows = (await connection.query(
        `DELETE FROM audit_log
          WHERE created_at::timestamptz<$1::timestamptz
        RETURNING id`,
        [auditCutoff],
      )).length;
      const deletedScoreRuns = (await connection.query(
        `DELETE FROM social_score_runs
          WHERE calculated_at::timestamptz<$1::timestamptz
            AND id NOT IN (
              SELECT MAX(id) FROM social_score_runs GROUP BY user_id
            )
        RETURNING id`,
        [auditCutoff],
      )).length;
      const deletedSignalRuns = (await connection.query(
        `DELETE FROM signal_calculation_runs
          WHERE calculated_at::timestamptz<$1::timestamptz
        RETURNING id`,
        [auditCutoff],
      )).length;
      const deletedProcessingJobs = (await connection.query(
        `DELETE FROM processing_jobs
          WHERE status IN ('completed','failed','cancelled')
            AND COALESCE(completed_at,updated_at,created_at)::timestamptz
                <$1::timestamptz
        RETURNING id`,
        [auditCutoff],
      )).length;
      return Object.freeze({
        deletedMessages,
        deletedObservations,
        deletedAuditLogRows,
        deletedSignalRuns,
        deletedScoreRuns,
        deletedProcessingJobs,
      });
    });
  }
}

function cutoffTimestamp(now: Date, retentionDays: number): string {
  return new Date(now.getTime() - retentionDays * 86_400_000).toISOString();
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}
