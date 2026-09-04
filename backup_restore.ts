import { rehearsePostgresRestore } from "./src/ops/backup_restore.ts";

export interface RestoreArguments {
  readonly archive: string;
}

export function parseRestoreArguments(
  args: readonly string[],
): RestoreArguments {
  const [archive, ...unknown] = args;
  if (!archive) throw new TypeError("restore rehearsal requires an archive");
  if (unknown.length) throw new TypeError(`unknown argument: ${unknown[0]}`);
  return { archive };
}

export async function main(args = Deno.args): Promise<number> {
  const parsed = parseRestoreArguments(args);
  const sourceDatabaseUrl = Deno.env.get("QBOT_DATABASE_URL");
  const targetDatabaseUrl = Deno.env.get("QBOT_RESTORE_DATABASE_URL");
  if (!sourceDatabaseUrl || !targetDatabaseUrl) {
    throw new TypeError(
      "QBOT_DATABASE_URL and QBOT_RESTORE_DATABASE_URL are required",
    );
  }
  console.log(JSON.stringify(
    await rehearsePostgresRestore(
      parsed.archive,
      sourceDatabaseUrl,
      targetDatabaseUrl,
    ),
  ));
  return 0;
}

if (import.meta.main) Deno.exit(await main());
