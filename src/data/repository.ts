import { ActorAttribution, TenantContext } from "../core/contexts.ts";
import type { DatabaseConnection, DatabaseRow } from "./database.ts";
import { FernetCipher } from "../security/fernet.ts";
import type {
  DiscordIdentity,
  OperatorAuthStore,
  OperatorLogin,
  OperatorMembership,
} from "../web/web_auth.ts";

export interface CommunityRecord {
  readonly id: number;
  readonly workspaceId: number;
  readonly name: string;
  readonly slug: string;
  readonly status: string;
  readonly timezone: string;
  readonly locale: string;
  readonly description: string;
  readonly notificationsEnabled: boolean;
  readonly retentionDays: number;
}

export interface InstallationRecord {
  readonly id: number;
  readonly communityId: number;
  readonly platform: string;
  readonly externalCommunityId: string;
  readonly displayName: string;
  readonly status: string;
  readonly scopes: readonly string[];
  readonly metadata: Readonly<Record<string, unknown>>;
  readonly capabilities: readonly string[];
  readonly healthStatus: string;
  readonly tokenReference: string | null;
}

export interface InstallationCredentials {
  readonly accessToken: string;
  readonly refreshToken: string | null;
  readonly scopes: readonly string[];
  readonly keyVersion: number;
  readonly rotationCount: number;
}

function integer(row: DatabaseRow, field: string): number {
  const value = Number(row[field]);
  if (!Number.isInteger(value)) {
    throw new TypeError(`${field} must be an integer`);
  }
  return value;
}

function jsonArray(row: DatabaseRow, field: string): readonly string[] {
  const value = typeof row[field] === "string"
    ? JSON.parse(row[field])
    : row[field];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new TypeError(`${field} must be a JSON string array`);
  }
  return Object.freeze([...value]);
}

function jsonObject(
  row: DatabaseRow,
  field: string,
): Readonly<Record<string, unknown>> {
  const value = typeof row[field] === "string"
    ? JSON.parse(row[field])
    : row[field];
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${field} must be a JSON object`);
  }
  return Object.freeze({ ...value as Record<string, unknown> });
}

export function decodeCommunity(
  row: DatabaseRow,
  tenant: TenantContext,
): CommunityRecord {
  const id = integer(row, "id");
  if (id !== tenant.communityId) {
    throw new TypeError("community_id does not match tenant context");
  }
  return Object.freeze({
    id,
    workspaceId: integer(row, "workspace_id"),
    name: String(row.name),
    slug: String(row.slug),
    status: String(row.status),
    timezone: String(row.timezone),
    locale: String(row.locale),
    description: String(row.description),
    notificationsEnabled: Boolean(row.notifications_enabled),
    retentionDays: integer(row, "retention_days"),
  });
}

export function decodeInstallation(
  row: DatabaseRow,
  tenant: TenantContext,
): InstallationRecord {
  const communityId = integer(row, "community_id");
  if (communityId !== tenant.communityId) {
    throw new TypeError("community_id does not match tenant context");
  }
  const id = integer(row, "id");
  if (tenant.installationId !== null && id !== tenant.installationId) {
    throw new TypeError("installation_id does not match tenant context");
  }
  return Object.freeze({
    id,
    communityId,
    platform: String(row.platform),
    externalCommunityId: String(row.external_community_id),
    displayName: String(row.display_name),
    status: String(row.status),
    scopes: jsonArray(row, "scopes_json"),
    metadata: jsonObject(row, "metadata_json"),
    capabilities: jsonArray(row, "capabilities_json"),
    healthStatus: String(row.health_status),
    tokenReference:
      row.token_reference === null || row.token_reference === undefined
        ? null
        : String(row.token_reference),
  });
}

export class ScopedRepository {
  constructor(
    private readonly connection: DatabaseConnection,
    readonly tenant: TenantContext,
  ) {}

  async transaction<T>(
    callback: (repository: ScopedRepository) => Promise<T>,
  ): Promise<T> {
    return await this.connection.transaction(async (transaction) => {
      return await callback(new ScopedRepository(transaction, this.tenant));
    });
  }

  async findCommunityInOrganization(
    organizationId: number,
  ): Promise<CommunityRecord | null> {
    const rows = await this.connection.query(
      `SELECT c.id, c.workspace_id, c.name, c.slug, c.status, c.timezone,
              c.locale, c.description, c.notifications_enabled, c.retention_days
         FROM communities AS c
         JOIN workspaces AS w ON w.id = c.workspace_id
        WHERE w.organization_id = $1 AND c.id = $2`,
      [organizationId, this.tenant.communityId],
    );
    return rows[0] ? decodeCommunity(rows[0], this.tenant) : null;
  }

  async findInstallation(
    platform: string,
    externalCommunityId: string,
  ): Promise<InstallationRecord | null> {
    const parameters: (string | number)[] = [
      this.tenant.communityId,
      platform,
      externalCommunityId,
    ];
    let installationClause = "";
    if (this.tenant.installationId !== null) {
      parameters.push(this.tenant.installationId);
      installationClause = " AND id = $4";
    }
    const rows = await this.connection.query(
      `SELECT id, community_id, platform, external_community_id, display_name,
              status, scopes_json, metadata_json, capabilities_json,
              health_status, token_reference
         FROM community_installations
        WHERE community_id = $1 AND platform = $2 AND external_community_id = $3${installationClause}`,
      parameters,
    );
    return rows[0] ? decodeInstallation(rows[0], this.tenant) : null;
  }

  async loadInstallationCredentials(
    encryptionKey: string,
  ): Promise<InstallationCredentials> {
    if (this.tenant.installationId === null) {
      throw new TypeError("installation_id is required");
    }
    const rows = await this.connection.query(
      `SELECT c.access_token_ciphertext, c.refresh_token_ciphertext, c.scopes_json,
              c.key_version, c.rotation_count
         FROM installation_credentials AS c
         JOIN community_installations AS i ON i.id = c.installation_id
        WHERE c.installation_id = $1 AND i.community_id = $2
          AND i.status IN ('pending', 'active', 'degraded')`,
      [this.tenant.installationId, this.tenant.communityId],
    );
    const row = rows[0];
    if (!row) throw new TypeError("installation credentials not found");
    const tokenText = (value: unknown): string => {
      if (value instanceof Uint8Array) return new TextDecoder().decode(value);
      return String(value);
    };
    const cipher = await FernetCipher.fromKey(encryptionKey);
    const accessToken = await cipher.decrypt(
      tokenText(row.access_token_ciphertext),
    );
    const refreshToken = row.refresh_token_ciphertext === null ||
        row.refresh_token_ciphertext === undefined
      ? null
      : await cipher.decrypt(tokenText(row.refresh_token_ciphertext));
    return Object.freeze({
      accessToken,
      refreshToken,
      scopes: jsonArray(row, "scopes_json"),
      keyVersion: integer(row, "key_version"),
      rotationCount: integer(row, "rotation_count"),
    });
  }

  async storeInstallationCredentials(input: {
    accessToken: string;
    refreshToken?: string | null;
    scopes: readonly string[];
    encryptionKey: string;
    keyVersion?: number;
    actorOperatorId: number;
  }): Promise<string> {
    if (this.tenant.installationId === null) {
      throw new TypeError("installation_id is required");
    }
    const accessToken = input.accessToken.trim();
    if (!accessToken) throw new TypeError("access_token is required");
    const actor = new ActorAttribution("operator", input.actorOperatorId);
    const scopes = [
      ...new Set(input.scopes.map((scope) => scope.trim()).filter(Boolean)),
    ].sort();
    const cipher = await FernetCipher.fromKey(input.encryptionKey);
    const encryptedAccess = new TextEncoder().encode(
      await cipher.encrypt(accessToken),
    );
    const encryptedRefresh = input.refreshToken?.trim()
      ? new TextEncoder().encode(
        await cipher.encrypt(input.refreshToken.trim()),
      )
      : null;
    const keyVersion = input.keyVersion ?? 1;

    return await this.transaction(async (repository) => {
      const installations = await repository.connection.query(
        `SELECT status FROM community_installations
          WHERE id = $1 AND community_id = $2 FOR UPDATE`,
        [repository.tenant.installationId!, repository.tenant.communityId],
      );
      if (!installations[0] || String(installations[0].status) === "revoked") {
        throw new TypeError("installation not found");
      }
      const credentials = await repository.connection.query(
        `INSERT INTO installation_credentials(
           installation_id, access_token_ciphertext, refresh_token_ciphertext,
           scopes_json, key_version, rotation_count)
         VALUES ($1, $2, $3, $4, $5, 1)
         ON CONFLICT(installation_id) DO UPDATE SET
           access_token_ciphertext = EXCLUDED.access_token_ciphertext,
           refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
           scopes_json = EXCLUDED.scopes_json,
           key_version = EXCLUDED.key_version,
           rotation_count = installation_credentials.rotation_count + 1,
           rotated_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
         RETURNING id, rotation_count`,
        [
          repository.tenant.installationId!,
          encryptedAccess,
          encryptedRefresh,
          JSON.stringify(scopes),
          keyVersion,
        ],
      );
      if (!credentials[0]) {
        throw new TypeError("installation credentials were not stored");
      }
      const credentialId = integer(credentials[0], "id");
      const rotationCount = integer(credentials[0], "rotation_count");
      const reference = `installation-credential:${credentialId}`;
      await repository.connection.query(
        `UPDATE community_installations
            SET token_reference = $1, scopes_json = $2, updated_at = CURRENT_TIMESTAMP
          WHERE id = $3 AND community_id = $4`,
        [
          reference,
          JSON.stringify(scopes),
          repository.tenant.installationId!,
          repository.tenant.communityId,
        ],
      );
      await repository.connection.query(
        `INSERT INTO audit_log(
           actor_type, actor_id, action_type, entity_type, entity_id, payload_json)
         VALUES ('operator', $1, 'integration.credentials_rotated',
                 'community_installation', $2, $3)`,
        [
          actor.actorId!,
          repository.tenant.installationId!,
          JSON.stringify({
            community_id: repository.tenant.communityId,
            key_version: keyVersion,
            rotation_count: rotationCount,
            scopes,
          }),
        ],
      );
      return reference;
    });
  }
}

function decodeMembership(row: DatabaseRow): OperatorMembership {
  return Object.freeze({
    id: integer(row, "id"),
    name: String(row.name),
    slug: String(row.slug),
    role: String(row.role),
  });
}

export class OperatorAuthRepository implements OperatorAuthStore {
  constructor(private readonly connection: DatabaseConnection) {}

  async completeLogin(
    identity: DiscordIdentity,
    role: string,
  ): Promise<OperatorLogin> {
    return await this.connection.transaction(async (connection) => {
      const existing = await connection.query(
        "SELECT id FROM operator_accounts WHERE discord_user_id = $1",
        [identity.userId],
      );
      const accounts = await connection.query(
        `INSERT INTO operator_accounts(discord_user_id, discord_username, role)
         VALUES ($1, $2, $3)
         ON CONFLICT(discord_user_id) DO UPDATE SET
           discord_username = EXCLUDED.discord_username,
           role = EXCLUDED.role,
           updated_at = CURRENT_TIMESTAMP
         RETURNING id, status, session_version`,
        [identity.userId, identity.username, role],
      );
      const account = accounts[0];
      if (!account) {
        throw new TypeError("Failed to resolve operator account after upsert");
      }
      const operatorId = integer(account, "id");
      if (!existing[0]) {
        await connection.query(
          `INSERT INTO operator_community_roles(operator_id, community_id, role)
           VALUES ($1, 1, $2)
           ON CONFLICT(operator_id, community_id) DO UPDATE SET role = EXCLUDED.role`,
          [
            operatorId,
            role.toLocaleLowerCase() === "admin"
              ? "owner"
              : role.toLocaleLowerCase(),
          ],
        );
      }

      await connection.query(
        `UPDATE operator_invitations SET status = 'expired'
          WHERE target_discord_user_id = $1 AND status = 'pending'
            AND expires_at::timestamptz <= CURRENT_TIMESTAMP`,
        [identity.userId],
      );
      const invitations = await connection.query(
        `SELECT id, community_id, invited_role
           FROM operator_invitations
          WHERE target_discord_user_id = $1 AND status = 'pending'
            AND expires_at::timestamptz > CURRENT_TIMESTAMP ORDER BY id`,
        [identity.userId],
      );
      for (const invitation of invitations) {
        const communityId = integer(invitation, "community_id");
        const invitationId = integer(invitation, "id");
        const invitedRole = String(invitation.invited_role);
        await connection.query(
          "UPDATE operator_accounts SET status = 'active', session_version = session_version + 1 WHERE id = $1",
          [operatorId],
        );
        await connection.query(
          `INSERT INTO operator_community_roles(operator_id, community_id, role)
           VALUES ($1, $2, $3)
           ON CONFLICT(operator_id, community_id) DO UPDATE SET role = EXCLUDED.role`,
          [operatorId, communityId, invitedRole],
        );
        await connection.query(
          `UPDATE operator_invitations SET status = 'accepted',
             accepted_by_operator_id = $1, accepted_at = CURRENT_TIMESTAMP WHERE id = $2`,
          [operatorId, invitationId],
        );
        await this.audit(
          connection,
          operatorId,
          "operator.invitation_accepted",
          "community",
          communityId,
          {
            invitation_id: invitationId,
            role: invitedRole,
          },
        );
      }

      await connection.query(
        "DELETE FROM operator_discord_guild_permissions WHERE operator_id = $1",
        [operatorId],
      );
      const recordedPermissions: Record<string, string> = {};
      const ownedGuilds = new Set(identity.ownedGuildIds);
      for (
        const [guildId, rawPermissions] of Object.entries(identity.permissions)
      ) {
        const id = guildId.trim();
        if (!id || !/^\d+$/u.test(String(rawPermissions).trim())) continue;
        let permissions = BigInt(String(rawPermissions).trim());
        if (ownedGuilds.has(id)) permissions |= 8n;
        recordedPermissions[id] = permissions.toString();
        await connection.query(
          `INSERT INTO operator_discord_guild_permissions(operator_id, guild_id, guild_name, permissions)
           VALUES ($1, $2, $3, $4::bigint)`,
          [
            operatorId,
            id,
            identity.guildNames[id] ?? id,
            permissions.toString(),
          ],
        );
      }
      await this.audit(
        connection,
        operatorId,
        "operator.discord_guild_permissions_refreshed",
        "operator_account",
        operatorId,
        { guild_permissions: recordedPermissions },
      );
      await this.audit(
        connection,
        operatorId,
        "auth.login",
        "operator_account",
        operatorId,
        {
          discord_user_id: identity.userId,
          role,
        },
      );
      return await this.loginState(connection, operatorId);
    });
  }

  async switchCommunity(
    operatorId: number,
    communityId: number,
    previousCommunityId: number | null,
  ): Promise<string | null> {
    return await this.connection.transaction(async (connection) => {
      const memberships = await connection.query(
        `SELECT role FROM operator_community_roles
          WHERE operator_id = $1 AND community_id = $2`,
        [operatorId, communityId],
      );
      if (!memberships[0]) return null;
      await this.audit(
        connection,
        operatorId,
        "community.switched",
        "community",
        communityId,
        {
          from_community_id: previousCommunityId,
        },
      );
      return String(memberships[0].role);
    });
  }

  async auditLogout(operatorId: number): Promise<void> {
    await this.audit(
      this.connection,
      operatorId,
      "auth.logout",
      "operator_account",
      operatorId,
      {},
    );
  }

  async resolveSession(operatorId: number): Promise<
    {
      readonly status: string;
      readonly sessionVersion: number;
      readonly memberships: readonly OperatorMembership[];
    } | null
  > {
    const accounts = await this.connection.query(
      "SELECT status, session_version FROM operator_accounts WHERE id = $1",
      [operatorId],
    );
    if (!accounts[0]) return null;
    return {
      status: String(accounts[0].status),
      sessionVersion: integer(accounts[0], "session_version"),
      memberships: await this.memberships(this.connection, operatorId),
    };
  }

  private async loginState(
    connection: DatabaseConnection,
    operatorId: number,
  ): Promise<OperatorLogin> {
    const accounts = await connection.query(
      "SELECT status, session_version FROM operator_accounts WHERE id = $1",
      [operatorId],
    );
    if (!accounts[0]) throw new TypeError("operator account not found");
    return Object.freeze({
      operatorId,
      status: String(accounts[0].status),
      sessionVersion: integer(accounts[0], "session_version"),
      memberships: await this.memberships(connection, operatorId),
    });
  }

  private async memberships(
    connection: DatabaseConnection,
    operatorId: number,
  ): Promise<readonly OperatorMembership[]> {
    const rows = await connection.query(
      `SELECT c.id, c.name, c.slug, r.role
         FROM operator_community_roles AS r
         JOIN communities AS c ON c.id = r.community_id
        WHERE r.operator_id = $1 AND c.status = 'active'
        ORDER BY LOWER(c.name), c.id`,
      [operatorId],
    );
    return Object.freeze(rows.map(decodeMembership));
  }

  private async audit(
    connection: DatabaseConnection,
    actorId: number,
    actionType: string,
    entityType: string,
    entityId: number,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    await connection.query(
      `INSERT INTO audit_log(actor_type, actor_id, action_type, entity_type, entity_id, payload_json)
       VALUES ('operator', $1, $2, $3, $4, $5)`,
      [actorId, actionType, entityType, entityId, JSON.stringify(payload)],
    );
  }
}
