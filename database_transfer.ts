import {
  exportSqliteDatabase,
  importPostgresDatabase,
} from "./src/ops/database_transfer.ts";

export type TransferArguments =
  | {
    readonly command: "export";
    readonly source: string;
    readonly output: string;
    readonly markSourceReadOnly: boolean;
  }
  | {
    readonly command: "import";
    readonly manifest: string;
    readonly databaseUrl: string;
    readonly replaceTarget: boolean;
  };

export function parseTransferArguments(
  args: readonly string[],
): TransferArguments {
  const [command, first, second, ...options] = args;
  if (command === "export") {
    if (!first || !second) {
      throw new TypeError("export requires source and output paths");
    }
    const unknown = options.filter((option) =>
      option !== "--mark-source-read-only"
    );
    if (unknown.length) throw new TypeError(`unknown argument: ${unknown[0]}`);
    return {
      command,
      source: first,
      output: second,
      markSourceReadOnly: options.includes("--mark-source-read-only"),
    };
  }
  if (command === "import") {
    if (!first || !second) {
      throw new TypeError("import requires manifest path and database URL");
    }
    const unknown = options.filter((option) => option !== "--replace-target");
    if (unknown.length) throw new TypeError(`unknown argument: ${unknown[0]}`);
    return {
      command,
      manifest: first,
      databaseUrl: second,
      replaceTarget: options.includes("--replace-target"),
    };
  }
  throw new TypeError("command must be export or import");
}

export async function main(args = Deno.args): Promise<number> {
  const parsed = parseTransferArguments(args);
  if (parsed.command === "export") {
    console.log(
      await exportSqliteDatabase(
        parsed.source,
        parsed.output,
        parsed.markSourceReadOnly,
      ),
    );
    return 0;
  }
  console.log(JSON.stringify(
    await importPostgresDatabase(
      parsed.manifest,
      parsed.databaseUrl,
      parsed.replaceTarget,
    ),
  ));
  return 0;
}

if (import.meta.main) Deno.exit(await main());
