import type { DatabaseConnection } from "../data/database.ts";
import type { ProcessingJobStore } from "./jobs.ts";
import type { RawArchiveService } from "../ops/raw_archive.ts";
import type { RetentionReport, RetentionService } from "../ops/retention.ts";
import type { CheckpointReminderService } from "../domain/onboarding_automation.ts";

export interface MetricsRollupService {
  refresh(now: Date): Promise<number>;
}

export class PostgresMetricsRollupRepository implements MetricsRollupService {
  constructor(private readonly connection: DatabaseConnection) {}

  async refresh(now: Date): Promise<number> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    const bucketStart = new Date(now);
    bucketStart.setUTCHours(0, 0, 0, 0);
    const rows = await this.connection.query(
      `INSERT INTO metrics_rollups(
         metric_name,bucket_start,bucket_size,dimension_json,value
       )
       SELECT metric_name,$1,'1d','{}',value
         FROM (VALUES
           ('messages_total',(SELECT COUNT(*)::double precision FROM messages)),
           ('open_reviews',(SELECT COUNT(*)::double precision FROM review_queue WHERE status='open')),
           ('pending_actions',(SELECT COUNT(*)::double precision FROM moderation_actions WHERE status='pending')),
           ('observations_total',(SELECT COUNT(*)::double precision FROM observations)),
           ('observations_24h',(SELECT COUNT(*)::double precision FROM observations
             WHERE occurred_at::timestamptz>=$2::timestamptz-INTERVAL '1 day'))
         ) AS rollups(metric_name,value)
       ON CONFLICT(metric_name,bucket_start,bucket_size,dimension_json)
       DO UPDATE SET value=excluded.value,created_at=CURRENT_TIMESTAMP
       RETURNING metric_name`,
      [bucketStart.toISOString(), now.toISOString()],
    );
    return rows.length;
  }
}

export interface MaintenanceReport extends RetentionReport {
  readonly recoveredProcessingJobs: number;
  readonly rawEventsArchived: number;
  readonly rollupRows: number;
  readonly checkpointRemindersQueued: number;
}

export interface MaintenanceRunner {
  run(
    now: Date,
    auditRetentionDays: number,
    archiveRoot: string,
  ): Promise<MaintenanceReport>;
}

export class MaintenanceOrchestrator implements MaintenanceRunner {
  constructor(
    private readonly jobs: ProcessingJobStore,
    private readonly retention: RetentionService,
    private readonly rawArchive: RawArchiveService,
    private readonly rollups: MetricsRollupService,
    private readonly reminders: CheckpointReminderService,
  ) {}

  async run(
    now: Date,
    auditRetentionDays: number,
    archiveRoot: string,
  ): Promise<MaintenanceReport> {
    const recoveredProcessingJobs = await this.jobs.recoverExpired();
    const rawEventsArchived = await this.rawArchive.flush(archiveRoot);
    const retained = await this.retention.purge(now, auditRetentionDays);
    const rollupRows = await this.rollups.refresh(now);
    const checkpointRemindersQueued = await this.reminders
      .queueCheckpointReminders(now);
    return Object.freeze({
      ...retained,
      recoveredProcessingJobs,
      rawEventsArchived,
      rollupRows,
      checkpointRemindersQueued,
    });
  }
}
