import postgres from "postgres";
import type { AppSettings } from "../core/config.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "./database.ts";

export const MIGRATION_LOCK_ID = 7_324_680_441;
export const MIGRATION_NAMES = Object.freeze([
  "processing job leases",
  "canonical identity attribution",
  "intelligence platform P0-P3",
  "community tenancy and professional operations",
  "real-time stream operations and incident command",
  "Discord installation authorization intents",
  "legacy schema convergence",
  "tenant announcement delivery",
  "community onboarding controls",
  "processing lease recovery",
  "identity unlink attribution",
  "moderation provider confirmation",
  "community analytics isolation",
  "operator access lifecycle",
  "strict tenant ownership",
  "provider installation attribution",
  "encrypted Twitch installation onboarding",
  "API client community ownership",
  "community runtime policy",
  "community anti-abuse policy",
  "legacy Twitch onboarding convergence",
  "data subject request community ownership",
  "community profile settings",
  "saved moderation filters",
  "moderation rule lifecycle",
  "tenant slo samples",
  "tenant quotas and fair jobs",
  "provider installation ownership leases",
]);

export interface AuditCheck {
  readonly key: string;
  readonly status: "pass" | "warn" | "fail";
  readonly detail: string;
}

export interface PlatformAudit {
  readonly status: "pass" | "warn" | "fail";
  readonly checks: readonly AuditCheck[];
}

export type JobRuntime = "python" | "deno" | "none";

export class OperationalService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly schemaSql: string,
  ) {}

  async migrate(): Promise<readonly string[]> {
    await this.connection.transaction(async (connection) => {
      await connection.query("SELECT pg_advisory_xact_lock($1)", [
        MIGRATION_LOCK_ID,
      ]);
      await validateMigrationState(connection);
      await connection.query(this.schemaSql);
      for (const [index, name] of MIGRATION_NAMES.entries()) {
        await connection.query(
          `INSERT INTO schema_migrations(version,name)
           VALUES ($1,$2) ON CONFLICT(version) DO NOTHING`,
          [index + 1, name],
        );
      }
      await connection.query(
        `INSERT INTO organizations(id,name,slug)
         VALUES (1,'Default Organization','default') ON CONFLICT(id) DO NOTHING`,
      );
      await connection.query(
        `INSERT INTO workspaces(id,organization_id,name,slug)
         VALUES (1,1,'Default Workspace','default') ON CONFLICT(id) DO NOTHING`,
      );
      await connection.query(
        `INSERT INTO communities(id,workspace_id,name,slug)
         VALUES (1,1,'Default Community','default') ON CONFLICT(id) DO NOTHING`,
      );
      await connection.query(
        `INSERT INTO community_policy_settings(community_id)
         VALUES (1) ON CONFLICT(community_id) DO NOTHING`,
      );
      for (const table of ["organizations", "workspaces", "communities"]) {
        await connection.query(
          `SELECT setval(pg_get_serial_sequence('${table}','id'),
                  GREATEST((SELECT COALESCE(MAX(id),1) FROM ${table}),1),true)`,
        );
      }
    });
    return await this.listTables();
  }

  async listTables(): Promise<readonly string[]> {
    const rows = await this.connection.query(
      `SELECT table_name FROM information_schema.tables
       WHERE table_schema=current_schema() AND table_type='BASE TABLE'
       ORDER BY table_name`,
    );
    return Object.freeze(rows.map((row) => String(row.table_name)));
  }

  async audit(settings: AppSettings): Promise<PlatformAudit> {
    const checks: AuditCheck[] = [];
    const existing = new Set(await this.listTables());
    const required = new Set(schemaTables(this.schemaSql));
    const missing = [...required].filter((table) => !existing.has(table))
      .sort();
    checks.push({
      key: "schema",
      status: missing.length ? "fail" : "pass",
      detail: missing.length
        ? `missing: ${missing.join(", ")}`
        : "professional schema present",
    });
    checks.push(
      await countCheck(
        this.connection,
        "tenancy",
        "SELECT COUNT(*) AS count FROM communities WHERE status='active'",
        "active communities",
      ),
    );
    checks.push(
      await countCheck(
        this.connection,
        "scoped_roles",
        "SELECT COUNT(*) AS count FROM operator_community_roles",
        "community role grants",
        true,
      ),
    );
    checks.push({
      key: "eventsub",
      status:
        settings.twitchEventsubSecret && settings.twitchEventsubCallbackUrl
          ? "pass"
          : "warn",
      detail:
        settings.twitchEventsubSecret && settings.twitchEventsubCallbackUrl
          ? "webhook signing configured"
          : "set QBOT_TWITCH_EVENTSUB_SECRET and QBOT_TWITCH_EVENTSUB_CALLBACK_URL",
    });
    const models = await this.connection.query(
      "SELECT model_key,status FROM model_registry ORDER BY model_key",
    );
    const unapproved = models.filter((row) => String(row.status) !== "active")
      .map((row) => String(row.model_key));
    checks.push({
      key: "model_governance",
      status: unapproved.length ? "warn" : "pass",
      detail: unapproved.length
        ? `shadow/paused: ${unapproved.join(", ")}`
        : "all models active",
    });
    checks.push(
      await zeroCheck(
        this.connection,
        "raw_archive",
        "SELECT COUNT(*) AS count FROM raw_event_archive WHERE archive_path IS NULL",
        "events awaiting archive flush",
      ),
    );
    checks.push(
      await zeroCheck(
        this.connection,
        "dead_letters",
        "SELECT COUNT(*) AS count FROM dead_letter_events WHERE status='open'",
        "open dead letters",
      ),
    );
    checks.push(await this.auditJobOwnership());
    const status = checks.some((check) => check.status === "fail")
      ? "fail"
      : checks.some((check) => check.status === "warn")
      ? "warn"
      : "pass";
    return Object.freeze({ status, checks: Object.freeze(checks) });
  }

  async auditJobOwnership(): Promise<AuditCheck> {
    const rows = await this.connection.query(
      `SELECT DISTINCT job.job_type,
              COALESCE(ownership.owner_runtime,'missing') AS owner_runtime
         FROM processing_jobs AS job
         LEFT JOIN processing_job_ownership AS ownership
           ON ownership.job_type=job.job_type
        WHERE ownership.owner_runtime IS DISTINCT FROM 'deno'
        ORDER BY job.job_type`,
    );
    const blocked = rows.map((row) =>
      `${String(row.job_type)}=${String(row.owner_runtime)}`
    );
    return Object.freeze({
      key: "job_ownership",
      status: blocked.length ? "fail" : "pass",
      detail: blocked.length
        ? `not Deno-owned: ${blocked.join(", ")}`
        : "all observed job types owned by Deno",
    });
  }

  async issuePilotInvite(
    communityId: number,
    expiresHours = 72,
    operatorId?: number,
    now = new Date(),
  ): Promise<string> {
    if (!Number.isSafeInteger(communityId) || communityId <= 0) {
      throw new TypeError("community_id must be a positive integer");
    }
    if (
      !Number.isSafeInteger(expiresHours) || expiresHours < 1 ||
      expiresHours > 720
    ) {
      throw new TypeError("--expires-hours must be between 1 and 720");
    }
    if (
      operatorId !== undefined &&
      (!Number.isSafeInteger(operatorId) || operatorId <= 0)
    ) {
      throw new TypeError("operator_id must be a positive integer");
    }
    const code = tokenUrlSafe(24);
    const codeHash = await sha256(code);
    const expiresAt = new Date(now.valueOf() + expiresHours * 3_600_000)
      .toISOString();
    await this.connection.transaction(async (connection) => {
      const inserted = (await connection.query(
        `INSERT INTO pilot_invitations(
           community_id,code_hash,expires_at,created_by_operator_id
         ) VALUES ($1,$2,$3,$4) RETURNING id`,
        [communityId, codeHash, expiresAt, operatorId ?? null],
      ))[0];
      if (!inserted) throw new TypeError("pilot invitation was not created");
      await connection.query(
        `INSERT INTO audit_log(
           actor_type,actor_id,action_type,entity_type,entity_id,payload_json
         ) VALUES ($1,$2,'pilot.invitation_issued','pilot_invitation',$3,$4)`,
        [
          operatorId === undefined ? "system" : "operator",
          operatorId ?? null,
          Number(inserted.id),
          JSON.stringify({ community_id: communityId, expires_at: expiresAt }),
        ],
      );
    });
    return code;
  }

  async transferJobOwnership(
    jobType: string,
    ownerRuntime: JobRuntime,
    shadowRuntime: Exclude<JobRuntime, "none"> | null,
    operatorId?: number,
  ): Promise<void> {
    const normalizedJobType = jobType.trim();
    if (!normalizedJobType) throw new TypeError("job_type must not be empty");
    if (!["python", "deno", "none"].includes(ownerRuntime)) {
      throw new TypeError("owner_runtime must be python, deno, or none");
    }
    if (shadowRuntime === ownerRuntime) {
      throw new TypeError("shadow_runtime must differ from owner_runtime");
    }
    await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtext('qbot4k:job-owner:' || $1))",
        [normalizedJobType],
      );
      const running = (await connection.query(
        `SELECT COUNT(*) AS count FROM processing_jobs
          WHERE job_type=$1 AND status='running'`,
        [normalizedJobType],
      ))[0];
      if (Number(running?.count ?? 0) !== 0) {
        throw new TypeError(
          "active job leases must drain before ownership transfer",
        );
      }
      const previous = (await connection.query(
        `SELECT owner_runtime,shadow_runtime FROM processing_job_ownership
          WHERE job_type=$1 FOR UPDATE`,
        [normalizedJobType],
      ))[0];
      await connection.query(
        `INSERT INTO processing_job_ownership(
           job_type,owner_runtime,shadow_runtime,updated_at
         ) VALUES ($1,$2,$3,CURRENT_TIMESTAMP)
         ON CONFLICT(job_type) DO UPDATE SET
           owner_runtime=EXCLUDED.owner_runtime,
           shadow_runtime=EXCLUDED.shadow_runtime,
           updated_at=CURRENT_TIMESTAMP`,
        [normalizedJobType, ownerRuntime, shadowRuntime],
      );
      await connection.query(
        `INSERT INTO audit_log(
           actor_type,actor_id,action_type,entity_type,entity_id,payload_json
         ) VALUES ($1,$2,'processing_job.ownership_transferred',
                   'processing_job_type',NULL,$3)`,
        [
          operatorId === undefined ? "system" : "operator",
          operatorId ?? null,
          JSON.stringify({
            job_type: normalizedJobType,
            previous_owner: previous?.owner_runtime ?? "python",
            owner_runtime: ownerRuntime,
            shadow_runtime: shadowRuntime,
          }),
        ],
      );
    });
  }

  async transferInstallationOwnership(
    installationId: number,
    ownerRuntime: JobRuntime,
    operatorId?: number,
  ): Promise<void> {
    if (!Number.isSafeInteger(installationId) || installationId < 1) {
      throw new TypeError("installation_id must be a positive integer");
    }
    await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtext('qbot4k:installation-owner:' || $1))",
        [String(installationId)],
      );
      const installation = (await connection.query(
        `SELECT installation.platform,lease.owner_runtime,lease.lease_holder,
                lease.lease_expires_at
           FROM community_installations installation
           LEFT JOIN installation_runtime_leases lease
             ON lease.installation_id=installation.id
          WHERE installation.id=$1 FOR UPDATE OF installation`,
        [installationId],
      ))[0];
      if (!installation) throw new TypeError("installation not found");
      if (
        installation.lease_holder && installation.lease_expires_at &&
        new Date(String(installation.lease_expires_at)) > new Date()
      ) {
        throw new TypeError(
          "active provider lease must drain before ownership transfer",
        );
      }
      await connection.query(
        `INSERT INTO installation_runtime_leases(installation_id,owner_runtime)
         VALUES ($1,$2) ON CONFLICT(installation_id) DO UPDATE SET
           owner_runtime=EXCLUDED.owner_runtime,lease_holder=NULL,
           lease_acquired_at=NULL,lease_expires_at=NULL,last_renewed_at=NULL,
           updated_at=CURRENT_TIMESTAMP`,
        [installationId, ownerRuntime],
      );
      await connection.query(
        `INSERT INTO audit_log(
           actor_type,actor_id,action_type,entity_type,entity_id,payload_json
         ) VALUES ($1,$2,'provider_installation.ownership_transferred',
                   'community_installation',$3,$4)`,
        [
          operatorId === undefined ? "system" : "operator",
          operatorId ?? null,
          installationId,
          JSON.stringify({
            platform: String(installation.platform),
            previous_owner: installation.owner_runtime ?? "python",
            owner_runtime: ownerRuntime,
          }),
        ],
      );
    });
  }
}

export async function withOperationalService<T>(
  databaseUrl: string,
  callback: (service: OperationalService) => Promise<T>,
): Promise<T> {
  const sql = postgres(databaseUrl, { max: 1, transform: { undefined: null } });
  const connection: DatabaseConnection = {
    query: async (statement, parameters: readonly DatabaseParameter[] = []) =>
      (await sql.unsafe(statement, [...parameters])).map((row) =>
        Object.freeze({ ...row }) as DatabaseRow
      ),
    transaction: <R>(
      operation: (connection: DatabaseConnection) => Promise<R>,
    ) =>
      sql.begin(async (transaction) => {
        const scoped: DatabaseConnection = {
          query: async (
            statement,
            parameters: readonly DatabaseParameter[] = [],
          ) =>
            (await transaction.unsafe(statement, [...parameters])).map((row) =>
              Object.freeze({ ...row }) as DatabaseRow
            ),
          transaction: (nested) => nested(scoped),
        };
        return await operation(scoped);
      }) as Promise<R>,
  };
  try {
    const schemaSql = await Deno.readTextFile(
      new URL("../postgres_schema.sql", import.meta.url),
    );
    return await callback(new OperationalService(connection, schemaSql));
  } finally {
    await sql.end();
  }
}

async function validateMigrationState(
  connection: DatabaseConnection,
): Promise<void> {
  const ownership = (await connection.query(
    `SELECT nspowner=(SELECT usesysid FROM pg_user WHERE usename=current_user) AS owns_schema
     FROM pg_namespace WHERE nspname=current_schema()`,
  ))[0];
  if (!ownership || ownership.owns_schema !== true) {
    throw new TypeError("PostgreSQL migration role must own the target schema");
  }
  const registry = (await connection.query(
    "SELECT to_regclass(current_schema() || '.schema_migrations') AS registry",
  ))[0];
  if (!registry?.registry) {
    const count = (await connection.query(
      `SELECT COUNT(*) AS count FROM information_schema.tables
       WHERE table_schema=current_schema() AND table_type='BASE TABLE'`,
    ))[0];
    if (Number(count?.count ?? 0) !== 0) {
      throw new TypeError("PostgreSQL schema contains unmanaged tables");
    }
    return;
  }
  const applied = await connection.query(
    "SELECT version,name FROM schema_migrations ORDER BY version",
  );
  const compatible = applied.every((row, index) =>
    Number(row.version) === index + 1 &&
    String(row.name) === MIGRATION_NAMES[index]
  );
  if (!compatible || applied.length > MIGRATION_NAMES.length) {
    throw new TypeError("PostgreSQL migration history is incompatible");
  }
}

function schemaTables(schemaSql: string): readonly string[] {
  return [
    ...schemaSql.matchAll(/CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)/gu),
  ]
    .map((match) => match[1]);
}

async function countCheck(
  connection: DatabaseConnection,
  key: string,
  sql: string,
  label: string,
  warnWhenZero = false,
): Promise<AuditCheck> {
  const count = Number((await connection.query(sql))[0]?.count ?? 0);
  return {
    key,
    status: warnWhenZero && count === 0 ? "warn" : "pass",
    detail: `${count} ${label}`,
  };
}

async function zeroCheck(
  connection: DatabaseConnection,
  key: string,
  sql: string,
  label: string,
): Promise<AuditCheck> {
  const count = Number((await connection.query(sql))[0]?.count ?? 0);
  return {
    key,
    status: count === 0 ? "pass" : "warn",
    detail: `${count} ${label}`,
  };
}

function tokenUrlSafe(length: number): string {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-")
    .replaceAll("/", "_").replace(/=+$/u, "");
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}
