import { assertEquals } from "jsr:@std/assert@1.0.14";
import { evaluateCutoverPreflight } from "../src/ops/cutover_preflight.ts";
import type { CutoverMonitorReport } from "../src/ops/cutover_monitor.ts";

const monitor: CutoverMonitorReport = {
  status: "pass",
  blockers: [],
  observed_at: "2026-09-04T12:00:00.000Z",
  window_minutes: 15,
  job_error_rate_percent: 0,
  queue_age_seconds: 0,
  unhealthy_providers: 0,
  active_communities: 2,
  missing_or_breached_slos: 0,
  webhook_acceptance_ms: 100,
  database_connection_percent: 10,
};

Deno.test("cutover preflight passes only the ready ownership state", () => {
  const report = evaluateCutoverPreflight(
    {
      non_deno_job_types: [],
      non_deno_installations: [],
      inactive_provider_leases: [],
    },
    false,
    monitor,
  );
  assertEquals(report.status, "pass");
  assertEquals(report.blockers, []);
});

Deno.test("cutover preflight names ownership, lease, web, and monitor blockers", () => {
  const report = evaluateCutoverPreflight(
    {
      non_deno_job_types: ["analysis"],
      non_deno_installations: [10],
      inactive_provider_leases: [11],
    },
    true,
    { ...monitor, status: "fail", blockers: ["queue_age"] },
  );
  assertEquals(report.status, "fail");
  assertEquals(report.blockers, [
    "queue_age",
    "job_ownership",
    "provider_ownership",
    "provider_lease",
    "web_read_only",
  ]);
});
