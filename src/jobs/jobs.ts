import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import { consumeTenantQuota } from "../domain/quota.ts";

export type ProcessingStage = "analysis" | "action";

export interface ProcessingJob {
  readonly id: number;
  readonly communityId: number;
  readonly stage: ProcessingStage;
  readonly jobType: string;
  readonly observationId: number | null;
  readonly payload: Readonly<Record<string, unknown>>;
  readonly attempts: number;
  readonly maxAttempts: number;
}

export interface EnqueueProcessingJob {
  readonly stage: ProcessingStage;
  readonly jobType: string;
  readonly idempotencyKey: string;
  readonly communityId?: number;
  readonly observationId?: number;
  readonly payload?: Readonly<Record<string, unknown>>;
  readonly priority?: number;
  readonly maxAttempts?: number;
}

export interface ProcessingJobStore {
  enqueue(input: EnqueueProcessingJob): Promise<number | null>;
  claim(
    stage: ProcessingStage,
    workerId: string,
  ): Promise<ProcessingJob | null>;
  complete(jobId: number, workerId: string): Promise<void>;
  renewLease(
    jobId: number,
    workerId: string,
    leaseSeconds?: number,
  ): Promise<boolean>;
  retry(
    jobId: number,
    workerId: string,
    error: string,
    delaySeconds?: number,
  ): Promise<boolean>;
  fail(jobId: number, workerId: string, error: string): Promise<void>;
  recoverExpired(): Promise<number>;
  replayDeadLetter(deadLetterId: number, replayedAt?: Date): Promise<number>;
}

export class PostgresProcessingJobRepository implements ProcessingJobStore {
  constructor(private readonly connection: DatabaseConnection) {}

  async enqueue(input: EnqueueProcessingJob): Promise<number | null> {
    const jobType = input.jobType.trim();
    const idempotencyKey = input.idempotencyKey.trim();
    if (!jobType || !idempotencyKey) {
      throw new TypeError("job_type and idempotency_key must not be empty");
    }
    if (!isProcessingStage(input.stage)) {
      throw new TypeError("stage must be either 'analysis' or 'action'");
    }
    return await this.connection.transaction(async (connection) => {
      let communityId = input.communityId;
      if (input.observationId !== undefined) {
        const observation = (await connection.query(
          "SELECT community_id FROM observations WHERE id=$1",
          [input.observationId],
        ))[0];
        if (!observation) {
          throw new TypeError("processing job observation was not found");
        }
        const observationCommunityId = integer(
          observation.community_id,
          "community_id",
        );
        if (
          communityId !== undefined && communityId !== observationCommunityId
        ) throw new TypeError("processing job tenant does not own observation");
        communityId = observationCommunityId;
      }
      if (!Number.isSafeInteger(communityId) || Number(communityId) <= 0) {
        throw new TypeError("processing job community_id is required");
      }
      const inserted = (await connection.query(
        `INSERT INTO processing_jobs(
           community_id,stage,job_type,observation_id,payload_json,
           priority,max_attempts,idempotency_key
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
         ON CONFLICT(idempotency_key) DO NOTHING RETURNING id`,
        [
          Number(communityId),
          input.stage,
          jobType,
          input.observationId ?? null,
          JSON.stringify(input.payload ?? {}),
          input.priority ?? 100,
          input.maxAttempts ?? 5,
          idempotencyKey,
        ],
      ))[0];
      if (!inserted) return null;
      await consumeTenantQuota(connection, Number(communityId), "jobs");
      return integer(inserted.id, "id");
    });
  }

  async claim(
    stage: ProcessingStage,
    workerId: string,
  ): Promise<ProcessingJob | null> {
    const normalizedStage = stage.trim().toLocaleLowerCase();
    const normalizedWorkerId = workerId.trim();
    if (!isProcessingStage(normalizedStage)) {
      throw new TypeError("stage must be either 'analysis' or 'action'");
    }
    if (!normalizedWorkerId) throw new TypeError("worker_id must not be empty");

    return await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtext('qbot4k:processing_jobs:' || $1))",
        [normalizedStage],
      );
      const rows = await connection.query(
        `WITH eligible_tenants AS (
           SELECT DISTINCT community_id
             FROM processing_jobs
            WHERE stage=$1 AND community_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM processing_job_ownership AS ownership
                 WHERE ownership.job_type=processing_jobs.job_type
                   AND ownership.owner_runtime='deno'
              )
              AND ((status IN ('pending','retry') AND available_at::timestamptz<=CURRENT_TIMESTAMP)
                OR (status='running' AND lease_expires_at::timestamptz<=CURRENT_TIMESTAMP))
              AND attempts<max_attempts
         ), selected_tenant AS (
           SELECT eligible_tenants.community_id
             FROM eligible_tenants
             LEFT JOIN tenant_job_schedule USING (community_id)
            ORDER BY COALESCE(last_claim_sequence,0),eligible_tenants.community_id
            LIMIT 1
         ), candidate AS (
           SELECT id,community_id
             FROM processing_jobs
            WHERE stage=$1
              AND EXISTS (
                SELECT 1 FROM processing_job_ownership AS ownership
                 WHERE ownership.job_type=processing_jobs.job_type
                   AND ownership.owner_runtime='deno'
              )
              AND community_id=(SELECT community_id FROM selected_tenant)
              AND ((status IN ('pending','retry') AND available_at::timestamptz<=CURRENT_TIMESTAMP)
                OR (status='running' AND lease_expires_at::timestamptz<=CURRENT_TIMESTAMP))
              AND attempts<max_attempts
            ORDER BY priority,available_at,id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
         )
         UPDATE processing_jobs AS job
            SET status='running',attempts=job.attempts+1,
                claimed_at=CURRENT_TIMESTAMP,claimed_by=$2,
                lease_expires_at=CURRENT_TIMESTAMP+INTERVAL '2 minutes',
                completed_at=NULL,updated_at=CURRENT_TIMESTAMP
           FROM candidate
          WHERE job.id=candidate.id
        RETURNING job.*`,
        [normalizedStage, normalizedWorkerId],
      );
      if (!rows[0]) return null;
      const job = decodeJob(rows[0]);
      await connection.query(
        `INSERT INTO tenant_job_schedule(community_id,last_claim_sequence)
         VALUES ($1,(SELECT COALESCE(MAX(last_claim_sequence),0)+1 FROM tenant_job_schedule))
         ON CONFLICT(community_id) DO UPDATE SET
           last_claim_sequence=(SELECT COALESCE(MAX(last_claim_sequence),0)+1
                                  FROM tenant_job_schedule)`,
        [job.communityId],
      );
      return job;
    });
  }

  async complete(jobId: number, workerId: string): Promise<void> {
    const rows = await this.connection.query(
      `UPDATE processing_jobs
          SET status='completed',completed_at=CURRENT_TIMESTAMP,
              claimed_at=NULL,claimed_by=NULL,lease_expires_at=NULL,
              last_error=NULL,updated_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND status='running' AND claimed_by=$2 RETURNING id`,
      [jobId, workerId],
    );
    if (!rows[0]) {
      throw new TypeError(`Running processing job ${jobId} was not found`);
    }
  }

  async renewLease(
    jobId: number,
    workerId: string,
    leaseSeconds = 120,
  ): Promise<boolean> {
    const duration = Math.max(1, Math.min(3600, Math.trunc(leaseSeconds)));
    const rows = await this.connection.query(
      `UPDATE processing_jobs
          SET lease_expires_at=CURRENT_TIMESTAMP+($3*INTERVAL '1 second'),
              updated_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND status='running' AND claimed_by=$2
        RETURNING id`,
      [jobId, workerId, duration],
    );
    return Boolean(rows[0]);
  }

  async retry(
    jobId: number,
    workerId: string,
    error: string,
    delaySeconds?: number,
  ): Promise<boolean> {
    return await this.connection.transaction(async (connection) => {
      const row = (await connection.query(
        "SELECT community_id,observation_id,stage,payload_json,attempts,max_attempts FROM processing_jobs WHERE id=$1 AND status='running' AND claimed_by=$2 FOR UPDATE",
        [jobId, workerId],
      ))[0];
      if (!row) {
        throw new TypeError(`Running processing job ${jobId} was not found`);
      }
      const attempts = integer(row.attempts, "attempts");
      const maxAttempts = integer(row.max_attempts, "max_attempts");
      if (attempts >= maxAttempts) {
        await this.failWithin(connection, jobId, error, row);
        return false;
      }
      const delay = delaySeconds === undefined
        ? Math.min(300, 2 ** attempts)
        : Math.max(0, Math.trunc(delaySeconds));
      const rows = await connection.query(
        `UPDATE processing_jobs
            SET status='retry',available_at=CURRENT_TIMESTAMP+($2*INTERVAL '1 second'),
                claimed_at=NULL,claimed_by=NULL,lease_expires_at=NULL,
                completed_at=NULL,last_error=$3,updated_at=CURRENT_TIMESTAMP
          WHERE id=$1 AND status='running' AND claimed_by=$4 RETURNING id`,
        [jobId, delay, boundedError(error), workerId],
      );
      if (!rows[0]) {
        throw new TypeError(`Could not retry processing job ${jobId}`);
      }
      return true;
    });
  }

  async fail(jobId: number, workerId: string, error: string): Promise<void> {
    await this.connection.transaction(async (connection) => {
      await this.failWithin(connection, jobId, error, undefined, workerId);
    });
  }

  async recoverExpired(): Promise<number> {
    const rows = await this.connection.query(
      `WITH expired AS MATERIALIZED (
         SELECT id
           FROM processing_jobs
          WHERE status='running' AND lease_expires_at::timestamptz<=CURRENT_TIMESTAMP
          FOR UPDATE
       ), recovered AS (
         UPDATE processing_jobs AS job
            SET status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'retry' END,
                available_at=CURRENT_TIMESTAMP,claimed_at=NULL,claimed_by=NULL,
                lease_expires_at=NULL,last_error='Recovered after worker lease expired',
                completed_at=CASE WHEN attempts>=max_attempts THEN CURRENT_TIMESTAMP ELSE NULL END,
                updated_at=CURRENT_TIMESTAMP
           FROM expired
          WHERE job.id=expired.id
        RETURNING job.id,job.community_id,job.observation_id,job.stage,
                  job.status,job.payload_json
       ), dead_letters AS (
         INSERT INTO dead_letter_events(
           community_id,observation_id,processing_job_id,stage,
           error_class,error_message,payload_json
         )
         SELECT community_id,observation_id,id,stage,'LeaseExpired',
                'Recovered after worker lease expired',payload_json
           FROM recovered
          WHERE status='failed'
         RETURNING processing_job_id
       )
       SELECT recovered.id
         FROM recovered
         LEFT JOIN dead_letters ON dead_letters.processing_job_id=recovered.id`,
    );
    return rows.length;
  }

  async replayDeadLetter(
    deadLetterId: number,
    replayedAt = new Date(),
  ): Promise<number> {
    if (!Number.isSafeInteger(deadLetterId) || deadLetterId <= 0) {
      throw new TypeError("dead_letter_id is invalid");
    }
    return await this.connection.transaction(async (connection) => {
      const deadLetter = (await connection.query(
        `SELECT d.community_id,d.observation_id,d.stage,d.status,j.job_type,
                o.community_id AS observation_community_id
           FROM dead_letter_events d
           LEFT JOIN processing_jobs j ON j.id=d.processing_job_id
           LEFT JOIN observations o ON o.id=d.observation_id
          WHERE d.id=$1
          FOR UPDATE OF d`,
        [deadLetterId],
      ))[0];
      if (!deadLetter) {
        throw new TypeError(`dead letter ${deadLetterId} was not found`);
      }
      if (
        deadLetter.observation_id === null ||
        deadLetter.observation_id === undefined ||
        deadLetter.job_type === null ||
        deadLetter.job_type === undefined
      ) throw new TypeError("dead letter has no replayable observation");
      const communityId = integer(deadLetter.community_id, "community_id");
      const observationCommunityId = integer(
        deadLetter.observation_community_id,
        "observation_community_id",
      );
      if (communityId !== observationCommunityId) {
        throw new TypeError("dead letter tenant does not own observation");
      }
      const observationId = integer(
        deadLetter.observation_id,
        "observation_id",
      );
      const stage = String(deadLetter.stage);
      if (!isProcessingStage(stage)) {
        throw new TypeError("dead letter stage is invalid");
      }
      const timestamp = replayedAt.toISOString().replace("Z", "+00:00");
      const inserted = (await connection.query(
        `INSERT INTO processing_jobs(
           community_id,stage,job_type,observation_id,payload_json,
           priority,max_attempts,idempotency_key
         ) VALUES ($1,$2,$3,$4,'{}',100,5,$5)
         ON CONFLICT(idempotency_key) DO NOTHING RETURNING id`,
        [
          communityId,
          stage,
          String(deadLetter.job_type),
          observationId,
          `dead-letter:${deadLetterId}:${timestamp}`,
        ],
      ))[0];
      if (!inserted) {
        throw new TypeError("dead letter replay job already exists");
      }
      await consumeTenantQuota(connection, communityId, "jobs", 1, replayedAt);
      const updated = (await connection.query(
        `UPDATE dead_letter_events
            SET status='replayed',replayed_at=CURRENT_TIMESTAMP
          WHERE id=$1 RETURNING id`,
        [deadLetterId],
      ))[0];
      if (!updated) {
        throw new TypeError(`dead letter ${deadLetterId} was not found`);
      }
      return integer(inserted.id, "id");
    });
  }

  private async failWithin(
    connection: DatabaseConnection,
    jobId: number,
    error: string,
    lockedJob?: DatabaseRow,
    workerId?: string,
  ): Promise<void> {
    const job = lockedJob ?? (await connection.query(
      "SELECT community_id,observation_id,stage,payload_json FROM processing_jobs WHERE id=$1 AND status='running' AND claimed_by=$2 FOR UPDATE",
      [jobId, workerId ?? ""],
    ))[0];
    if (!job) {
      throw new TypeError(`Active processing job ${jobId} was not found`);
    }
    const rows = await connection.query(
      `UPDATE processing_jobs
          SET status='failed',claimed_at=NULL,claimed_by=NULL,
              lease_expires_at=NULL,completed_at=CURRENT_TIMESTAMP,
              last_error=$2,updated_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND status='running'${
        workerId ? " AND claimed_by=$3" : ""
      } RETURNING id`,
      workerId
        ? [jobId, boundedError(error), workerId]
        : [jobId, boundedError(error)],
    );
    if (!rows[0]) {
      throw new TypeError(`Active processing job ${jobId} was not found`);
    }
    await connection.query(
      `INSERT INTO dead_letter_events(
         community_id,observation_id,processing_job_id,stage,
         error_class,error_message,payload_json
       ) VALUES ($1,$2,$3,$4,'ProcessingError',$5,$6)`,
      [
        integer(job.community_id, "community_id"),
        job.observation_id === null || job.observation_id === undefined
          ? null
          : integer(job.observation_id, "observation_id"),
        jobId,
        String(job.stage),
        boundedError(error),
        typeof job.payload_json === "string"
          ? job.payload_json
          : JSON.stringify(job.payload_json ?? {}),
      ],
    );
  }
}

export class PermanentJobError extends Error {}
export class JobLeaseLostError extends Error {}
export type JobHandler = (job: ProcessingJob) => Promise<void>;

export class JobRegistry {
  private readonly handlers = new Map<string, JobHandler>();

  register(jobType: string, handler: JobHandler): this {
    const normalized = jobType.trim().toLocaleLowerCase();
    if (!normalized) throw new TypeError("job_type must not be empty");
    if (this.handlers.has(normalized)) {
      throw new TypeError(`job handler already registered: ${normalized}`);
    }
    this.handlers.set(normalized, handler);
    return this;
  }

  async dispatch(job: ProcessingJob): Promise<void> {
    const handler = this.handlers.get(job.jobType.toLocaleLowerCase());
    if (!handler) {
      throw new PermanentJobError(`unsupported job type: ${job.jobType}`);
    }
    await handler(job);
  }
}

export class ProcessingWorker {
  constructor(
    private readonly store: ProcessingJobStore,
    private readonly registry: JobRegistry,
    readonly stage: ProcessingStage,
    readonly workerId: string,
    readonly pollIntervalMs = 500,
    readonly recoveryIntervalMs = 60_000,
    readonly leaseRenewalIntervalMs = 40_000,
    readonly onError?: (error: unknown) => void,
  ) {}

  async processNext(): Promise<boolean> {
    const job = await this.store.claim(this.stage, this.workerId);
    if (!job) return false;
    const leaseController = new AbortController();
    let leaseFailure: unknown;
    const renewal = this.maintainLease(job.id, leaseController.signal).catch(
      (error) => leaseFailure = error,
    );
    let dispatchError: unknown;
    try {
      await this.registry.dispatch(job);
    } catch (error) {
      dispatchError = error;
    } finally {
      leaseController.abort();
      await renewal;
    }
    if (leaseFailure) throw leaseFailure;
    if (!dispatchError) {
      await this.store.complete(job.id, this.workerId);
      return true;
    }
    const message = dispatchError instanceof Error
      ? dispatchError.message
      : String(dispatchError);
    if (dispatchError instanceof PermanentJobError) {
      await this.store.fail(job.id, this.workerId, message);
    } else {
      await this.store.retry(job.id, this.workerId, message);
    }
    return true;
  }

  private async maintainLease(
    jobId: number,
    signal: AbortSignal,
  ): Promise<void> {
    while (!signal.aborted) {
      await abortableDelay(this.leaseRenewalIntervalMs, signal);
      if (signal.aborted) return;
      if (!await this.store.renewLease(jobId, this.workerId)) {
        throw new JobLeaseLostError(
          `Processing job ${jobId} lease ownership was lost`,
        );
      }
    }
  }

  async run(signal: AbortSignal): Promise<void> {
    let nextRecoveryAt = 0;
    while (!signal.aborted) {
      try {
        if (Date.now() >= nextRecoveryAt) {
          await this.store.recoverExpired();
          nextRecoveryAt = Date.now() + this.recoveryIntervalMs;
          if (signal.aborted) break;
        }
        const processed = await this.processNext();
        if (!processed) await abortableDelay(this.pollIntervalMs, signal);
      } catch (error) {
        this.onError?.(error);
        await abortableDelay(this.pollIntervalMs, signal);
      }
    }
  }
}

export class ProcessingWorkerPool {
  readonly workers: readonly ProcessingWorker[];

  constructor(
    store: ProcessingJobStore,
    registry: JobRegistry,
    stage: ProcessingStage,
    workerIdPrefix: string,
    concurrency: number,
    pollIntervalMs = 500,
    recoveryIntervalMs = 60_000,
    leaseRenewalIntervalMs = 40_000,
    onError?: (error: unknown) => void,
  ) {
    const normalizedPrefix = workerIdPrefix.trim();
    if (!normalizedPrefix) throw new TypeError("worker_id_prefix is required");
    if (!Number.isSafeInteger(concurrency) || concurrency < 1) {
      throw new TypeError("worker concurrency must be a positive integer");
    }
    this.workers = Object.freeze(Array.from(
      { length: concurrency },
      (_, index) =>
        new ProcessingWorker(
          store,
          registry,
          stage,
          `${normalizedPrefix}-${index + 1}`,
          pollIntervalMs,
          recoveryIntervalMs,
          leaseRenewalIntervalMs,
          onError,
        ),
    ));
  }

  async run(signal: AbortSignal): Promise<void> {
    await Promise.all(this.workers.map((worker) => worker.run(signal)));
  }
}

function decodeJob(row: DatabaseRow): ProcessingJob {
  const stage = String(row.stage);
  if (!isProcessingStage(stage)) {
    throw new TypeError("processing job stage is invalid");
  }
  let payload: unknown;
  try {
    payload = typeof row.payload_json === "string"
      ? JSON.parse(row.payload_json)
      : row.payload_json;
  } catch {
    throw new TypeError("processing job payload_json is invalid");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("processing job payload_json must be an object");
  }
  return Object.freeze({
    id: integer(row.id, "id"),
    communityId: integer(row.community_id, "community_id"),
    stage,
    jobType: String(row.job_type),
    observationId:
      row.observation_id === null || row.observation_id === undefined
        ? null
        : integer(row.observation_id, "observation_id"),
    payload: Object.freeze(payload as Record<string, unknown>),
    attempts: integer(row.attempts, "attempts"),
    maxAttempts: integer(row.max_attempts, "max_attempts"),
  });
}

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) {
    throw new TypeError(`${name} must be an integer`);
  }
  return parsed;
}

function boundedError(error: string): string {
  return error.trim().slice(0, 2000);
}

function isProcessingStage(value: string): value is ProcessingStage {
  return value === "analysis" || value === "action";
}

function abortableDelay(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(done, milliseconds);
    signal.addEventListener("abort", done, { once: true });
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}
