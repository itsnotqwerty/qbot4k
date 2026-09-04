import { assert, assertEquals } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import {
  createShadowReadHandler,
  PostgresShadowComparisonStore,
  type ShadowComparison,
  type ShadowComparisonStore,
} from "../src/ops/shadow_read.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import { PostgresDatabase } from "../src/data/database.ts";

class MemoryStore implements ShadowComparisonStore {
  readonly comparisons: ShadowComparison[] = [];
  record(comparison: ShadowComparison): Promise<void> {
    this.comparisons.push(comparison);
    return Promise.resolve();
  }
}

Deno.test("shadow reads compare normalized responses and preserve primary output", async () => {
  const store = new MemoryStore();
  const upstreamRequests: Request[] = [];
  let clock = 0;
  const handler = createShadowReadHandler(
    () => Response.json({ status: "ready", items: [1, 2] }),
    "https://python.example/base/",
    store,
    (request) => {
      if (!(request instanceof Request)) {
        throw new TypeError("expected mirrored Request");
      }
      upstreamRequests.push(request);
      return Promise.resolve(
        new Response('{"items":[1,2],"status":"ready"}', {
          headers: { "content-type": "application/json; charset=utf-8" },
        }),
      );
    },
    () => clock += 2,
  );
  const response = await handler(
    new Request("https://fresh.example/api/overview?q=1", {
      headers: {
        cookie: "qbot4k_session=fixture",
        authorization: "Bearer secret",
      },
    }),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "ready", items: [1, 2] });
  assertEquals(upstreamRequests.length, 1);
  assertEquals(
    upstreamRequests[0].url,
    "https://python.example/api/overview?q=1",
  );
  assertEquals(
    upstreamRequests[0].headers.get("cookie"),
    "qbot4k_session=fixture",
  );
  assertEquals(upstreamRequests[0].headers.has("authorization"), false);
  assertEquals(upstreamRequests[0].headers.get("x-qbot-shadow-read"), "1");
  assertEquals(store.comparisons[0].matched, true);
  assertEquals(store.comparisons[0].path, "/api/overview");
});

Deno.test("shadow mode never mirrors mutating methods", async () => {
  const store = new MemoryStore();
  let upstreamCalls = 0;
  const handler = createShadowReadHandler(
    () => new Response(null, { status: 204 }),
    "https://python.example",
    store,
    () => {
      upstreamCalls += 1;
      return Promise.resolve(new Response());
    },
  );
  const response = await handler(
    new Request("https://fresh.example/moderation/bulk", {
      method: "POST",
      body: "confirmation=EXECUTE",
    }),
  );
  assertEquals(response.status, 204);
  assertEquals(upstreamCalls, 0);
  assertEquals(store.comparisons, []);
});

Deno.test("shadow failures do not alter primary responses", async () => {
  const store = new MemoryStore();
  const handler = createShadowReadHandler(
    () => new Response("primary", { status: 200 }),
    "https://python.example",
    store,
    () => Promise.reject(new Error("upstream unavailable")),
  );
  const response = await handler(
    new Request("https://fresh.example/dashboard"),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.text(), "primary");
  assertEquals(store.comparisons, []);
});

Deno.test("PostgreSQL shadow evidence records match and latency metrics", async () => {
  const calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
  const connection: DatabaseConnection = {
    query(sql, parameters = []): Promise<readonly DatabaseRow[]> {
      calls.push({ sql, parameters });
      return Promise.resolve([]);
    },
    transaction: (operation) => operation(connection),
  };
  await new PostgresShadowComparisonStore(connection).record({
    method: "GET",
    path: "/dashboard",
    matched: false,
    primaryStatus: 200,
    upstreamStatus: 500,
    primaryContentType: "text/html",
    upstreamContentType: "text/plain",
    primaryLatencyMs: 4,
    upstreamLatencyMs: 9,
    comparedAt: "2026-09-04T12:00:00.000Z",
  });
  assertEquals(calls.length, 3);
  assertEquals(calls.map((call) => call.parameters[0]), [
    "shadow_read.matched",
    "shadow_read.primary_latency_ms",
    "shadow_read.upstream_latency_ms",
  ]);
  assertEquals(calls[0].parameters[2], 0);
  assertEquals(calls[0].sql.includes("operational_metrics"), true);
});

const databaseUrl = Deno.env.get("QBOT_TEST_POSTGRES_URL");

Deno.test({
  name: "shadow reads persist PostgreSQL correctness and latency evidence",
  ignore: !databaseUrl,
  async fn() {
    const fixturePath = `/shadow-evidence-${crypto.randomUUID()}`;
    const sql = postgres(databaseUrl!, { max: 1 });
    const database = new PostgresDatabase(databaseUrl!, { maxConnections: 2 });
    try {
      const handler = createShadowReadHandler(
        () => Response.json({ fixture: true }),
        "http://127.0.0.1:8081",
        database.shadowComparisonStore(),
        () => Promise.resolve(Response.json({ fixture: true })),
      );
      await handler(new Request(`http://127.0.0.1:8080${fixturePath}`));

      const evidence = await sql`
        SELECT metric_name,value
          FROM operational_metrics
         WHERE dimension_key::jsonb->>'path'=${fixturePath}
         ORDER BY metric_name
      `;
      assertEquals(evidence.length, 3);
      assertEquals(
        evidence.find((row) => row.metric_name === "shadow_read.matched")
          ?.value,
        1,
      );
      for (
        const metric of evidence.filter((row) =>
          String(row.metric_name).endsWith("_latency_ms")
        )
      ) {
        assert(Number(metric.value) >= 0);
        assert(Number(metric.value) < 1_000);
      }
    } finally {
      await sql`
        DELETE FROM operational_metrics
         WHERE dimension_key::jsonb->>'path'=${fixturePath}
      `;
      await database.close();
      await sql.end();
    }
  },
});
