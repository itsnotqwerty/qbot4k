import postgres from "postgres";

export interface CutoverSignals {
  readonly job_error_rate_percent: number;
  readonly queue_age_seconds: number;
  readonly unhealthy_providers: number;
  readonly active_communities: number;
  readonly missing_or_breached_slos: number;
  readonly webhook_acceptance_ms: number | null;
  readonly database_connection_percent: number;
}

export interface CutoverMonitorReport extends CutoverSignals {
  readonly status: "pass" | "fail";
  readonly blockers: readonly string[];
  readonly observed_at: string;
  readonly window_minutes: number;
}

export function evaluateCutoverSignals(
  signals: CutoverSignals,
  observedAt: Date,
  windowMinutes: number,
): CutoverMonitorReport {
  const blockers: string[] = [];
  if (signals.job_error_rate_percent > 1) blockers.push("job_error_rate");
  if (signals.queue_age_seconds > 900) blockers.push("queue_age");
  if (signals.unhealthy_providers > 0) blockers.push("provider_health");
  if (signals.missing_or_breached_slos > 0) blockers.push("tenant_slo");
  if (
    signals.active_communities > 0 && signals.webhook_acceptance_ms === null
  ) {
    blockers.push("webhook_acceptance");
  }
  if (
    signals.webhook_acceptance_ms !== null &&
    signals.webhook_acceptance_ms > 1_000
  ) {
    blockers.push("webhook_acceptance");
  }
  if (signals.database_connection_percent > 80) {
    blockers.push("database_saturation");
  }
  return Object.freeze({
    ...signals,
    status: blockers.length ? "fail" : "pass",
    blockers: Object.freeze([...new Set(blockers)]),
    observed_at: observedAt.toISOString(),
    window_minutes: windowMinutes,
  });
}

export async function collectCutoverMonitor(
  databaseUrl: string,
  windowMinutes = 15,
  now = new Date(),
): Promise<CutoverMonitorReport> {
  if (!Number.isSafeInteger(windowMinutes) || windowMinutes < 1) {
    throw new TypeError("window_minutes must be a positive integer");
  }
  const sql = postgres(databaseUrl, { max: 1 });
  try {
    const [jobs] = await sql`
      WITH outcomes AS (
        SELECT
        COALESCE(100.0*COUNT(*) FILTER (WHERE status='failed') /
          NULLIF(COUNT(*) FILTER (WHERE status IN ('completed','failed')),0),0)::real
          AS error_rate
        FROM processing_jobs
        WHERE updated_at::timestamptz >= ${now.toISOString()}::timestamptz
          - ${windowMinutes} * INTERVAL '1 minute'
      ), queue AS (
        SELECT COALESCE(MAX(GREATEST(0,EXTRACT(EPOCH FROM (
          ${now.toISOString()}::timestamptz-created_at::timestamptz)))),0)::real
          AS queue_age
        FROM processing_jobs WHERE status IN ('pending','retry')
      )
      SELECT outcomes.error_rate,queue.queue_age FROM outcomes CROSS JOIN queue
    `;
    const [providers] = await sql`
      SELECT COUNT(*)::integer AS count FROM community_installations
       WHERE status='active' AND health_status IS DISTINCT FROM 'healthy'
    `;
    const [communities] = await sql`
      SELECT COUNT(*)::integer AS count FROM communities WHERE status='active'
    `;
    const [slos] = await sql`
      WITH expected(metric_name) AS (VALUES
        ('webhook_acceptance_ms'),('event_to_alert_ms'),
        ('moderation_confirmation_ms'),('queue_age_seconds'),
        ('connector_health_percent'),('dashboard_availability_percent'),
        ('open_dead_letters'),('backup_freshness_seconds')
      ), latest AS (
        SELECT DISTINCT ON (community_id,metric_name)
               community_id,metric_name,value,status,observed_at
          FROM tenant_slo_samples
         ORDER BY community_id,metric_name,observed_at::timestamptz DESC
      ), active AS (
        SELECT id FROM communities WHERE status='active'
      )
      SELECT
        COUNT(*) FILTER (
          WHERE latest.status IS NULL OR latest.status <> 'met'
             OR latest.observed_at::timestamptz < ${now.toISOString()}::timestamptz
                - ${windowMinutes} * INTERVAL '1 minute'
        )::integer AS blocked,
        MAX(latest.value) FILTER (
          WHERE latest.metric_name='webhook_acceptance_ms'
        )::real AS webhook
      FROM active CROSS JOIN expected
      LEFT JOIN latest ON latest.community_id=active.id
        AND latest.metric_name=expected.metric_name
    `;
    const [database] = await sql`
      SELECT (100.0*COUNT(*)/
        NULLIF(current_setting('max_connections')::integer,0))::real AS percent
      FROM pg_stat_activity WHERE datname=current_database()
    `;
    return evaluateCutoverSignals(
      {
        job_error_rate_percent: Number(jobs.error_rate),
        queue_age_seconds: Number(jobs.queue_age),
        unhealthy_providers: Number(providers.count),
        active_communities: Number(communities.count),
        missing_or_breached_slos: Number(slos.blocked),
        webhook_acceptance_ms: slos.webhook === null
          ? null
          : Number(slos.webhook),
        database_connection_percent: Number(database.percent),
      },
      now,
      windowMinutes,
    );
  } finally {
    await sql.end();
  }
}
