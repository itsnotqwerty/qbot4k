import { dirname, join } from "jsr:@std/path@1.1.2";
import type { DatabaseConnection } from "../data/database.ts";

export interface ArchiveFileStore {
  write(path: string, contents: string): Promise<void>;
}

export interface RawArchiveService {
  flush(archiveRoot: string, limit?: number): Promise<number>;
}

export class DenoArchiveFileStore implements ArchiveFileStore {
  async write(path: string, contents: string): Promise<void> {
    const directory = dirname(path);
    await Deno.mkdir(directory, { recursive: true });
    const temporaryPath = join(directory, `.raw-event-${crypto.randomUUID()}`);
    try {
      const file = await Deno.open(temporaryPath, {
        createNew: true,
        write: true,
        mode: 0o640,
      });
      try {
        await file.write(new TextEncoder().encode(contents));
        await file.sync();
      } finally {
        file.close();
      }
      await Deno.rename(temporaryPath, path);
    } finally {
      try {
        await Deno.remove(temporaryPath);
      } catch {
        // Atomic rename removes the temporary path on success.
      }
    }
  }
}

export class PostgresRawArchiveRepository implements RawArchiveService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly files: ArchiveFileStore = new DenoArchiveFileStore(),
  ) {}

  async flush(archiveRoot: string, limit = 1000): Promise<number> {
    const root = archiveRoot.trim();
    if (!root) throw new TypeError("archive_root is required");
    const boundedLimit = Math.max(1, Math.trunc(limit));
    return await this.connection.transaction(async (connection) => {
      const rows = await connection.query(
        `SELECT id,community_id,platform,event_type,payload_sha256,
                payload_json,received_at
           FROM raw_event_archive
          WHERE archive_path IS NULL
          ORDER BY id
          FOR UPDATE SKIP LOCKED
          LIMIT $1`,
        [boundedLimit],
      );
      let flushed = 0;
      for (const row of rows) {
        const archiveId = positiveInteger(row.id, "archive_id");
        const communityId = positiveInteger(row.community_id, "community_id");
        const receivedAt = String(row.received_at);
        const date = receivedAt.slice(0, 10).split("-");
        if (
          date.length !== 3 ||
          Number.isNaN(new Date(receivedAt.replace(" ", "T")).getTime())
        ) throw new TypeError("raw archive received_at is invalid");
        let payload: unknown;
        try {
          payload = JSON.parse(String(row.payload_json) || "{}");
        } catch {
          throw new TypeError("raw archive payload_json is invalid");
        }
        const payloadSha256 = String(row.payload_sha256);
        const destination = join(
          root,
          `community-${communityId}`,
          date[0],
          date[1],
          date[2],
          `${archiveId}-${payloadSha256.slice(0, 16)}.json`,
        );
        const envelope = canonicalJson({
          archive_id: archiveId,
          community_id: communityId,
          platform: String(row.platform),
          event_type: String(row.event_type),
          payload_sha256: payloadSha256,
          received_at: receivedAt,
          payload,
        });
        await this.files.write(destination, envelope);
        const updated = await connection.query(
          `UPDATE raw_event_archive SET archive_path=$1
            WHERE id=$2 AND archive_path IS NULL
          RETURNING id`,
          [destination, archiveId],
        );
        if (updated[0]) flushed += 1;
      }
      return flushed;
    });
  }
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(canonicalValue);
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalValue(item)]),
  );
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}
