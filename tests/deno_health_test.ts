import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  type DatabaseConnection,
  type DatabaseParameter,
  type DatabaseRow,
  EXPECTED_SCHEMA_VERSION,
  probeDatabaseHealth,
} from "../src/data/database.ts";
import { healthResponse, RoleHealthMonitor } from "../src/core/health.ts";

class HealthConnection implements DatabaseConnection {
  constructor(
    private readonly rows: readonly DatabaseRow[] = [],
    private readonly failure?: Error,
  ) {}

  query(
    _sql: string,
    _parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    return this.failure
      ? Promise.reject(this.failure)
      : Promise.resolve(this.rows);
  }

  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

Deno.test("PostgreSQL health requires the current migration version", async () => {
  const ready = await probeDatabaseHealth(
    new HealthConnection([{
      table_count: 42,
      schema_version: EXPECTED_SCHEMA_VERSION,
    }]),
    "postgresql://database/qbot4k",
  );
  assertEquals(ready.status, "ready");
  assertEquals(ready.integrity, "ok");
  assertEquals(ready.tableCount, 42);

  const pending = await probeDatabaseHealth(
    new HealthConnection([{
      table_count: 42,
      schema_version: EXPECTED_SCHEMA_VERSION - 1,
    }]),
    "postgresql://database/qbot4k",
  );
  assertEquals(pending.status, "degraded");
  assertEquals(pending.integrity, "migration_pending");
});

Deno.test("PostgreSQL health reports unavailable without throwing", async () => {
  const health = await probeDatabaseHealth(
    new HealthConnection([], new Error("connection refused")),
    "postgresql://database/qbot4k",
  );
  assertEquals(health.status, "degraded");
  assertEquals(health.integrity, "unavailable");
  assertEquals(health.error, "connection refused");
});

Deno.test("role readiness includes migration and dependency state", async () => {
  const database = {
    health: () =>
      probeDatabaseHealth(
        new HealthConnection([{
          table_count: 42,
          schema_version: EXPECTED_SCHEMA_VERSION,
        }]),
        "postgresql://database/qbot4k",
      ),
  };
  const monitor = new RoleHealthMonitor(
    database,
    "jobs",
    new Date("2026-09-03T12:00:00Z"),
  );
  monitor.setStatus("ready");
  const snapshot = await monitor.snapshot(new Date("2026-09-03T12:00:05Z"));
  assertEquals(snapshot.status, "ready");
  assertEquals(snapshot.services.jobs, "ready");
  assertEquals(snapshot.services.web, "disabled");
  assertEquals(snapshot.dependencies.migrations, "ready");
  assertEquals(snapshot.uptime.app_uptime_seconds, 5);
});

Deno.test("ready endpoint returns 503 while migrations are pending", async () => {
  const database = {
    health: () =>
      probeDatabaseHealth(
        new HealthConnection([{
          table_count: 41,
          schema_version: EXPECTED_SCHEMA_VERSION - 1,
        }]),
        "postgresql://database/qbot4k",
      ),
  };
  const monitor = new RoleHealthMonitor(database, "web");
  monitor.setStatus("ready");
  assertEquals((await healthResponse("/health/live", monitor)).status, 200);
  assertEquals((await healthResponse("/health/ready", monitor)).status, 503);
});
