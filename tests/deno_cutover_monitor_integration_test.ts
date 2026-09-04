import { assertEquals } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { collectCutoverMonitor } from "../src/ops/cutover_monitor.ts";
import { collectCutoverPreflight } from "../src/ops/cutover_preflight.ts";
import { withOperationalService } from "../src/data/operations.ts";

const adminDatabaseUrl = Deno.env.get("QBOT_MONITOR_TEST_POSTGRES_URL");

Deno.test({
  name:
    "PostgreSQL cutover monitor passes healthy evidence and blocks breaches",
  ignore: !adminDatabaseUrl,
  async fn() {
    const databaseName = `qbot_monitor_${
      crypto.randomUUID().replaceAll("-", "")
    }`;
    const databaseUrl = withDatabase(adminDatabaseUrl!, databaseName);
    const admin = postgres(adminDatabaseUrl!, { max: 1 });
    const now = new Date("2026-09-04T12:00:00Z");
    try {
      await admin`CREATE DATABASE ${admin(databaseName)}`;
      const database = postgres(databaseUrl, { max: 1 });
      try {
        await database`ALTER SCHEMA public OWNER TO CURRENT_USER`;
      } finally {
        await database.end();
      }
      await withOperationalService(databaseUrl, (service) => service.migrate());

      const sql = postgres(databaseUrl, { max: 1 });
      try {
        const [organization] = await sql`
          INSERT INTO organizations(name,slug) VALUES ('Monitor','monitor') RETURNING id
        `;
        const [workspace] = await sql`
          INSERT INTO workspaces(organization_id,name,slug)
          VALUES (${organization.id},'Monitor','monitor') RETURNING id
        `;
        const [community] = await sql`
          INSERT INTO communities(workspace_id,name,slug,status)
          VALUES (${workspace.id},'Monitor','monitor','active') RETURNING id
        `;
        const communities = await sql`
          SELECT id FROM communities WHERE status='active' ORDER BY id
        `;
        for (const community of communities) {
          for (
            const metric of [
              "webhook_acceptance_ms",
              "event_to_alert_ms",
              "moderation_confirmation_ms",
              "queue_age_seconds",
              "connector_health_percent",
              "dashboard_availability_percent",
              "open_dead_letters",
              "backup_freshness_seconds",
            ]
          ) {
            await sql`
              INSERT INTO tenant_slo_samples(
                community_id,metric_name,value,target_value,status,observed_at
              ) VALUES (${community.id},${metric},100,1000,'met',${now.toISOString()})
            `;
          }
        }
        const healthy = await collectCutoverMonitor(databaseUrl, 15, now);
        assertEquals(healthy.status, "pass", JSON.stringify(healthy));
        await sql`
          INSERT INTO processing_job_ownership(job_type,owner_runtime)
          VALUES ('monitor.fixture','python')
        `;
        await sql`
          INSERT INTO processing_jobs(
            community_id,stage,job_type,idempotency_key,created_at,updated_at
          ) VALUES (
            ${community.id},'analysis','monitor.fixture','monitor-fixture',
            ${now.toISOString()},${now.toISOString()}
          )
        `;
        const [installation] = await sql`
          INSERT INTO community_installations(
            community_id,platform,external_community_id,display_name,status,health_status
          ) VALUES (${community.id},'discord','monitor-discord','Monitor','active','healthy')
          RETURNING id
        `;
        await sql`
          INSERT INTO installation_runtime_leases(installation_id,owner_runtime)
          VALUES (${installation.id},'python')
        `;
        const blockedOwnership = await collectCutoverPreflight(
          databaseUrl,
          false,
          15,
          now,
        );
        assertEquals(blockedOwnership.blockers, [
          "job_ownership",
          "provider_ownership",
        ]);
        await sql`
          UPDATE processing_job_ownership SET owner_runtime='deno'
           WHERE job_type='monitor.fixture'
        `;
        await sql`
          UPDATE installation_runtime_leases SET owner_runtime='deno',
            lease_holder='deno-monitor',lease_acquired_at=${now.toISOString()},
            lease_expires_at=${new Date(now.getTime() + 60_000).toISOString()},
            last_renewed_at=${now.toISOString()}
           WHERE installation_id=${installation.id}
        `;
        assertEquals(
          (await collectCutoverPreflight(databaseUrl, false, 15, now)).status,
          "pass",
        );
        await sql`
          UPDATE tenant_slo_samples SET value=1001,status='breached'
           WHERE metric_name='webhook_acceptance_ms'
        `;
        const blocked = await collectCutoverMonitor(databaseUrl, 15, now);
        assertEquals(blocked.status, "fail");
        assertEquals(blocked.blockers, ["tenant_slo", "webhook_acceptance"]);
      } finally {
        await sql.end();
      }
    } finally {
      await admin`
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
         WHERE datname=${databaseName} AND pid <> pg_backend_pid()
      `;
      await admin`DROP DATABASE IF EXISTS ${admin(databaseName)}`;
      await admin.end();
    }
  },
});

function withDatabase(adminUrl: string, database: string): string {
  const parsed = new URL(adminUrl);
  parsed.pathname = `/${database}`;
  return parsed.toString();
}
