import { assert, assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { PostgresBackupService } from "../src/ops/backup.ts";
import { rehearsePostgresRestore } from "../src/ops/backup_restore.ts";
import { withOperationalService } from "../src/data/operations.ts";

const adminDatabaseUrl = Deno.env.get("QBOT_RESTORE_TEST_POSTGRES_URL");

Deno.test({
  name: "PostgreSQL backup restores into an empty disposable database",
  ignore: !adminDatabaseUrl,
  async fn() {
    const fixtureId = crypto.randomUUID().replaceAll("-", "");
    const sourceName = `qbot_restore_source_${fixtureId}`;
    const targetName = `qbot_restore_target_${fixtureId}`;
    const sourceUrl = databaseUrl(adminDatabaseUrl!, sourceName);
    const targetUrl = databaseUrl(adminDatabaseUrl!, targetName);
    const admin = postgres(adminDatabaseUrl!, { max: 1 });
    const directory = await Deno.makeTempDir();
    try {
      await admin`CREATE DATABASE ${admin(sourceName)}`;
      await admin`CREATE DATABASE ${admin(targetName)}`;
      const sourceOwner = postgres(sourceUrl, { max: 1 });
      try {
        await sourceOwner`ALTER SCHEMA public OWNER TO CURRENT_USER`;
      } finally {
        await sourceOwner.end();
      }
      await withOperationalService(sourceUrl, (service) => service.migrate());
      const source = postgres(sourceUrl, { max: 1 });
      try {
        await source`
          INSERT INTO organizations(name,slug)
          VALUES ('Restore rehearsal','restore-rehearsal')
        `;
      } finally {
        await source.end();
      }

      const createdAt = new Date("2026-09-04T12:00:00Z");
      const backup = await new PostgresBackupService(sourceUrl, directory, 1)
        .create(createdAt);
      await assertRejects(
        () => rehearsePostgresRestore(backup.backupPath, sourceUrl, sourceUrl),
        TypeError,
        "restore target must differ from the source database",
      );
      const report = await rehearsePostgresRestore(
        backup.backupPath,
        sourceUrl,
        targetUrl,
        new Date("2026-09-04T12:01:00Z"),
      );
      assert(report.schema_version > 0);
      assert(report.tables > 0);
      assert(report.rows > 0);
      assert(report.rto_ms >= 0);
      assertEquals(report.rpo_seconds, 60);
      assertEquals(report.constraints_validated, true);
      await assertRejects(
        () => rehearsePostgresRestore(backup.backupPath, sourceUrl, targetUrl),
        TypeError,
        "restore target must have an empty public schema",
      );

      const target = postgres(targetUrl, { max: 1 });
      try {
        const [organization] = await target`
          SELECT name,slug FROM organizations WHERE slug='restore-rehearsal'
        `;
        assertEquals(organization.name, "Restore rehearsal");
      } finally {
        await target.end();
      }
    } finally {
      for (const name of [sourceName, targetName]) {
        await admin`
          SELECT pg_terminate_backend(pid) FROM pg_stat_activity
           WHERE datname=${name} AND pid <> pg_backend_pid()
        `;
        await admin`DROP DATABASE IF EXISTS ${admin(name)}`;
      }
      await admin.end();
      await Deno.remove(directory, { recursive: true });
    }
  },
});

function databaseUrl(adminUrl: string, database: string): string {
  const parsed = new URL(adminUrl);
  parsed.pathname = `/${database}`;
  return parsed.toString();
}
