import { basename, join } from "@std/path";

export interface BackupReport {
  readonly backupPath: string;
  readonly metadataPath: string;
  readonly sha256: string;
  readonly sizeBytes: number;
}

export interface BackupService {
  create(now: Date): Promise<BackupReport>;
  verify(backupPath: string, expectedSha256?: string): Promise<BackupReport>;
}

export interface BackupCommandRunner {
  run(
    command: string,
    args: readonly string[],
    env: Record<string, string>,
  ): Promise<void>;
}

export class DenoBackupCommandRunner implements BackupCommandRunner {
  async run(
    command: string,
    args: readonly string[],
    env: Record<string, string>,
  ): Promise<void> {
    const output = await new Deno.Command(command, {
      args: [...args],
      env,
      clearEnv: false,
      stdout: "piped",
      stderr: "piped",
    }).output();
    if (output.success) return;
    const detail = new TextDecoder().decode(output.stderr).trim();
    throw new Error(`${command} failed${detail ? `: ${detail}` : ""}`);
  }
}

export class PostgresBackupService implements BackupService {
  constructor(
    private readonly databaseUrl: string,
    private readonly backupDir: string,
    private readonly retentionCount: number,
    private readonly commands: BackupCommandRunner =
      new DenoBackupCommandRunner(),
  ) {
    if (!databaseUrl.startsWith("postgres")) {
      throw new TypeError("PostgreSQL database URL is required");
    }
    if (!Number.isSafeInteger(retentionCount) || retentionCount < 1) {
      throw new TypeError("backup retention count must be positive");
    }
  }

  async create(now: Date): Promise<BackupReport> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    await Deno.mkdir(this.backupDir, { recursive: true });
    const stamp = now.toISOString().replaceAll(/[-:.]/gu, "");
    const stem = `qbot4k-${stamp}`;
    const temporaryPath = join(this.backupDir, `.${stem}.dump.tmp`);
    const backupPath = join(this.backupDir, `${stem}.dump`);
    const metadataPath = `${backupPath}.json`;
    try {
      const connection = commandConnection(this.databaseUrl);
      await this.commands.run("pg_dump", [
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        `--dbname=${connection.databaseUrl}`,
        `--file=${temporaryPath}`,
      ], connection.env);
      await this.commands.run("pg_restore", ["--list", temporaryPath], {});
      const bytes = await Deno.readFile(temporaryPath);
      const sha256 = toHex(await crypto.subtle.digest("SHA-256", bytes));
      const metadata = {
        format: "postgresql-custom",
        version: 1,
        created_at: now.toISOString(),
        backup_file: basename(backupPath),
        sha256,
        size_bytes: bytes.byteLength,
        verified_with: "pg_restore --list",
      };
      await Deno.writeTextFile(
        `${metadataPath}.tmp`,
        `${JSON.stringify(metadata, null, 2)}\n`,
      );
      await Deno.rename(temporaryPath, backupPath);
      await Deno.rename(`${metadataPath}.tmp`, metadataPath);
      await this.prune();
      return Object.freeze({
        backupPath,
        metadataPath,
        sha256,
        sizeBytes: bytes.byteLength,
      });
    } catch (error) {
      await removeIfPresent(temporaryPath);
      await removeIfPresent(`${metadataPath}.tmp`);
      throw error;
    }
  }

  async verify(
    backupPath: string,
    expectedSha256?: string,
  ): Promise<BackupReport> {
    const metadataPath = `${backupPath}.json`;
    const bytes = await Deno.readFile(backupPath);
    const sha256 = toHex(await crypto.subtle.digest("SHA-256", bytes));
    let expected = expectedSha256;
    if (!expected) {
      const metadata = JSON.parse(await Deno.readTextFile(metadataPath));
      expected = String(metadata.sha256 ?? "");
      if (metadata.backup_file !== basename(backupPath)) {
        throw new Error("backup manifest file name does not match archive");
      }
      if (Number(metadata.size_bytes) !== bytes.byteLength) {
        throw new Error("backup manifest size does not match archive");
      }
    }
    if (!/^[0-9a-f]{64}$/u.test(expected) || sha256 !== expected) {
      throw new Error("backup SHA-256 verification failed");
    }
    await this.commands.run("pg_restore", ["--list", backupPath], {});
    return Object.freeze({
      backupPath,
      metadataPath,
      sha256,
      sizeBytes: bytes.byteLength,
    });
  }

  private async prune(): Promise<void> {
    const generations: string[] = [];
    for await (const entry of Deno.readDir(this.backupDir)) {
      if (entry.isFile && /^qbot4k-.*\.dump$/u.test(entry.name)) {
        generations.push(entry.name);
      }
    }
    generations.sort().reverse();
    for (const name of generations.slice(this.retentionCount)) {
      const path = join(this.backupDir, name);
      await removeIfPresent(path);
      await removeIfPresent(`${path}.json`);
    }
  }
}

async function removeIfPresent(path: string): Promise<void> {
  try {
    await Deno.remove(path);
  } catch (error) {
    if (!(error instanceof Deno.errors.NotFound)) throw error;
  }
}

function toHex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export function commandConnection(databaseUrl: string): {
  readonly databaseUrl: string;
  readonly env: Record<string, string>;
} {
  const parsed = new URL(databaseUrl);
  const password = decodeURIComponent(parsed.password);
  parsed.password = "";
  return {
    databaseUrl: parsed.toString(),
    env: password ? { PGPASSWORD: password } : {},
  };
}
