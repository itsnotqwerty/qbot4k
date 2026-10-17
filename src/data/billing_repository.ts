import {
  type AccessGrant,
  BILLING_CATALOG,
  type OrganizationEntitlement,
  resolveOrganizationEntitlement,
} from "../domain/billing.ts";
import type { DatabaseConnection, DatabaseRow } from "./database.ts";

export type AdminView = "organizations" | "accounts" | "audit";

export type OrganizationBillingCapability = "billing.read" | "billing.manage";
export type PlatformBillingCapability =
  | "customers.read"
  | "benefits.manage"
  | "staff.manage";

export class OrganizationSignupConflict extends Error {}

export const STAFF_ORGANIZATION_ACCESS_SQL = `EXISTS (
  SELECT 1 FROM organization_operator_roles owner
  JOIN operator_accounts account ON account.id=owner.operator_id
  JOIN platform_staff_roles staff ON staff.operator_id=account.id
  WHERE owner.organization_id=organization.id AND owner.role='owner'
    AND account.status='active' AND staff.role='admin'
)`;

function requireId(value: number): void {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError("ID must be a positive integer");
  }
}

function isoTimestamp(value: unknown): string {
  if (!(value instanceof Date) && typeof value !== "string") {
    throw new TypeError("invalid stored timestamp");
  }
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) {
    throw new TypeError("invalid stored timestamp");
  }
  return date.toISOString();
}

export class PostgresBillingRepository {
  constructor(private readonly connection: DatabaseConnection) {}

  async organizationAllows(
    operatorId: number,
    organizationId: number,
    capability: OrganizationBillingCapability,
  ): Promise<boolean> {
    requireId(operatorId);
    requireId(organizationId);
    const [membership] = await this.connection.query(
      `SELECT membership.role FROM organization_operator_roles membership
       JOIN operator_accounts operator ON operator.id=membership.operator_id
       JOIN organizations organization ON organization.id=membership.organization_id
       WHERE membership.operator_id=$1 AND membership.organization_id=$2
         AND operator.status='active' AND organization.status='active'`,
      [operatorId, organizationId],
    );
    return (capability === "billing.read" || capability === "billing.manage") &&
      (membership?.role === "owner" || membership?.role === "billing_manager");
  }

  async platformAllows(
    operatorId: number,
    capability: PlatformBillingCapability,
  ): Promise<boolean> {
    requireId(operatorId);
    const [staff] = await this.connection.query(
      `SELECT staff.role FROM platform_staff_roles staff
       JOIN operator_accounts operator ON operator.id=staff.operator_id
       WHERE staff.operator_id=$1 AND operator.status='active'`,
      [operatorId],
    );
    if (capability === "staff.manage") return staff?.role === "admin";
    if (capability === "benefits.manage") {
      return staff?.role === "billing" || staff?.role === "admin";
    }
    return capability === "customers.read" &&
      ["support", "billing", "admin"].includes(String(staff?.role));
  }

  async createOrganization(
    operatorId: number,
    name: string,
    slug: string,
    expectedSessionVersion?: number,
  ): Promise<Readonly<{ organizationId: number; communityId: number }>> {
    requireId(operatorId);
    if (expectedSessionVersion !== undefined) requireId(expectedSessionVersion);
    const normalizedName = name.trim();
    if (!normalizedName || normalizedName.length > 120) {
      throw new TypeError("organization name must contain 1 to 120 characters");
    }
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(slug) || slug.length > 80) {
      throw new TypeError("invalid organization slug");
    }
    return await this.connection.transaction(async (connection) => {
      const [operator] = await connection.query(
        "SELECT id,session_version FROM operator_accounts WHERE id=$1 AND status='active' FOR UPDATE",
        [operatorId],
      );
      if (!operator) throw new TypeError("active account required");
      if (expectedSessionVersion !== undefined) {
        if (Number(operator.session_version) !== expectedSessionVersion) {
          throw new OrganizationSignupConflict(
            "Account session has changed; sign in again.",
          );
        }
        const [existing] = await connection.query(
          "SELECT organization_id FROM organization_operator_roles WHERE operator_id=$1 AND role='owner' LIMIT 1",
          [operatorId],
        );
        if (existing) {
          throw new OrganizationSignupConflict(
            "This account already owns an organization.",
          );
        }
      }
      const [organization] = await connection.query(
        "INSERT INTO organizations(name,slug) VALUES ($1,$2) RETURNING id",
        [normalizedName, slug],
      );
      const organizationId = Number(organization?.id);
      requireId(organizationId);
      const [workspace] = await connection.query(
        "INSERT INTO workspaces(organization_id,name,slug) VALUES ($1,$2,$3) RETURNING id",
        [organizationId, normalizedName, slug],
      );
      const workspaceId = Number(workspace?.id);
      requireId(workspaceId);
      const [community] = await connection.query(
        "INSERT INTO communities(workspace_id,name,slug) VALUES ($1,$2,$3) RETURNING id",
        [workspaceId, normalizedName, slug],
      );
      const communityId = Number(community?.id);
      requireId(communityId);
      await connection.query(
        "INSERT INTO organization_operator_roles(organization_id,operator_id,role) VALUES ($1,$2,'owner')",
        [organizationId, operatorId],
      );
      await connection.query(
        "INSERT INTO operator_community_roles(operator_id,community_id,role) VALUES ($1,$2,'owner')",
        [operatorId, communityId],
      );
      await connection.query(
        "INSERT INTO community_policy_settings(community_id) VALUES ($1)",
        [communityId],
      );
      await connection.query(
        "UPDATE operator_accounts SET session_version=session_version+1 WHERE id=$1",
        [operatorId],
      );
      await connection.query(
        `INSERT INTO organization_billing_audit(organization_id,operator_id,action,reason)
         VALUES ($1,$2,'organization.created','Customer organization provisioned')`,
        [organizationId, operatorId],
      );
      return Object.freeze({ organizationId, communityId });
    });
  }

  async adminRows(
    operatorId: number,
    view: AdminView,
    page: number,
  ): Promise<readonly DatabaseRow[] | null> {
    if (!await this.platformAllows(operatorId, "staff.manage")) return null;
    requireId(page);
    if (page > 100_000) throw new TypeError("page exceeds limit");
    const queries = {
      organizations:
        `SELECT organization.id,organization.name,organization.status,
        ${STAFF_ORGANIZATION_ACCESS_SQL} AS complimentary,
        (SELECT count(*) FROM workspaces workspace JOIN communities community ON community.workspace_id=workspace.id
         WHERE workspace.organization_id=organization.id) AS communities
        FROM organizations organization ORDER BY organization.id LIMIT 51 OFFSET $1`,
      accounts:
        `SELECT account.id,account.discord_username,account.status,staff.role AS platform_role
        FROM operator_accounts account LEFT JOIN platform_staff_roles staff ON staff.operator_id=account.id
        ORDER BY account.id LIMIT 51 OFFSET $1`,
      audit: `SELECT id,organization_id,operator_id,action,reason,created_at
        FROM organization_billing_audit ORDER BY id DESC LIMIT 51 OFFSET $1`,
    };
    if (!Object.hasOwn(queries, view)) {
      throw new TypeError("invalid admin view");
    }
    return await this.connection.query(queries[view], [(page - 1) * 50]);
  }

  async entitlement(
    organizationId: number,
    now = new Date(),
  ): Promise<OrganizationEntitlement> {
    requireId(organizationId);
    const rows = await this.connection.query(
      `SELECT grant_row.organization_id,grant_row.source,grant_row.source_id,
              grant_row.kind,grant_row.status,grant_row.starts_at,grant_row.ends_at
       FROM organization_access_grants grant_row
       JOIN organizations organization ON organization.id=grant_row.organization_id
       WHERE grant_row.organization_id=$1 AND organization.status='active'
         AND grant_row.status='active' ORDER BY grant_row.id`,
      [organizationId],
    );
    const grants = rows.map((row): AccessGrant => ({
      organizationId: Number(row.organization_id),
      source: String(row.source) as AccessGrant["source"],
      sourceId: String(row.source_id),
      kind: String(row.kind) as AccessGrant["kind"],
      status: String(row.status) as AccessGrant["status"],
      startsAt: isoTimestamp(row.starts_at),
      endsAt: row.ends_at === null ? null : isoTimestamp(row.ends_at),
    }));
    const entitlement = resolveOrganizationEntitlement(
      organizationId,
      grants,
      now,
    );
    const [organization] = await this.connection.query(
      `SELECT ${STAFF_ORGANIZATION_ACCESS_SQL} AS complimentary
       FROM organizations organization WHERE organization.id=$1 AND organization.status='active'`,
      [organizationId],
    );
    return organization?.complimentary === true
      ? Object.freeze({
        ...entitlement,
        active: true,
        complimentary: true,
        communityLimit: BILLING_CATALOG.communityLimit,
        endsAt: null,
      })
      : entitlement;
  }
}
