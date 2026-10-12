import { assertEquals } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { readReliabilitySummary } from "../src/data/database.ts";

const databaseUrl = Deno.env.get("QBOT_TEST_POSTGRES_URL");

Deno.test({
  name:
    "PostgreSQL status summaries preserve observations and downtime intervals",
  ignore: !databaseUrl,
  async fn() {
    const sql = postgres(databaseUrl!, { max: 1 });
    try {
      await sql.begin(async (transaction) => {
        await transaction.unsafe(`
          CREATE TEMP TABLE service_reliability_buckets (
            service_name text, bucket_start text, is_up integer, status text
          ) ON COMMIT DROP
        `);
        await transaction.unsafe(`
          INSERT INTO service_reliability_buckets VALUES
            ('web','2026-10-12T12:00:00.000Z',0,'unknown'),
            ('web','2026-10-12T12:00:00.000Z',1,'ready'),
            ('web','2026-10-12T12:01:00.000Z',0,'down'),
            ('web','2026-10-12T12:01:00.000Z',0,'unknown'),
            ('web','2026-10-12T12:03:00.000Z',0,'unknown'),
            ('web','2026-10-12T12:09:00.000Z',0,'unknown'),
            ('web','2026-10-12T12:10:00.000Z',1,'degraded'),
            ('other','2026-10-12T12:00:00.000Z',1,'ready'),
            ('web','2026-10-11T12:00:00.000Z',1,'ready'),
            ('web','2026-10-13T12:00:00.000Z',1,'ready')
        `);
        const connection = {
          query: async (query: string, parameters: readonly unknown[] = []) =>
            await transaction.unsafe(query, [...parameters] as never[]),
          transaction: () => {
            throw new Error("Nested transaction is not expected");
          },
        };
        const summary = await readReliabilitySummary(
          connection,
          "web",
          "2026-10-12T00:00:00.000Z",
          "2026-10-12T12:10:00.000Z",
        );
        assertEquals(summary.days, [{
          date: "2026-10-12",
          observed: 7,
          up: 2,
          degraded: 1,
        }]);
        assertEquals(summary.downtime, [
          {
            start: "2026-10-12T12:09:00.000Z",
            minutes: 1,
            status: "unknown",
            ongoing: true,
          },
          {
            start: "2026-10-12T12:03:00.000Z",
            minutes: 1,
            status: "unknown",
            ongoing: false,
          },
          {
            start: "2026-10-12T12:00:00.000Z",
            minutes: 2,
            status: "down",
            ongoing: false,
          },
        ]);
        await transaction.unsafe(`
          INSERT INTO service_reliability_buckets
          SELECT 'events',
                 to_char(moment AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                 0, 'down'
            FROM generate_series(
              '2026-10-12T00:00:00Z'::timestamptz,
              '2026-10-12T03:00:00Z'::timestamptz,
              interval '3 minutes'
            ) AS moment
        `);
        const bounded = await readReliabilitySummary(
          connection,
          "events",
          "2026-10-12T00:00:00.000Z",
          "2026-10-12T12:10:00.000Z",
        );
        assertEquals(bounded.downtime.length, 25);
        assertEquals(bounded.downtime[0].start, "2026-10-12T03:00:00.000Z");
      });
    } finally {
      await sql.end();
    }
  },
});
