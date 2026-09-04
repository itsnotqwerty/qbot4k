import { assertEquals } from "jsr:@std/assert@1.0.14";
import { monitorCutoverWindow } from "../cutover_monitor.ts";
import { evaluateCutoverSignals } from "../src/ops/cutover_monitor.ts";

const healthy = {
  job_error_rate_percent: 0.5,
  queue_age_seconds: 120,
  unhealthy_providers: 0,
  active_communities: 2,
  missing_or_breached_slos: 0,
  webhook_acceptance_ms: 250,
  database_connection_percent: 20,
} as const;

Deno.test("cutover monitor passes healthy rollback-window signals", () => {
  const report = evaluateCutoverSignals(
    healthy,
    new Date("2026-09-04T12:00:00Z"),
    15,
  );
  assertEquals(report.status, "pass");
  assertEquals(report.blockers, []);
});

Deno.test("cutover monitor names every breached rollback-window signal", () => {
  const report = evaluateCutoverSignals(
    {
      job_error_rate_percent: 2,
      queue_age_seconds: 901,
      unhealthy_providers: 1,
      active_communities: 2,
      missing_or_breached_slos: 1,
      webhook_acceptance_ms: 1_001,
      database_connection_percent: 81,
    },
    new Date("2026-09-04T12:00:00Z"),
    15,
  );
  assertEquals(report.status, "fail");
  assertEquals(report.blockers, [
    "job_error_rate",
    "queue_age",
    "provider_health",
    "tenant_slo",
    "webhook_acceptance",
    "database_saturation",
  ]);
});

Deno.test("cutover monitor samples the complete rollback window", async () => {
  const report = evaluateCutoverSignals(
    healthy,
    new Date("2026-09-04T12:00:00Z"),
    15,
  );
  const emitted: string[] = [];
  const waits: number[] = [];
  const result = await monitorCutoverWindow({
    sampleCount: 3,
    intervalMilliseconds: 900_000,
    collect: () => Promise.resolve(report),
    emit: (sample) => emitted.push(sample.status),
    wait: (milliseconds) => {
      waits.push(milliseconds);
      return Promise.resolve();
    },
  });
  assertEquals(result, 0);
  assertEquals(emitted, ["pass", "pass", "pass"]);
  assertEquals(waits, [900_000, 900_000]);
});

Deno.test("cutover monitor exits immediately on a rollback blocker", async () => {
  const reports = [
    evaluateCutoverSignals(healthy, new Date("2026-09-04T12:00:00Z"), 15),
    evaluateCutoverSignals(
      { ...healthy, queue_age_seconds: 901 },
      new Date("2026-09-04T12:15:00Z"),
      15,
    ),
  ];
  let collected = 0;
  let waits = 0;
  const result = await monitorCutoverWindow({
    sampleCount: 3,
    intervalMilliseconds: 900_000,
    collect: () => Promise.resolve(reports[collected++]),
    emit: () => undefined,
    wait: () => {
      waits += 1;
      return Promise.resolve();
    },
  });
  assertEquals(result, 1);
  assertEquals(collected, 2);
  assertEquals(waits, 1);
});
