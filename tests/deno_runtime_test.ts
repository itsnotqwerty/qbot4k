import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import { SERVICE_ROLES, type ServiceRole } from "../src/core/config.ts";
import type { DatabaseHealthSource } from "../src/data/database.ts";
import { RoleHealthMonitor } from "../src/core/health.ts";
import {
  ShutdownController,
  type ShutdownSignal,
  type SignalRegistrar,
} from "../src/core/lifecycle.ts";
import { StructuredLogger } from "../src/core/logging.ts";
import type { MaintenanceRunner } from "../src/jobs/maintenance.ts";
import {
  AnalysisRoleService,
  JobsRoleService,
  type RoleService,
  runRole,
  TwitchStreamPollingWorker,
} from "../runtime.ts";

class FakeSignals implements SignalRegistrar {
  add(_signal: ShutdownSignal, _listener: () => void): void {}
  remove(_signal: ShutdownSignal, _listener: () => void): void {}
}

class FakeService implements RoleService {
  started = false;
  stopped = false;
  start(_signal: AbortSignal): Promise<void> {
    this.started = true;
    return Promise.resolve();
  }
  stop(): Promise<void> {
    this.stopped = true;
    return Promise.resolve();
  }
}

const database: DatabaseHealthSource = {
  health: () =>
    Promise.resolve({
      status: "ready",
      backend: "postgresql",
      path: "postgresql://database/qbot4k",
      tableCount: 42,
      integrity: "ok",
      schemaVersion: 27,
    }),
};

function lifecycle(): ShutdownController {
  return new ShutdownController(
    new StructuredLogger("test"),
    new FakeSignals(),
  );
}

Deno.test("every enabled process role starts, reports ready, and stops", async () => {
  for (const role of SERVICE_ROLES) {
    const service = new FakeService();
    const monitor = new RoleHealthMonitor(database, role);
    let snapshot: unknown;
    await runRole(
      role,
      { enabledServices: [role] },
      service,
      monitor,
      lifecycle(),
      {
        once: true,
        writeSnapshot: (value) => snapshot = value,
      },
    );
    assertEquals(service.started, true);
    assertEquals(service.stopped, true);
    assertEquals((snapshot as { status: string }).status, "ready");
  }
});

Deno.test("long-running role exits on shutdown request", async () => {
  const role: ServiceRole = "analysis";
  const service = new FakeService();
  const controller = lifecycle();
  const running = runRole(
    role,
    { enabledServices: [role] },
    service,
    new RoleHealthMonitor(database, role),
    controller,
  );
  await Promise.resolve();
  controller.request("SIGTERM");
  await running;
  assertEquals(service.stopped, true);
});

Deno.test("disabled role is rejected before startup", async () => {
  const service = new FakeService();
  await assertRejects(
    () =>
      runRole(
        "discord",
        { enabledServices: ["analysis"] },
        service,
        new RoleHealthMonitor(database, "discord"),
        lifecycle(),
      ),
    TypeError,
    "role discord is not enabled",
  );
  assertEquals(service.started, false);
});

Deno.test("analysis role starts its worker pool and stops through abort", async () => {
  let started = false;
  let stopped = false;
  const service = new AnalysisRoleService({
    run: (signal) => {
      started = true;
      return new Promise<void>((resolve) => {
        signal.addEventListener("abort", () => {
          stopped = true;
          resolve();
        }, { once: true });
      });
    },
  });
  await service.start(new AbortController().signal);
  assertEquals(started, true);
  await service.stop();
  assertEquals(stopped, true);
});

Deno.test("jobs role gates readiness on scheduled work and stops promptly", async () => {
  const runs: Array<{ auditDays: number; archiveRoot: string }> = [];
  const runner: MaintenanceRunner = {
    run: (_now, auditDays, archiveRoot) => {
      runs.push({ auditDays, archiveRoot });
      return Promise.resolve({
        deletedMessages: 0,
        deletedObservations: 0,
        deletedAuditLogRows: 0,
        deletedSignalRuns: 0,
        deletedScoreRuns: 0,
        deletedProcessingJobs: 0,
        recoveredProcessingJobs: 0,
        rawEventsArchived: 0,
        rollupRows: 5,
        checkpointRemindersQueued: 0,
      });
    },
  };
  const records: string[] = [];
  const service = new JobsRoleService(
    runner,
    {
      refresh: () =>
        Promise.resolve({
          topicCount: 1,
          graphNodeCount: 2,
          identitySuggestionCount: 3,
          cohortBaselineCount: 4,
          evaluationRunId: 5,
        }),
    },
    {
      create: () =>
        Promise.resolve({
          backupPath: "/backup/qbot4k.dump",
          metadataPath: "/backup/qbot4k.dump.json",
          sha256: "abc",
          sizeBytes: 10,
        }),
      verify: () => Promise.reject(new Error("not used")),
    },
    {
      maintenanceIntervalSeconds: 60,
      analyticsIntervalSeconds: 120,
      backupIntervalSeconds: 180,
      auditRetentionDays: 90,
      rawArchiveDir: "./var/raw-events",
    },
    new StructuredLogger(
      "test.jobs",
      "INFO",
      (record) => records.push(record.message),
    ),
  );
  await service.start(new AbortController().signal);
  await service.stop();
  assertEquals(runs, [{ auditDays: 90, archiveRoot: "./var/raw-events" }]);
  assertEquals(records, [
    "maintenance run complete",
    "analytics run complete",
    "backup run complete",
  ]);
});

Deno.test("Twitch stream polling worker runs immediately and stops on abort", async () => {
  const controller = new AbortController();
  let polls = 0;
  const worker = new TwitchStreamPollingWorker({
    poll: () => {
      polls += 1;
      controller.abort();
      return Promise.resolve({ checked: 1, transitions: 1 });
    },
  });
  await worker.run(controller.signal);
  assertEquals(polls, 1);
});
