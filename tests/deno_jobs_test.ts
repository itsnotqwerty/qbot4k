import {
  assertEquals,
  assertRejects,
  assertThrows,
} from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  JobLeaseLostError,
  JobRegistry,
  PermanentJobError,
  PostgresProcessingJobRepository,
  type ProcessingJob,
  type ProcessingJobStore,
  ProcessingWorker,
  ProcessingWorkerPool,
} from "../src/jobs/jobs.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<
    { sql: string; parameters: readonly DatabaseParameter[] }
  > = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]> = []) {}
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

const row = {
  id: 7,
  community_id: 2,
  stage: "analysis",
  job_type: "analyze.message.created",
  observation_id: 9,
  payload_json: '{"source":"test"}',
  attempts: 1,
  max_attempts: 5,
};

Deno.test("enqueue resolves tenant ownership and consumes quota only for new jobs", async () => {
  const connection = new FakeConnection([
    [{ community_id: 2 }],
    [{ id: 7 }],
    [],
    [{ usage_count: 1 }],
  ]);
  const repository = new PostgresProcessingJobRepository(connection);
  assertEquals(
    await repository.enqueue({
      stage: "analysis",
      jobType: "analyze.message.created",
      idempotencyKey: "observation:9:analysis:v1",
      observationId: 9,
    }),
    7,
  );
  assertEquals(connection.calls[1].parameters[0], 2);
  assertEquals(connection.calls[3].sql.includes("tenant_quota_usage"), true);
  assertEquals(connection.calls[3].parameters[0], 2);

  const duplicate = new FakeConnection([[], []]);
  assertEquals(
    await new PostgresProcessingJobRepository(duplicate).enqueue({
      stage: "action",
      jobType: "fixture",
      idempotencyKey: "duplicate",
      communityId: 2,
    }),
    null,
  );
  assertEquals(duplicate.calls.length, 1);
});

Deno.test("PostgreSQL claims use fair leased SKIP LOCKED transactions", async () => {
  const connection = new FakeConnection([[], [row], []]);
  const job = await new PostgresProcessingJobRepository(connection).claim(
    "analysis",
    "worker-1",
  );
  assertEquals(job?.communityId, 2);
  assertEquals(job?.payload, { source: "test" });
  assertEquals(connection.calls[0].sql.includes("pg_advisory_xact_lock"), true);
  assertEquals(
    connection.calls[1].sql.includes("FOR UPDATE SKIP LOCKED"),
    true,
  );
  assertEquals(
    connection.calls[1].sql.includes("available_at::timestamptz"),
    true,
  );
  assertEquals(connection.calls[1].sql.includes("INTERVAL '2 minutes'"), true);
  assertEquals(connection.calls[2].sql.includes("tenant_job_schedule"), true);
});

Deno.test("lease renewal is bounded and requires active worker ownership", async () => {
  const active = new FakeConnection([[{ id: 7 }]]);
  assertEquals(
    await new PostgresProcessingJobRepository(active).renewLease(
      7,
      "worker-1",
      10_000,
    ),
    true,
  );
  assertEquals(active.calls[0].parameters, [7, "worker-1", 3600]);
  assertEquals(active.calls[0].sql.includes("claimed_by=$2"), true);
  const lost = new FakeConnection([[]]);
  assertEquals(
    await new PostgresProcessingJobRepository(lost).renewLease(7, "worker-2"),
    false,
  );
});

Deno.test("retry exhausts attempts into a permanent failed state", async () => {
  const retrying = new FakeConnection([[{ attempts: 2, max_attempts: 5 }], [{
    id: 7,
  }]]);
  assertEquals(
    await new PostgresProcessingJobRepository(retrying).retry(
      7,
      "worker-1",
      "temporary",
    ),
    true,
  );
  assertEquals(retrying.calls[1].parameters[1], 4);
  const exhausted = new FakeConnection([
    [{
      community_id: 2,
      observation_id: 9,
      stage: "analysis",
      payload_json: "{}",
      attempts: 5,
      max_attempts: 5,
    }],
    [{ id: 7 }],
    [],
  ]);
  assertEquals(
    await new PostgresProcessingJobRepository(exhausted).retry(
      7,
      "worker-1",
      "final",
    ),
    false,
  );
  assertEquals(exhausted.calls[1].sql.includes("status='failed'"), true);
  assertEquals(exhausted.calls[0].sql.includes("claimed_by=$2"), true);
  assertEquals(exhausted.calls[2].sql.includes("dead_letter_events"), true);
  assertEquals(exhausted.calls[2].parameters[0], 2);
});

Deno.test("worker completes successes and distinguishes transient and permanent failures", async () => {
  const jobs: ProcessingJob[] = [
    {
      id: 1,
      communityId: 1,
      stage: "analysis",
      jobType: "ok",
      observationId: null,
      payload: {},
      attempts: 1,
      maxAttempts: 5,
    },
    {
      id: 2,
      communityId: 1,
      stage: "analysis",
      jobType: "retry",
      observationId: null,
      payload: {},
      attempts: 1,
      maxAttempts: 5,
    },
    {
      id: 3,
      communityId: 1,
      stage: "analysis",
      jobType: "missing",
      observationId: null,
      payload: {},
      attempts: 1,
      maxAttempts: 5,
    },
  ];
  const transitions: string[] = [];
  const store: ProcessingJobStore = {
    enqueue: () => Promise.resolve(null),
    claim: () => Promise.resolve(jobs.shift() ?? null),
    complete: (id) => {
      transitions.push(`complete:${id}`);
      return Promise.resolve();
    },
    renewLease: () => Promise.resolve(true),
    retry: (id) => {
      transitions.push(`retry:${id}`);
      return Promise.resolve(true);
    },
    fail: (id) => {
      transitions.push(`fail:${id}`);
      return Promise.resolve();
    },
    recoverExpired: () => Promise.resolve(0),
    replayDeadLetter: () => Promise.resolve(0),
  };
  const registry = new JobRegistry().register("ok", () => Promise.resolve())
    .register("retry", () => Promise.reject(new Error("network")));
  const worker = new ProcessingWorker(store, registry, "analysis", "worker-1");
  await worker.processNext();
  await worker.processNext();
  await worker.processNext();
  assertEquals(transitions, ["complete:1", "retry:2", "fail:3"]);
  assertThrows(
    () => registry.register("ok", () => Promise.resolve()),
    TypeError,
    "already registered",
  );
  assertEquals(new PermanentJobError("x") instanceof Error, true);
});

Deno.test("worker renews long jobs and leaves lost leases for recovery", async () => {
  let releaseHandler: (() => void) | undefined;
  const handlerBlocked = new Promise<void>((resolve) =>
    releaseHandler = resolve
  );
  const transitions: string[] = [];
  let renewals = 0;
  const store: ProcessingJobStore = {
    enqueue: () => Promise.resolve(null),
    claim: () =>
      Promise.resolve({
        id: 8,
        communityId: 1,
        stage: "analysis",
        jobType: "slow",
        observationId: null,
        payload: {},
        attempts: 1,
        maxAttempts: 5,
      }),
    complete: (id) => {
      transitions.push(`complete:${id}`);
      return Promise.resolve();
    },
    renewLease: () => {
      renewals += 1;
      releaseHandler?.();
      return Promise.resolve(false);
    },
    retry: (id) => {
      transitions.push(`retry:${id}`);
      return Promise.resolve(true);
    },
    fail: (id) => {
      transitions.push(`fail:${id}`);
      return Promise.resolve();
    },
    recoverExpired: () => Promise.resolve(0),
    replayDeadLetter: () => Promise.resolve(0),
  };
  const worker = new ProcessingWorker(
    store,
    new JobRegistry().register("slow", () => handlerBlocked),
    "analysis",
    "worker-1",
    500,
    60_000,
    0,
  );

  await assertRejects(
    () => worker.processNext(),
    JobLeaseLostError,
    "lease ownership was lost",
  );
  assertEquals(renewals, 1);
  assertEquals(transitions, []);
});

Deno.test("expired leases recover and dead-letter exhausted jobs atomically", async () => {
  const connection = new FakeConnection([[{ id: 1 }, { id: 2 }]]);
  assertEquals(
    await new PostgresProcessingJobRepository(connection).recoverExpired(),
    2,
  );
  assertEquals(
    connection.calls[0].sql.includes("attempts>=max_attempts"),
    true,
  );
  assertEquals(
    connection.calls[0].sql.includes("lease_expires_at::timestamptz"),
    true,
  );
  assertEquals(
    connection.calls[0].sql.includes("INSERT INTO dead_letter_events"),
    true,
  );
  assertEquals(
    connection.calls[0].sql.includes("WHERE status='failed'"),
    true,
  );
  assertEquals(connection.calls.length, 1);
});

Deno.test("dead-letter replay validates tenant and atomically enqueues fresh work", async () => {
  const connection = new FakeConnection([
    [{
      community_id: 2,
      observation_id: 9,
      observation_community_id: 2,
      stage: "analysis",
      status: "open",
      job_type: "analyze.message.created",
    }],
    [{ id: 11 }],
    [],
    [{ usage_count: 1 }],
    [{ id: 5 }],
  ]);
  const replayedAt = new Date("2026-08-11T12:00:00Z");
  assertEquals(
    await new PostgresProcessingJobRepository(connection).replayDeadLetter(
      5,
      replayedAt,
    ),
    11,
  );
  assertEquals(connection.calls[0].sql.includes("FOR UPDATE OF d"), true);
  assertEquals(connection.calls[1].parameters, [
    2,
    "analysis",
    "analyze.message.created",
    9,
    "dead-letter:5:2026-08-11T12:00:00.000+00:00",
  ]);
  assertEquals(connection.calls[3].sql.includes("tenant_quota_usage"), true);
  assertEquals(connection.calls[4].sql.includes("status='replayed'"), true);

  const mismatched = new FakeConnection([[{
    community_id: 2,
    observation_id: 9,
    observation_community_id: 3,
    stage: "analysis",
    status: "open",
    job_type: "analyze.message.created",
  }]]);
  await assertRejects(
    () => new PostgresProcessingJobRepository(mismatched).replayDeadLetter(5),
    TypeError,
    "dead letter tenant does not own observation",
  );
  assertEquals(mismatched.calls.length, 1);
});

Deno.test("worker schedules lease recovery while it is running", async () => {
  const abortController = new AbortController();
  let recoveries = 0;
  const store: ProcessingJobStore = {
    enqueue: () => Promise.resolve(null),
    claim: () => Promise.resolve(null),
    complete: () => Promise.resolve(),
    renewLease: () => Promise.resolve(true),
    retry: () => Promise.resolve(true),
    fail: () => Promise.resolve(),
    recoverExpired: () => {
      recoveries += 1;
      if (recoveries === 2) abortController.abort();
      return Promise.resolve(0);
    },
    replayDeadLetter: () => Promise.resolve(0),
  };
  const worker = new ProcessingWorker(
    store,
    new JobRegistry(),
    "analysis",
    "worker-1",
    0,
    0,
  );

  await worker.run(abortController.signal);

  assertEquals(recoveries, 2);
});

Deno.test("worker loop survives a claim crash and resumes consumption", async () => {
  const abortController = new AbortController();
  const errors: unknown[] = [];
  let claims = 0;
  const store: ProcessingJobStore = {
    enqueue: () => Promise.resolve(null),
    claim: () => {
      claims += 1;
      if (claims === 1) return Promise.reject(new Error("database restart"));
      if (claims === 2) {
        return Promise.resolve({
          id: 9,
          communityId: 1,
          stage: "analysis",
          jobType: "after-crash",
          observationId: null,
          payload: {},
          attempts: 1,
          maxAttempts: 5,
        });
      }
      return Promise.resolve(null);
    },
    complete: () => {
      abortController.abort();
      return Promise.resolve();
    },
    renewLease: () => Promise.resolve(true),
    retry: () => Promise.resolve(true),
    fail: () => Promise.resolve(),
    recoverExpired: () => Promise.resolve(0),
    replayDeadLetter: () => Promise.resolve(0),
  };
  const worker = new ProcessingWorker(
    store,
    new JobRegistry().register("after-crash", () => Promise.resolve()),
    "analysis",
    "worker-1",
    0,
    60_000,
    40_000,
    (error) => errors.push(error),
  );

  await worker.run(abortController.signal);

  assertEquals(claims, 2);
  assertEquals(errors.length, 1);
  assertEquals((errors[0] as Error).message, "database restart");
});

Deno.test("worker pool processes concurrently with unique ownership IDs", async () => {
  const abortController = new AbortController();
  const claimedBy: string[] = [];
  let nextJobId = 1;
  let activeHandlers = 0;
  let peakHandlers = 0;
  let releaseHandlers: (() => void) | undefined;
  const barrier = new Promise<void>((resolve) => releaseHandlers = resolve);
  const store: ProcessingJobStore = {
    enqueue: () => Promise.resolve(null),
    claim: (_stage, workerId) => {
      if (nextJobId > 3) return Promise.resolve(null);
      claimedBy.push(workerId);
      return Promise.resolve({
        id: nextJobId++,
        communityId: 1,
        stage: "analysis",
        jobType: "concurrent",
        observationId: null,
        payload: {},
        attempts: 1,
        maxAttempts: 5,
      });
    },
    complete: () => {
      if (claimedBy.length === 3) abortController.abort();
      return Promise.resolve();
    },
    renewLease: () => Promise.resolve(true),
    retry: () => Promise.resolve(true),
    fail: () => Promise.resolve(),
    recoverExpired: () => Promise.resolve(0),
    replayDeadLetter: () => Promise.resolve(0),
  };
  const registry = new JobRegistry().register("concurrent", async () => {
    activeHandlers += 1;
    peakHandlers = Math.max(peakHandlers, activeHandlers);
    if (activeHandlers === 3) releaseHandlers?.();
    await barrier;
    activeHandlers -= 1;
  });
  const pool = new ProcessingWorkerPool(
    store,
    registry,
    "analysis",
    "analysis-test",
    3,
    0,
  );

  await pool.run(abortController.signal);

  assertEquals(peakHandlers, 3);
  assertEquals(
    new Set(claimedBy),
    new Set([
      "analysis-test-1",
      "analysis-test-2",
      "analysis-test-3",
    ]),
  );
  assertThrows(
    () =>
      new ProcessingWorkerPool(
        store,
        registry,
        "analysis",
        "analysis-test",
        0,
      ),
    TypeError,
    "positive integer",
  );
});
