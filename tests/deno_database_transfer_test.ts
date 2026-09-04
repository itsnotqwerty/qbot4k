import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import { DatabaseSync } from "node:sqlite";
import postgres from "postgres";
import { parseTransferArguments } from "../database_transfer.ts";
import {
  canonicalRow,
  exportSqliteDatabase,
  foreignKeyOrder,
  importPostgresDatabase,
  type TransferManifest,
  type TransferTable,
} from "../src/ops/database_transfer.ts";
import { SCHEMA_SCOPE_INVENTORY } from "../src/data/schema_scope.ts";

Deno.test("database transfer CLI requires explicit destructive flags", () => {
  assertEquals(
    parseTransferArguments([
      "export",
      "source.sqlite3",
      "transfer",
      "--mark-source-read-only",
    ]),
    {
      command: "export",
      source: "source.sqlite3",
      output: "transfer",
      markSourceReadOnly: true,
    },
  );
  assertEquals(
    parseTransferArguments([
      "import",
      "transfer/manifest.json",
      "postgres://localhost/qbot4k",
      "--replace-target",
    ]),
    {
      command: "import",
      manifest: "transfer/manifest.json",
      databaseUrl: "postgres://localhost/qbot4k",
      replaceTarget: true,
    },
  );
});

Deno.test("database transfer classifies every PostgreSQL schema table", async () => {
  const schema = await Deno.readTextFile(
    new URL("../src/postgres_schema.sql", import.meta.url),
  );
  const tables = [...schema.matchAll(
    /CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)/g,
  )].map((match) => match[1]);
  assertEquals(
    tables.filter((table) => !(table in SCHEMA_SCOPE_INVENTORY)),
    [],
  );
});

Deno.test("database transfer canonical rows match Python JSONL encoding", () => {
  assertEquals(
    canonicalRow(["café 😀", new Uint8Array([0, 1]), null]),
    '["caf\\u00e9 \\ud83d\\ude00",{"$base64":"AAE="},null]',
  );
});

Deno.test("database transfer orders dependencies and rejects cycles", () => {
  const table = (dependencies: readonly string[]): TransferTable => ({
    columns: ["id"],
    primary_key: ["id"],
    dependencies,
    row_count: 0,
    sha256: "",
    ownership: { scope: "global", checked_rows: 0, unowned_rows: 0 },
  });
  assertEquals(
    foreignKeyOrder({ child: table(["parent"]), parent: table([]) }),
    ["parent", "child"],
  );
  assertThrows(
    () =>
      foreignKeyOrder({
        left: table(["right"]),
        right: table(["left"]),
      }),
    TypeError,
    "cyclic transfer dependencies",
  );
});

Deno.test("Deno exports deterministic SQLite transfer artifacts", async () => {
  const directory = await Deno.makeTempDir();
  const source = `${directory}/source.sqlite3`;
  const output = `${directory}/export`;
  const database = new DatabaseSync(source);
  try {
    database.exec(`
      PRAGMA foreign_keys=ON;
      CREATE TABLE schema_migrations(
        version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL
      );
      CREATE TABLE organizations(
        id INTEGER PRIMARY KEY,name TEXT NOT NULL,slug TEXT NOT NULL UNIQUE
      );
      CREATE TABLE workspaces(
        id INTEGER PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES organizations(id),
        name TEXT NOT NULL,slug TEXT NOT NULL
      );
      INSERT INTO schema_migrations VALUES (28,'provider leases','2026-09-04T12:00:00Z');
      INSERT INTO organizations VALUES (7,'Café 😀','fixture');
      INSERT INTO workspaces VALUES (8,7,'Workspace','fixture');
    `);
  } finally {
    database.close();
  }
  try {
    const manifestPath = await exportSqliteDatabase(source, output);
    const manifest = JSON.parse(
      await Deno.readTextFile(manifestPath),
    ) as TransferManifest;
    assertEquals(manifest.format_version, 1);
    assertEquals(manifest.schema_version, 28);
    assertEquals(manifest.totals, { tables: 3, rows: 3 });
    assertEquals(manifest.orphan_check.count, 0);
    assertEquals(manifest.tables.workspaces.dependencies, ["organizations"]);
    assertEquals(manifest.tables.workspaces.ownership, {
      scope: "organization",
      checked_rows: 1,
      unowned_rows: 0,
    });
    assertEquals(
      await Deno.readTextFile(`${output}/organizations.jsonl`),
      '[7,"Caf\\u00e9 \\ud83d\\ude00","fixture"]\n',
    );
  } finally {
    await Deno.remove(directory, { recursive: true });
  }
});

const transferDatabaseUrl = Deno.env.get("QBOT_TRANSFER_TEST_POSTGRES_URL");

Deno.test({
  name: "Deno imports and verifies SQLite artifacts in PostgreSQL",
  ignore: !transferDatabaseUrl,
  async fn() {
    const directory = await Deno.makeTempDir();
    const source = `${directory}/source.sqlite3`;
    const output = `${directory}/export`;
    const database = new DatabaseSync(source);
    try {
      database.exec(`
        PRAGMA foreign_keys=ON;
        CREATE TABLE schema_migrations(
          version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL
        );
        CREATE TABLE organizations(
          id INTEGER PRIMARY KEY,name TEXT NOT NULL,slug TEXT NOT NULL UNIQUE
        );
        CREATE TABLE workspaces(
          id INTEGER PRIMARY KEY,
          organization_id INTEGER NOT NULL REFERENCES organizations(id),
          name TEXT NOT NULL,slug TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (28,'provider leases','2026-09-04T12:00:00Z');
        INSERT INTO organizations VALUES (7,'Transfer fixture','fixture');
        INSERT INTO workspaces VALUES (8,7,'Workspace','fixture');
      `);
    } finally {
      database.close();
    }
    try {
      const manifestPath = await exportSqliteDatabase(source, output);
      assertEquals(
        await importPostgresDatabase(
          manifestPath,
          transferDatabaseUrl!,
          true,
        ),
        {
          schema_version: 28,
          tables: 3,
          rows: 3,
          constraints_validated: true,
        },
      );
      const sql = postgres(transferDatabaseUrl!, { max: 1 });
      try {
        const [workspace] = await sql`
          SELECT organization_id,name FROM workspaces WHERE id=8
        `;
        assertEquals(workspace, {
          organization_id: 7,
          name: "Workspace",
        });
      } finally {
        await sql.end();
      }
    } finally {
      await Deno.remove(directory, { recursive: true });
    }
  },
});
