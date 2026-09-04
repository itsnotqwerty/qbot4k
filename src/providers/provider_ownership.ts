import type { DatabaseConnection } from "../data/database.ts";

export type ProviderRuntime = "python" | "deno" | "none";

export interface ProviderOwnershipLease {
  installations(platform: "discord" | "twitch"): Promise<readonly number[]>;
  acquire(
    installationId: number,
    holder: string,
    leaseSeconds?: number,
  ): Promise<boolean>;
  renew(
    installationId: number,
    holder: string,
    leaseSeconds?: number,
  ): Promise<boolean>;
  release(installationId: number, holder: string): Promise<boolean>;
  owns(installationId: number, holder: string): Promise<boolean>;
  active(installationId: number): Promise<boolean>;
  releaseAll(holder: string): Promise<number>;
}

export class PostgresProviderOwnershipLease implements ProviderOwnershipLease {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly runtime: ProviderRuntime = "deno",
  ) {}

  async installations(platform: "discord" | "twitch"): Promise<readonly number[]> {
    const rows = await this.connection.query(
      `SELECT installation.id
         FROM community_installations AS installation
         JOIN installation_runtime_leases AS lease
           ON lease.installation_id=installation.id
          AND lease.owner_runtime=$2
        WHERE installation.platform=$1 AND installation.status='active'
        ORDER BY installation.id`,
      [platform, this.runtime],
    );
    return Object.freeze(rows.map((row) => Number(row.id)));
  }

  async acquire(
    installationId: number,
    holder: string,
    leaseSeconds = 120,
  ): Promise<boolean> {
    const lease = validate(installationId, holder, leaseSeconds);
    return await this.connection.transaction(async (connection) => {
      await connection.query(
        `INSERT INTO installation_runtime_leases(installation_id,owner_runtime)
         VALUES ($1,'python') ON CONFLICT(installation_id) DO NOTHING`,
        [lease.installationId],
      );
      const row = (await connection.query(
        `UPDATE installation_runtime_leases SET
           lease_holder=$2,lease_acquired_at=CURRENT_TIMESTAMP,
           lease_expires_at=CURRENT_TIMESTAMP+($3::text||' seconds')::interval,
           last_renewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
         WHERE installation_id=$1 AND owner_runtime=$4
           AND (lease_holder IS NULL OR lease_holder=$2
             OR lease_expires_at::timestamptz<=CURRENT_TIMESTAMP)
         RETURNING installation_id`,
        [lease.installationId, lease.holder, lease.seconds, this.runtime],
      ))[0];
      return Boolean(row);
    });
  }

  async renew(
    installationId: number,
    holder: string,
    leaseSeconds = 120,
  ): Promise<boolean> {
    const lease = validate(installationId, holder, leaseSeconds);
    const row = (await this.connection.query(
      `UPDATE installation_runtime_leases SET
         lease_expires_at=CURRENT_TIMESTAMP+($3::text||' seconds')::interval,
         last_renewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
       WHERE installation_id=$1 AND owner_runtime=$4 AND lease_holder=$2
         AND lease_expires_at::timestamptz>CURRENT_TIMESTAMP
       RETURNING installation_id`,
      [lease.installationId, lease.holder, lease.seconds, this.runtime],
    ))[0];
    return Boolean(row);
  }

  async release(installationId: number, holder: string): Promise<boolean> {
    const lease = validateIdentity(installationId, holder);
    const row = (await this.connection.query(
      `UPDATE installation_runtime_leases SET lease_holder=NULL,
         lease_acquired_at=NULL,lease_expires_at=NULL,last_renewed_at=NULL,
         updated_at=CURRENT_TIMESTAMP
       WHERE installation_id=$1 AND owner_runtime=$3 AND lease_holder=$2
       RETURNING installation_id`,
      [lease.installationId, lease.holder, this.runtime],
    ))[0];
    return Boolean(row);
  }

  async owns(installationId: number, holder: string): Promise<boolean> {
    const lease = validateIdentity(installationId, holder);
    const row = (await this.connection.query(
      `SELECT 1 FROM installation_runtime_leases
        WHERE installation_id=$1 AND owner_runtime=$3 AND lease_holder=$2
          AND lease_expires_at::timestamptz>CURRENT_TIMESTAMP`,
      [lease.installationId, lease.holder, this.runtime],
    ))[0];
    return Boolean(row);
  }

  async active(installationId: number): Promise<boolean> {
    if (!Number.isSafeInteger(installationId) || installationId < 1) {
      throw new TypeError("installation_id must be a positive integer");
    }
    return Boolean((await this.connection.query(
      `SELECT 1 FROM installation_runtime_leases
        WHERE installation_id=$1 AND owner_runtime=$2
          AND lease_holder IS NOT NULL
          AND lease_expires_at::timestamptz>CURRENT_TIMESTAMP`,
      [installationId, this.runtime],
    ))[0]);
  }

  async releaseAll(holder: string): Promise<number> {
    const normalizedHolder = holder.trim();
    if (!normalizedHolder) throw new TypeError("lease holder must not be empty");
    const rows = await this.connection.query(
      `UPDATE installation_runtime_leases SET lease_holder=NULL,
         lease_acquired_at=NULL,lease_expires_at=NULL,last_renewed_at=NULL,
         updated_at=CURRENT_TIMESTAMP
       WHERE owner_runtime=$2 AND lease_holder=$1 RETURNING installation_id`,
      [normalizedHolder, this.runtime],
    );
    return rows.length;
  }
}

function validate(
  installationId: number,
  holder: string,
  leaseSeconds: number,
): { installationId: number; holder: string; seconds: number } {
  const identity = validateIdentity(installationId, holder);
  if (
    !Number.isSafeInteger(leaseSeconds) || leaseSeconds < 10 ||
    leaseSeconds > 3600
  ) {
    throw new TypeError("lease seconds must be between 10 and 3600");
  }
  return { ...identity, seconds: leaseSeconds };
}

function validateIdentity(
  installationId: number,
  holder: string,
): { installationId: number; holder: string } {
  if (!Number.isSafeInteger(installationId) || installationId < 1) {
    throw new TypeError("installation_id must be a positive integer");
  }
  const normalizedHolder = holder.trim();
  if (!normalizedHolder) throw new TypeError("lease holder must not be empty");
  return { installationId, holder: normalizedHolder };
}
