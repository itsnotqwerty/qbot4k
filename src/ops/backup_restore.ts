import { basename, dirname } from "@std/path";
import postgres from "postgres";
import { commandConnection, PostgresBackupService } from "./backup.ts";

export interface RestoreRehearsalReport {
  readonly archive: string;
  readonly sha256: string;
  readonly size_bytes: number;
  readonly schema_version: number;
  readonly tables: number;
  readonly rows: number;
  readonly constraints_validated: boolean;
  readonly rto_ms: number;
  readonly rpo_seconds: number;
}

export async function rehearsePostgresRestore(
  archive: string,
  sourceDatabaseUrl: string,
  targetDatabaseUrl: string,
  now = new Date(),
): Promise<RestoreRehearsalReport> {
  if (
    databaseIdentity(sourceDatabaseUrl) === databaseIdentity(targetDatabaseUrl)
  ) {
    throw new TypeError("restore target must differ from the source database");
  }
  const started = performance.now();
  const target = postgres(targetDatabaseUrl, { max: 1 });
  try {
    const [{ count: existingTables }] = await target`
      SELECT COUNT(*)::integer AS count FROM pg_tables WHERE schemaname='public'
    `;
    if (Number(existingTables) !== 0) {
      throw new TypeError("restore target must have an empty public schema");
    }

    const backup = new PostgresBackupService(
      sourceDatabaseUrl,
      dirname(archive),
      1,
    );
    const verified = await backup.verify(archive);
    const metadata = JSON.parse(await Deno.readTextFile(`${archive}.json`));
    const createdAt = new Date(String(metadata.created_at));
    if (Number.isNaN(createdAt.getTime()) || createdAt > now) {
      throw new Error("backup manifest created_at is invalid");
    }

    await runRestore(archive, targetDatabaseUrl);
    const [{ version }] = await target`
      SELECT COALESCE(MAX(version),0)::integer AS version FROM schema_migrations
    `;
    const tables = await target`
      SELECT tablename FROM pg_tables
       WHERE schemaname='public' ORDER BY tablename
    `;
    let rows = 0;
    for (const table of tables) {
      const [{ count }] = await target`
        SELECT COUNT(*)::integer AS count FROM ${
        target(String(table.tablename))
      }
      `;
      rows += Number(count);
    }
    const [{ count: invalidConstraints }] = await target`
      SELECT COUNT(*)::integer AS count FROM pg_constraint
       WHERE connamespace='public'::regnamespace AND NOT convalidated
    `;
    return Object.freeze({
      archive: basename(archive),
      sha256: verified.sha256,
      size_bytes: verified.sizeBytes,
      schema_version: Number(version),
      tables: tables.length,
      rows,
      constraints_validated: Number(invalidConstraints) === 0,
      rto_ms: Math.round(performance.now() - started),
      rpo_seconds: Math.floor((now.getTime() - createdAt.getTime()) / 1_000),
    });
  } finally {
    await target.end();
  }
}

function databaseIdentity(databaseUrl: string): string {
  const parsed = new URL(databaseUrl);
  if (parsed.protocol !== "postgres:" && parsed.protocol !== "postgresql:") {
    throw new TypeError("PostgreSQL database URLs are required");
  }
  const port = parsed.port || "5432";
  return `${parsed.hostname.toLowerCase()}:${port}${parsed.pathname}`;
}

async function runRestore(archive: string, databaseUrl: string): Promise<void> {
  const connection = commandConnection(databaseUrl);
  const output = await new Deno.Command("pg_restore", {
    args: [
      "--exit-on-error",
      "--no-owner",
      "--no-privileges",
      `--dbname=${connection.databaseUrl}`,
      archive,
    ],
    env: connection.env,
    stdout: "piped",
    stderr: "piped",
  }).output();
  if (output.success) return;
  const detail = new TextDecoder().decode(output.stderr).trim();
  throw new Error(`pg_restore failed${detail ? `: ${detail}` : ""}`);
}
