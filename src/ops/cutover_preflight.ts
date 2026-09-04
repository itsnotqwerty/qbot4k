import postgres from "postgres";
import {
  collectCutoverMonitor,
  type CutoverMonitorReport,
} from "./cutover_monitor.ts";

export interface CutoverOwnership {
  readonly non_deno_job_types: readonly string[];
  readonly non_deno_installations: readonly number[];
  readonly inactive_provider_leases: readonly number[];
}

export interface CutoverPreflightReport extends CutoverOwnership {
  readonly status: "pass" | "fail";
  readonly blockers: readonly string[];
  readonly web_read_only: boolean;
  readonly monitor: CutoverMonitorReport;
}

export function evaluateCutoverPreflight(
  ownership: CutoverOwnership,
  webReadOnly: boolean,
  monitor: CutoverMonitorReport,
): CutoverPreflightReport {
  const blockers = [...monitor.blockers];
  if (ownership.non_deno_job_types.length) blockers.push("job_ownership");
  if (ownership.non_deno_installations.length) {
    blockers.push("provider_ownership");
  }
  if (ownership.inactive_provider_leases.length) {
    blockers.push("provider_lease");
  }
  if (webReadOnly) blockers.push("web_read_only");
  return Object.freeze({
    ...ownership,
    status: blockers.length ? "fail" : "pass",
    blockers: Object.freeze([...new Set(blockers)]),
    web_read_only: webReadOnly,
    monitor,
  });
}

export async function collectCutoverPreflight(
  databaseUrl: string,
  webReadOnly: boolean,
  windowMinutes = 15,
  now = new Date(),
): Promise<CutoverPreflightReport> {
  const sql = postgres(databaseUrl, { max: 1 });
  try {
    const jobRows = await sql`
      SELECT DISTINCT job.job_type
        FROM processing_jobs AS job
        LEFT JOIN processing_job_ownership AS ownership
          ON ownership.job_type=job.job_type
       WHERE ownership.owner_runtime IS DISTINCT FROM 'deno'
       ORDER BY job.job_type
    `;
    const installationRows = await sql`
      SELECT installation.id
        FROM community_installations AS installation
        LEFT JOIN installation_runtime_leases AS lease
          ON lease.installation_id=installation.id
       WHERE installation.status='active'
         AND lease.owner_runtime IS DISTINCT FROM 'deno'
       ORDER BY installation.id
    `;
    const leaseRows = await sql`
      SELECT installation.id
        FROM community_installations AS installation
        JOIN installation_runtime_leases AS lease
          ON lease.installation_id=installation.id
       WHERE installation.status='active' AND lease.owner_runtime='deno'
         AND (lease.lease_holder IS NULL
           OR lease.lease_expires_at::timestamptz<=${now.toISOString()}::timestamptz)
       ORDER BY installation.id
    `;
    const ownership = Object.freeze({
      non_deno_job_types: Object.freeze(
        jobRows.map((row) => String(row.job_type)),
      ),
      non_deno_installations: Object.freeze(
        installationRows.map((row) => Number(row.id)),
      ),
      inactive_provider_leases: Object.freeze(
        leaseRows.map((row) => Number(row.id)),
      ),
    });
    return evaluateCutoverPreflight(
      ownership,
      webReadOnly,
      await collectCutoverMonitor(databaseUrl, windowMinutes, now),
    );
  } finally {
    await sql.end();
  }
}
