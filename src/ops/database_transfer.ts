import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import postgres from "postgres";
import type { DatabaseParameter } from "../data/database.ts";
import { SCHEMA_SCOPE_INVENTORY } from "../data/schema_scope.ts";

export const DATABASE_TRANSFER_FORMAT_VERSION = 1;
const EXCLUDED_TABLE_PREFIXES = ["observation_fts", "sqlite_"] as const;
type TransferValue = null | string | number | bigint | Uint8Array;

export interface TransferOwnership {
  readonly scope: string;
  readonly checked_rows: number;
  readonly unowned_rows: number;
}

export interface TransferTable {
  readonly columns: readonly string[];
  readonly primary_key: readonly string[];
  readonly dependencies: readonly string[];
  readonly row_count: number;
  readonly sha256: string;
  readonly ownership: TransferOwnership;
}

export interface TransferManifest {
  readonly format_version: number;
  readonly schema_version: number;
  readonly source: string;
  readonly tables: Readonly<Record<string, TransferTable>>;
  readonly totals: { readonly tables: number; readonly rows: number };
  readonly orphan_check: {
    readonly count: number;
    readonly rows: readonly (readonly unknown[])[];
  };
}

export interface TransferResult {
  readonly schema_version: number;
  readonly tables: number;
  readonly rows: number;
  readonly constraints_validated: boolean;
}

export async function exportSqliteDatabase(
  sourcePath: string,
  outputDirectory: string,
  markSourceReadOnly = false,
): Promise<string> {
  const source = await Deno.realPath(sourcePath);
  await Deno.mkdir(outputDirectory, { recursive: true });
  const database = new DatabaseSync(source);
  const tables: Record<string, TransferTable> = {};
  let totalRows = 0;
  try {
    database.exec("PRAGMA wal_checkpoint(TRUNCATE)");
    database.exec("PRAGMA foreign_keys=ON");
    const schema = database.prepare(
      "SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations",
    ).get() as Record<string, unknown>;
    const orphanRows = rows(database, "PRAGMA foreign_key_check").map((row) =>
      Object.values(row)
    );
    const tableNames = rows(
      database,
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ).map((row) => String(row.name)).filter((table) =>
      !EXCLUDED_TABLE_PREFIXES.some((prefix) => table.startsWith(prefix))
    );
    for (const table of tableNames) {
      const metadata = tableMetadata(database, table);
      const dataPath = `${outputDirectory}/${table}.jsonl`;
      const output = await Deno.open(dataPath, {
        create: true,
        write: true,
        truncate: true,
      });
      const digest = createHash("sha256");
      let rowCount = 0;
      try {
        const selected = metadata.columns.map(quote).join(",");
        const ordered =
          (metadata.primaryKey.length ? metadata.primaryKey : metadata.columns)
            .map(quote).join(",");
        for (
          const row of rows(
            database,
            `SELECT ${selected} FROM ${quote(table)} ORDER BY ${ordered}`,
          )
        ) {
          const line = canonicalRow(
            metadata.columns.map((column) => row[column] as TransferValue),
          ) + "\n";
          const bytes = new TextEncoder().encode(line);
          await output.write(bytes);
          digest.update(bytes);
          rowCount += 1;
        }
      } finally {
        output.close();
      }
      totalRows += rowCount;
      tables[table] = Object.freeze({
        columns: Object.freeze(metadata.columns),
        primary_key: Object.freeze(metadata.primaryKey),
        dependencies: Object.freeze(metadata.dependencies),
        row_count: rowCount,
        sha256: digest.digest("hex"),
        ownership: ownershipCheck(database, table),
      });
    }
    const manifest: TransferManifest = Object.freeze({
      format_version: DATABASE_TRANSFER_FORMAT_VERSION,
      schema_version: Number(schema.version),
      source,
      tables: Object.freeze(tables),
      totals: Object.freeze({ tables: tableNames.length, rows: totalRows }),
      orphan_check: Object.freeze({
        count: orphanRows.length,
        rows: Object.freeze(orphanRows),
      }),
    });
    const manifestPath = `${outputDirectory}/manifest.json`;
    await Deno.writeTextFile(
      manifestPath,
      `${JSON.stringify(manifest, null, 2)}\n`,
    );
    if (markSourceReadOnly) {
      const mode = (await Deno.stat(source)).mode;
      if (mode !== null) await Deno.chmod(source, mode & ~0o222);
    }
    return manifestPath;
  } finally {
    database.close();
  }
}

export async function importPostgresDatabase(
  manifestPath: string,
  databaseUrl: string,
  replaceTarget = false,
): Promise<TransferResult> {
  if (!replaceTarget) {
    throw new TypeError(
      "PostgreSQL import requires explicit replace_target=true",
    );
  }
  const manifest = await loadManifest(manifestPath);
  const order = foreignKeyOrder(manifest.tables);
  const sql = postgres(databaseUrl, { max: 1, onnotice: () => undefined });
  try {
    const schemaSql = await Deno.readTextFile(
      new URL("../postgres_schema.sql", import.meta.url),
    );
    await sql.unsafe(schemaSql);
    return await sql.begin(async (transaction) => {
      if (order.length) {
        await transaction.unsafe(
          `TRUNCATE TABLE ${
            order.map(quote).join(",")
          } RESTART IDENTITY CASCADE`,
        );
      }
      for (const table of order) {
        const metadata = manifest.tables[table];
        const columnSql = metadata.columns.map(quote).join(",");
        for await (
          const batch of jsonLineBatches(
            new URL(`./${table}.jsonl`, toDirectoryUrl(manifestPath)),
          )
        ) {
          if (!batch.length) continue;
          const parameters = batch.flatMap((values) => values.map(decodeValue));
          const width = metadata.columns.length;
          const valueSql = batch.map((_, rowIndex) =>
            `(${
              metadata.columns.map((_, columnIndex) =>
                `$${rowIndex * width + columnIndex + 1}`
              ).join(",")
            })`
          ).join(",");
          await transaction.unsafe(
            `INSERT INTO ${quote(table)} (${columnSql}) VALUES ${valueSql}`,
            parameters,
          );
        }
      }
      const identities = await transaction.unsafe(
        `SELECT table_name,column_name FROM information_schema.columns
          WHERE table_schema='public' AND is_identity='YES' ORDER BY table_name`,
      );
      for (const identity of identities) {
        const table = String(identity.table_name);
        const column = String(identity.column_name);
        await transaction.unsafe(
          `SELECT setval(pg_get_serial_sequence($1,$2),
             COALESCE((SELECT MAX(${quote(column)}) FROM ${quote(table)}),1),
             EXISTS(SELECT 1 FROM ${quote(table)}))`,
          [table, column],
        );
      }
      return await verifyTarget(transaction, manifest);
    });
  } finally {
    await sql.end();
  }
}

export function canonicalRow(values: readonly TransferValue[]): string {
  const json = JSON.stringify(values.map(encodeValue));
  let ascii = "";
  for (let index = 0; index < json.length; index += 1) {
    const code = json.charCodeAt(index);
    ascii += code > 0x7f
      ? `\\u${code.toString(16).padStart(4, "0")}`
      : json[index];
  }
  return ascii;
}

function encodeValue(value: TransferValue): unknown {
  if (value instanceof Uint8Array) {
    return { $base64: Buffer.from(value).toString("base64") };
  }
  if (typeof value === "bigint") return Number(value);
  return value;
}

function decodeValue(value: unknown): DatabaseParameter {
  if (
    value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === 1 && "$base64" in value
  ) {
    return Buffer.from(
      String((value as { $base64: unknown }).$base64),
      "base64",
    );
  }
  if (
    value === null || typeof value === "string" ||
    typeof value === "number" || typeof value === "boolean"
  ) {
    return value;
  }
  throw new TypeError("database transfer row contains an unsupported value");
}

function rows(database: DatabaseSync, sql: string): Record<string, unknown>[] {
  return database.prepare(sql).all() as Record<string, unknown>[];
}

function quote(identifier: string): string {
  return `"${identifier.replaceAll('"', '""')}"`;
}

function tableMetadata(database: DatabaseSync, table: string): {
  columns: string[];
  primaryKey: string[];
  dependencies: string[];
} {
  const columns = rows(database, `PRAGMA table_info(${quote(table)})`);
  return {
    columns: columns.map((column) => String(column.name)),
    primaryKey: columns.filter((column) => Number(column.pk) > 0)
      .sort((left, right) => Number(left.pk) - Number(right.pk))
      .map((column) => String(column.name)),
    dependencies: [
      ...new Set(
        rows(database, `PRAGMA foreign_key_list(${quote(table)})`)
          .map((foreignKey) => String(foreignKey.table))
          .filter((dependency) => dependency !== table),
      ),
    ].sort(),
  };
}

function ownershipCheck(
  database: DatabaseSync,
  table: string,
): TransferOwnership {
  const rule = SCHEMA_SCOPE_INVENTORY[table];
  if (!rule) {
    return { scope: "unclassified", checked_rows: 0, unowned_rows: 0 };
  }
  if (!rule.ownerColumn || !rule.ownerTable) {
    return { scope: rule.scope, checked_rows: 0, unowned_rows: 0 };
  }
  const result = database.prepare(
    `SELECT COUNT(*) AS checked_rows,
       SUM(CASE WHEN child.${quote(rule.ownerColumn)} IS NOT NULL
                    AND owner.id IS NULL THEN 1 ELSE 0 END) AS unowned_rows
       FROM ${quote(table)} AS child
       LEFT JOIN ${quote(rule.ownerTable)} AS owner
         ON owner.id=child.${quote(rule.ownerColumn)}`,
  ).get() as Record<string, unknown>;
  return {
    scope: rule.scope,
    checked_rows: Number(result.checked_rows),
    unowned_rows: Number(result.unowned_rows ?? 0),
  };
}

async function loadManifest(path: string): Promise<TransferManifest> {
  const manifest = JSON.parse(
    await Deno.readTextFile(path),
  ) as TransferManifest;
  if (manifest.format_version !== DATABASE_TRANSFER_FORMAT_VERSION) {
    throw new TypeError("unsupported database transfer manifest version");
  }
  if (!manifest.tables || typeof manifest.tables !== "object") {
    throw new TypeError("database transfer manifest has no tables");
  }
  if (Number(manifest.orphan_check?.count ?? -1) !== 0) {
    throw new TypeError("source database contains foreign-key orphans");
  }
  const unowned = Object.entries(manifest.tables)
    .filter(([, metadata]) => metadata.ownership.unowned_rows !== 0)
    .map(([table]) => table).sort();
  if (unowned.length) {
    throw new TypeError(
      `source database contains unowned rows: ${unowned.join(", ")}`,
    );
  }
  const unclassified = Object.entries(manifest.tables)
    .filter(([, metadata]) => metadata.ownership.scope === "unclassified")
    .map(([table]) => table).sort();
  if (unclassified.length) {
    throw new TypeError(
      `source database contains unclassified tables: ${
        unclassified.join(", ")
      }`,
    );
  }
  return manifest;
}

export function foreignKeyOrder(
  tables: Readonly<Record<string, TransferTable>>,
): readonly string[] {
  const remaining = new Set(Object.keys(tables));
  const ordered: string[] = [];
  while (remaining.size) {
    const ready = [...remaining].filter((table) =>
      !tables[table].dependencies.some((dependency) =>
        remaining.has(dependency)
      )
    ).sort();
    if (!ready.length) {
      throw new TypeError(
        `cyclic transfer dependencies: ${[...remaining].sort().join(", ")}`,
      );
    }
    ordered.push(...ready);
    ready.forEach((table) => remaining.delete(table));
  }
  return Object.freeze(ordered);
}

async function verifyTarget(
  connection: postgres.TransactionSql,
  manifest: TransferManifest,
): Promise<TransferResult> {
  const mismatches: string[] = [];
  let totalRows = 0;
  for (const [table, metadata] of Object.entries(manifest.tables)) {
    const columnTypes = await connection.unsafe(
      `SELECT column_name,data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1`,
      [table],
    );
    const bigintColumns = new Set(
      columnTypes.filter((column) => column.data_type === "bigint")
        .map((column) => String(column.column_name)),
    );
    const selected = metadata.columns.map(quote).join(",");
    const ordered =
      (metadata.primary_key.length ? metadata.primary_key : metadata.columns)
        .map(quote).join(",");
    const targetRows = await connection.unsafe(
      `SELECT ${selected} FROM ${quote(table)} ORDER BY ${ordered}`,
    );
    const digest = createHash("sha256");
    for (const row of targetRows) {
      digest.update(
        `${
          canonicalRow(
            metadata.columns.map((column) => {
              const value = row[column] as TransferValue;
              return bigintColumns.has(column) && typeof value === "string"
                ? Number(value)
                : value;
            }),
          )
        }\n`,
      );
    }
    totalRows += targetRows.length;
    if (targetRows.length !== metadata.row_count) {
      mismatches.push(`${table}: row count`);
    }
    if (digest.digest("hex") !== metadata.sha256) {
      mismatches.push(`${table}: checksum`);
    }
    const rule = SCHEMA_SCOPE_INVENTORY[table];
    if (rule?.ownerColumn && rule.ownerTable) {
      const [ownership] = await connection.unsafe(
        `SELECT COUNT(*) AS unowned_rows FROM ${quote(table)} AS child
          LEFT JOIN ${quote(rule.ownerTable)} AS owner
            ON owner.id=child.${quote(rule.ownerColumn)}
         WHERE child.${
          quote(rule.ownerColumn)
        } IS NOT NULL AND owner.id IS NULL`,
      );
      if (Number(ownership.unowned_rows) !== 0) {
        mismatches.push(`${table}: tenant ownership`);
      }
    }
  }
  const [constraints] = await connection.unsafe(
    "SELECT COUNT(*) AS count FROM pg_constraint WHERE NOT convalidated",
  );
  if (Number(constraints.count) !== 0) {
    mismatches.push("unvalidated PostgreSQL constraints");
  }
  if (totalRows !== manifest.totals.rows) {
    mismatches.push("source/target row totals");
  }
  if (mismatches.length) {
    throw new TypeError(
      `database transfer verification failed: ${mismatches.join(", ")}`,
    );
  }
  return Object.freeze({
    schema_version: manifest.schema_version,
    tables: Object.keys(manifest.tables).length,
    rows: totalRows,
    constraints_validated: true,
  });
}

async function* jsonLineBatches(
  url: URL,
  batchSize = 500,
): AsyncGenerator<unknown[][]> {
  const file = await Deno.open(url, { read: true });
  let pending = "";
  let batch: unknown[][] = [];
  for await (
    const chunk of file.readable.pipeThrough(new TextDecoderStream())
  ) {
    pending += chunk;
    const lines = pending.split("\n");
    pending = lines.pop() ?? "";
    for (const line of lines) {
      if (!line) continue;
      batch.push(JSON.parse(line) as unknown[]);
      if (batch.length === batchSize) {
        yield batch;
        batch = [];
      }
    }
  }
  if (pending.trim()) batch.push(JSON.parse(pending) as unknown[]);
  if (batch.length) yield batch;
}

function toDirectoryUrl(path: string): URL {
  const url = new URL(path, `file://${Deno.cwd()}/`);
  return new URL("./", url);
}
