import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  type ArchiveFileStore,
  PostgresRawArchiveRepository,
} from "../src/ops/raw_archive.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];

  constructor(private readonly responses: Array<readonly DatabaseRow[]>) {}

  query(
    sql: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.responses.shift() ?? []);
  }

  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

class FakeFiles implements ArchiveFileStore {
  readonly writes: Array<{ path: string; contents: string }> = [];

  write(path: string, contents: string): Promise<void> {
    this.writes.push({ path, contents });
    return Promise.resolve();
  }
}

Deno.test("raw archive flush writes canonical envelopes before marking rows", async () => {
  const connection = new FakeConnection([
    [{
      id: 12,
      community_id: 3,
      platform: "twitch",
      event_type: "stream.started",
      payload_sha256: "0123456789abcdef0123456789abcdef",
      payload_json: '{"z":2,"nested":{"b":2,"a":1}}',
      received_at: "2026-09-04T12:30:00+00:00",
    }],
    [{ id: 12 }],
  ]);
  const files = new FakeFiles();
  const flushed = await new PostgresRawArchiveRepository(
    connection,
    files,
  ).flush("/archive", 25);

  assertEquals(flushed, 1);
  assertEquals(connection.calls[0].parameters, [25]);
  assertEquals(
    connection.calls[0].sql.includes("FOR UPDATE SKIP LOCKED"),
    true,
  );
  assertEquals(files.writes, [{
    path: "/archive/community-3/2026/09/04/12-0123456789abcdef.json",
    contents:
      '{"archive_id":12,"community_id":3,"event_type":"stream.started","payload":{"nested":{"a":1,"b":2},"z":2},"payload_sha256":"0123456789abcdef0123456789abcdef","platform":"twitch","received_at":"2026-09-04T12:30:00+00:00"}',
  }]);
  assertEquals(connection.calls[1].parameters, [files.writes[0].path, 12]);
});

Deno.test("raw archive flush validates input and does not count lost updates", async () => {
  await assertRejects(
    () =>
      new PostgresRawArchiveRepository(
        new FakeConnection([]),
        new FakeFiles(),
      ).flush(" "),
    TypeError,
    "archive_root is required",
  );
  const connection = new FakeConnection([
    [{
      id: 1,
      community_id: 1,
      platform: "discord",
      event_type: "message.created",
      payload_sha256: "abcdef0123456789",
      payload_json: "{}",
      received_at: "2026-09-04T12:30:00Z",
    }],
    [],
  ]);
  assertEquals(
    await new PostgresRawArchiveRepository(
      connection,
      new FakeFiles(),
    ).flush("/archive", 0),
    0,
  );
  assertEquals(connection.calls[0].parameters, [1]);
});
