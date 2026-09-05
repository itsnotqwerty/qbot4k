import { assertEquals } from "jsr:@std/assert@1.0.14";
import type { DatabaseConnection, DatabaseRow } from "../src/data/database.ts";
import {
  PostgresSocialScoreRepository,
  SOCIAL_SCORE_MODEL_VERSION,
} from "../src/domain/score_materialization.ts";

class FakeConnection implements DatabaseConnection {
  readonly queries: { sql: string; parameters: readonly unknown[] }[] = [];

  query(sql: string) {
    this.queries.push({ sql, parameters: [] });
    if (sql.startsWith("SELECT id FROM users")) {
      return Promise.resolve([{ id: 9 }] as DatabaseRow[]);
    }
    if (sql.includes("FROM derived_signals")) {
      return Promise.resolve([
        {
          signal_key: "activity.eligible_message_count",
          value_real: 25,
          confidence: 1,
          evidence_count: 25,
        },
        {
          signal_key: "identity.linked_account_count",
          value_real: 2,
          confidence: 1,
          evidence_count: 2,
        },
        {
          signal_key: "risk.composite",
          value_real: 10,
          confidence: 1,
          evidence_count: 25,
        },
      ] as DatabaseRow[]);
    }
    if (sql.includes("FROM derived_signal_windows")) {
      return Promise.resolve([
        {
          window_name: "24h",
          value: 5,
          confidence: 1,
          evidence_count: 3,
        },
      ] as DatabaseRow[]);
    }
    if (sql.includes("INSERT INTO social_score_runs")) {
      return Promise.resolve([{ id: 88 }] as DatabaseRow[]);
    }
    return Promise.resolve([] as DatabaseRow[]);
  }

  transaction<T>(callback: (connection: DatabaseConnection) => Promise<T>) {
    return callback(this);
  }
}

Deno.test("score v2 materializes explainable run without prior-score input", async () => {
  const connection = new FakeConnection();
  await new PostgresSocialScoreRepository(connection).calculate(9);
  const run = connection.queries.find((query) =>
    query.sql.includes("INSERT INTO social_score_runs")
  );
  const update = connection.queries.find((query) =>
    query.sql.includes("UPDATE users SET")
  );
  assertEquals(Boolean(run), true);
  assertEquals(Boolean(update), true);
  assertEquals(
    connection.queries.some((query) =>
      query.sql.includes("current_reputation_score") &&
      query.sql.includes("SELECT")
    ),
    false,
  );
});

Deno.test("score recalculation jobs are tenant-safe and model versioned", async () => {
  const connection = new FakeConnection();
  await new PostgresSocialScoreRepository(connection).enqueue(9, "test");
  const enqueue = connection.queries[0];
  assertEquals(enqueue.sql.includes("INSERT INTO processing_jobs"), true);
  assertEquals(enqueue.sql.includes("messages WHERE user_id=$1"), true);
  assertEquals(
    enqueue.sql.includes("ON CONFLICT(idempotency_key) DO NOTHING"),
    true,
  );
  assertEquals(SOCIAL_SCORE_MODEL_VERSION, 2);
});
