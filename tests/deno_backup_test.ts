import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import {
  type BackupCommandRunner,
  PostgresBackupService,
} from "../src/ops/backup.ts";

class FakeCommands implements BackupCommandRunner {
  readonly calls: Array<{
    command: string;
    args: readonly string[];
    env: Record<string, string>;
  }> = [];
  async run(
    command: string,
    args: readonly string[],
    env: Record<string, string>,
  ): Promise<void> {
    this.calls.push({ command, args, env });
    const output = args.find((argument) => argument.startsWith("--file="));
    if (output) await Deno.writeTextFile(output.slice(7), "verified backup");
  }
}

Deno.test("PostgreSQL backup verifies, publishes, manifests, and prunes", async () => {
  const directory = await Deno.makeTempDir();
  try {
    const commands = new FakeCommands();
    const service = new PostgresBackupService(
      "postgresql://localhost/qbot4k",
      directory,
      1,
      commands,
    );
    await service.create(new Date("2026-09-03T12:00:00Z"));
    const report = await service.create(new Date("2026-09-04T12:00:00Z"));
    assertEquals(commands.calls.map((call) => call.command), [
      "pg_dump",
      "pg_restore",
      "pg_dump",
      "pg_restore",
    ]);
    assertEquals((await Deno.stat(report.backupPath)).isFile, true);
    const metadata = JSON.parse(await Deno.readTextFile(report.metadataPath));
    assertEquals(metadata.sha256, report.sha256);
    assertEquals(metadata.size_bytes, 15);
    assertEquals(
      (await service.verify(report.backupPath)).sha256,
      report.sha256,
    );
    const files = [...Deno.readDirSync(directory)].map((entry) => entry.name)
      .sort();
    assertEquals(files, [
      "qbot4k-20260904T120000000Z.dump",
      "qbot4k-20260904T120000000Z.dump.json",
    ]);
  } finally {
    await Deno.remove(directory, { recursive: true });
  }
});

Deno.test("PostgreSQL backup verification rejects a modified archive", async () => {
  const directory = await Deno.makeTempDir();
  try {
    const service = new PostgresBackupService(
      "postgresql://localhost/qbot4k",
      directory,
      1,
      new FakeCommands(),
    );
    const report = await service.create(new Date("2026-09-04T12:00:00Z"));
    await Deno.writeTextFile(report.backupPath, "modified");
    await assertRejects(
      () => service.verify(report.backupPath),
      Error,
      "backup manifest size does not match",
    );
  } finally {
    await Deno.remove(directory, { recursive: true });
  }
});

Deno.test("PostgreSQL backup keeps credentials out of process arguments", async () => {
  const directory = await Deno.makeTempDir();
  try {
    const commands = new FakeCommands();
    await new PostgresBackupService(
      "postgresql://qbot:secret@db.example/qbot4k",
      directory,
      1,
      commands,
    ).create(new Date("2026-09-04T12:00:00Z"));
    const dump = commands.calls[0];
    assertEquals(
      dump.args.some((argument) => argument.includes("secret")),
      false,
    );
    assertEquals(dump.env, { PGPASSWORD: "secret" });
  } finally {
    await Deno.remove(directory, { recursive: true });
  }
});
