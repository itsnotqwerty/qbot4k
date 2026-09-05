import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { SettingsWorkspace } from "../../components/SettingsWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import {
  constantTimeEqual,
  isAllowedSameSiteOrigin,
} from "../security/security.ts";
import type { DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";

export interface SettingsSnapshot {
  readonly community: DatabaseRow;
  readonly policy: DatabaseRow;
  readonly installations: readonly DatabaseRow[];
  readonly destinations: readonly DatabaseRow[];
  readonly operators: readonly DatabaseRow[];
  readonly invitations: readonly DatabaseRow[];
}

export interface SettingsService {
  snapshot(communityId: number): Promise<SettingsSnapshot>;
  update(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  invite(
    communityId: number,
    operatorId: number,
    discordUserId: string,
    role: string,
    expiresHours: number,
  ): Promise<number>;
  access(
    communityId: number,
    operatorId: number,
    entityId: number,
    action: string,
    reason: string,
  ): Promise<void>;
}

const IANA_TIMEZONE_ALIAS: Readonly<Record<string, string>> = {
  CST: "America/Chicago",
  CDT: "America/Chicago",
  EST: "America/New_York",
  EDT: "America/New_York",
  MST: "America/Denver",
  MDT: "America/Denver",
  PST: "America/Los_Angeles",
  PDT: "America/Los_Angeles",
};

function normalizeTimeZoneName(zone: string): string | null {
  const raw = zone.trim();
  if (!raw) return null;
  const aliased = IANA_TIMEZONE_ALIAS[raw.toUpperCase()] ?? raw;
  try {
    new Intl.DateTimeFormat("en", { timeZone: aliased }).format();
    return aliased;
  } catch {
    return null;
  }
}

export class PostgresSettingsRepository implements SettingsService {
  constructor(private readonly connection: DatabaseConnection) {}

  async snapshot(communityId: number): Promise<SettingsSnapshot> {
    const community = (await this.connection.query(
      "SELECT name,slug,locale,timezone,description,guidelines,notifications_enabled FROM communities WHERE id=$1 AND status='active'",
      [communityId],
    ))[0];
    const policy = (await this.connection.query(
      "SELECT message_retention_days,analytics_retention_days,anti_abuse_enabled,anti_abuse_enforcement_mode,message_burst_limit,message_burst_window_seconds,mention_limit,join_raid_limit,join_raid_window_seconds FROM community_policy_settings WHERE community_id=$1",
      [communityId],
    ))[0];
    if (!community || !policy) {
      throw new TypeError("Community settings not found");
    }
    const installations = await this.connection.query(
      "SELECT platform,display_name,status,health_status FROM community_installations WHERE community_id=$1 ORDER BY platform,LOWER(display_name)",
      [communityId],
    );
    const destinations = await this.connection.query(
      "SELECT name,destination_type,minimum_severity,enabled FROM notification_destinations WHERE community_id=$1 ORDER BY name",
      [communityId],
    );
    const operators = await this.connection.query(
      "SELECT o.id,o.discord_username,r.role FROM operator_community_roles r JOIN operator_accounts o ON o.id=r.operator_id WHERE r.community_id=$1 AND o.status='active' ORDER BY LOWER(o.discord_username)",
      [communityId],
    );
    const invitations = await this.connection.query(
      "SELECT id,target_discord_user_id,invited_role,expires_at FROM operator_invitations WHERE community_id=$1 AND status='pending' ORDER BY created_at DESC",
      [communityId],
    );
    return Object.freeze({
      community,
      policy,
      installations,
      destinations,
      operators,
      invitations,
    });
  }

  async update(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const name = this.text(input.name);
    const locale = this.text(input.locale);
    const timezone = this.text(input.timezone);
    const description = this.text(input.description);
    const guidelines = this.text(input.guidelines);
    if (!name || name.length > 120) {
      throw new TypeError(
        "community name must be between 1 and 120 characters",
      );
    }
    if (!locale || locale.length > 35) {
      throw new TypeError("locale must be between 1 and 35 characters");
    }
    const normalizedTimeZone = normalizeTimeZoneName(timezone);
    if (!normalizedTimeZone) {
      throw new TypeError("timezone is not recognized");
    }
    if (description.length > 1000) {
      throw new TypeError(
        "community description must not exceed 1000 characters",
      );
    }
    if (guidelines.length > 10000) {
      throw new TypeError(
        "community guidelines must not exceed 10000 characters",
      );
    }
    const values = {
      message_retention_days: this.bounded(
        input,
        "message_retention_days",
        1,
        3650,
      ),
      analytics_retention_days: this.bounded(
        input,
        "analytics_retention_days",
        1,
        3650,
      ),
      message_burst_limit: this.bounded(input, "message_burst_limit", 2, 100),
      message_burst_window_seconds: this.bounded(
        input,
        "message_burst_window_seconds",
        1,
        300,
      ),
      mention_limit: this.bounded(input, "mention_limit", 1, 100),
      join_raid_limit: this.bounded(input, "join_raid_limit", 2, 1000),
      join_raid_window_seconds: this.bounded(
        input,
        "join_raid_window_seconds",
        1,
        3600,
      ),
    };
    const mode = this.text(input.anti_abuse_enforcement_mode)
      .toLocaleLowerCase();
    if (!["shadow", "enforce"].includes(mode)) {
      throw new TypeError(
        "anti-abuse enforcement mode must be shadow or enforce",
      );
    }
    await this.connection.transaction(async (connection) => {
      const updated = (await connection.query(
        "UPDATE communities SET name=$1,locale=$2,timezone=$3,description=$4,guidelines=$5,notifications_enabled=$6,updated_at=CURRENT_TIMESTAMP WHERE id=$7 AND status='active' RETURNING id",
        [
          name,
          locale,
          normalizedTimeZone,
          description,
          guidelines,
          this.flag(input.notifications_enabled),
          communityId,
        ],
      ))[0];
      if (!updated) throw new TypeError("active community not found");
      const policy = (await connection.query(
        "UPDATE community_policy_settings SET message_retention_days=$1,analytics_retention_days=$2,anti_abuse_enabled=$3,anti_abuse_enforcement_mode=$4,message_burst_limit=$5,message_burst_window_seconds=$6,mention_limit=$7,join_raid_limit=$8,join_raid_window_seconds=$9,updated_by_operator_id=$10,updated_at=CURRENT_TIMESTAMP WHERE community_id=$11 RETURNING community_id",
        [
          values.message_retention_days,
          values.analytics_retention_days,
          this.flag(input.anti_abuse_enabled),
          mode,
          values.message_burst_limit,
          values.message_burst_window_seconds,
          values.mention_limit,
          values.join_raid_limit,
          values.join_raid_window_seconds,
          operatorId,
          communityId,
        ],
      ))[0];
      if (!policy) throw new TypeError("community policy settings not found");
      await this.audit(
        connection,
        operatorId,
        "community.settings_updated",
        communityId,
        {
          locale,
          timezone: normalizedTimeZone,
          notifications_enabled: Boolean(input.notifications_enabled),
        },
      );
      await this.audit(
        connection,
        operatorId,
        "retention.policy_updated",
        communityId,
        {
          message_retention_days: values.message_retention_days,
          analytics_retention_days: values.analytics_retention_days,
        },
      );
      await this.audit(
        connection,
        operatorId,
        "anti_abuse.policy_updated",
        communityId,
        {
          enabled: Boolean(input.anti_abuse_enabled),
          enforcement_mode: mode,
          message_burst_limit: values.message_burst_limit,
          message_burst_window_seconds: values.message_burst_window_seconds,
          mention_limit: values.mention_limit,
          join_raid_limit: values.join_raid_limit,
          join_raid_window_seconds: values.join_raid_window_seconds,
        },
      );
    });
  }

  async invite(
    communityId: number,
    operatorId: number,
    discordUserId: string,
    role: string,
    expiresHours: number,
  ): Promise<number> {
    const target = discordUserId.trim();
    const normalizedRole = role.trim().toLocaleLowerCase();
    if (!target) {
      throw new TypeError(
        "operator invitation target and future expiry are required",
      );
    }
    if (!["viewer", "analyst", "moderator", "admin"].includes(normalizedRole)) {
      throw new TypeError(
        "invited role must be viewer, analyst, moderator, or admin",
      );
    }
    const hours = Math.max(1, Math.min(Math.trunc(expiresHours || 72), 720));
    const expiresAt = new Date(Date.now() + hours * 3_600_000).toISOString();
    return await this.connection.transaction(async (connection) => {
      const id = Number(
        (await connection.query(
          "INSERT INTO operator_invitations(community_id,target_discord_user_id,invited_role,expires_at,invited_by_operator_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
          [communityId, target, normalizedRole, expiresAt, operatorId],
        ))[0]?.id,
      );
      await this.audit(
        connection,
        operatorId,
        "operator.invitation_created",
        communityId,
        {
          invitation_id: id,
          target_discord_user_id: target,
          role: normalizedRole,
          expires_at: expiresAt,
        },
      );
      return id;
    });
  }

  async access(
    communityId: number,
    operatorId: number,
    entityId: number,
    action: string,
    reason: string,
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      if (action === "revoke-invitation") {
        const row = (await connection.query(
          "UPDATE operator_invitations SET status='revoked',revoked_at=CURRENT_TIMESTAMP WHERE id=$1 AND community_id=$2 AND status='pending' RETURNING id",
          [entityId, communityId],
        ))[0];
        if (!row) throw new TypeError("pending operator invitation not found");
        await this.audit(
          connection,
          operatorId,
          "operator.invitation_revoked",
          communityId,
          { invitation_id: entityId },
        );
        return;
      }
      const roles = await connection.query(
        "SELECT operator_id,role FROM operator_community_roles WHERE community_id=$1 AND operator_id IN ($2,$3)",
        [communityId, operatorId, entityId],
      );
      const role = (id: number) =>
        roles.find((row) => Number(row.operator_id) === id)?.role;
      if (action === "transfer-ownership") {
        if (operatorId === entityId) {
          throw new TypeError("new owner must be a different operator");
        }
        if (role(operatorId) !== "owner" || !role(entityId)) {
          throw new TypeError(
            "ownership transfer requires the current owner and an existing operator",
          );
        }
        await connection.query(
          "UPDATE operator_community_roles SET role=CASE WHEN operator_id=$1 THEN 'admin' ELSE 'owner' END WHERE community_id=$2 AND operator_id IN ($1,$3)",
          [operatorId, communityId, entityId],
        );
        await this.invalidate(connection, operatorId);
        await this.invalidate(connection, entityId);
        await this.audit(
          connection,
          operatorId,
          "operator.ownership_transferred",
          communityId,
          { previous_owner_id: operatorId, new_owner_id: entityId },
        );
        return;
      }
      if (action === "emergency-remove") {
        if (!reason.trim()) {
          throw new TypeError("emergency removal reason is required");
        }
        if (!role(entityId)) {
          throw new TypeError("operator community access not found");
        }
        if (role(entityId) === "owner") {
          throw new TypeError("transfer ownership before emergency removal");
        }
        await connection.query(
          "DELETE FROM operator_permission_overrides WHERE operator_id=$1 AND community_id=$2",
          [entityId, communityId],
        );
        await connection.query(
          "DELETE FROM operator_community_roles WHERE operator_id=$1 AND community_id=$2",
          [entityId, communityId],
        );
        await connection.query(
          "UPDATE operator_accounts SET status='disabled' WHERE id=$1",
          [entityId],
        );
        await this.invalidate(connection, entityId);
        await this.audit(
          connection,
          operatorId,
          "operator.access_emergency_removed",
          communityId,
          { operator_id: entityId, reason: reason.trim() },
        );
        return;
      }
      throw new TypeError("unsupported operator access action");
    });
  }
  private bounded(
    input: Readonly<Record<string, unknown>>,
    name: string,
    minimum: number,
    maximum: number,
  ) {
    const value = Number(input[name]);
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      throw new TypeError(`${name} must be between ${minimum} and ${maximum}`);
    }
    return value;
  }
  private text(value: unknown) {
    return String(value ?? "").trim();
  }
  private flag(value: unknown) {
    return value ? 1 : 0;
  }
  private async invalidate(connection: DatabaseConnection, operatorId: number) {
    await connection.query(
      "UPDATE operator_accounts SET session_version=session_version+1 WHERE id=$1",
      [operatorId],
    );
  }
  private async audit(
    connection: DatabaseConnection,
    operatorId: number,
    action: string,
    communityId: number,
    payload: Readonly<Record<string, unknown>>,
  ) {
    await connection.query(
      "INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,'community',$3,$4)",
      [operatorId, action, communityId, JSON.stringify(payload)],
    );
  }
}

export class WebSettingsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly settings: SettingsService,
  ) {}
  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request, false);
    if (session instanceof Response) return session;
    try {
      const snapshot = await this.settings.snapshot(session.communityId!);
      return new Response(
        dashboardDocument(
          render(h(SettingsWorkspace, {
            ...snapshot,
            canManageOperators: roleAllows(session.role, "operators.manage"),
            canManageIntegrations: roleAllows(
              session.role,
              "integrations.manage",
            ),
            status: new URL(request.url).searchParams.get("status") ?? "",
          })),
          "Settings | QBot4K",
        ),
        { headers: { "content-type": "text/html; charset=utf-8" } },
      );
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        { status: 404 },
      );
    }
  }
  async update(request: Request): Promise<Response> {
    return await this.formMutation(
      request,
      false,
      "Settings%20saved",
      async (session, form) =>
        await this.settings.update(
          session.communityId!,
          Number(session.userId),
          this.form(form),
        ),
    );
  }
  async invite(request: Request, json = false): Promise<Response> {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const payload = json
        ? await request.json() as Record<string, unknown>
        : this.form(await request.formData());
      const id = await this.settings.invite(
        session.communityId!,
        Number(session.userId),
        String(payload.discord_user_id ?? ""),
        String(payload.role ?? ""),
        Number(payload.expires_hours ?? 72),
      );
      return json
        ? Response.json({ invitation_id: id }, { status: 201 })
        : this.redirect("/settings?status=Operator%20invited");
    } catch (error) {
      return json
        ? Response.json({ error: this.message(error) }, { status: 400 })
        : new Response(this.message(error), { status: 400 });
    }
  }
  async access(
    request: Request,
    entityId: number,
    action: string,
  ): Promise<Response> {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const payload = await request.json() as Record<string, unknown>;
      if (["transfer-ownership", "emergency-remove"].includes(action)) {
        const expected = action === "transfer-ownership"
          ? `TRANSFER OWNERSHIP ${entityId}`
          : `EMERGENCY REMOVE ${entityId}`;
        if (!constantTimeEqual(String(payload.confirmation ?? ""), expected)) {
          throw new TypeError(`confirmation must be ${expected}`);
        }
      }
      await this.settings.access(
        session.communityId!,
        Number(session.userId),
        entityId,
        action,
        String(payload.reason ?? ""),
      );
      return Response.json({ status: "completed" });
    } catch (error) {
      return Response.json({ error: this.message(error) }, { status: 409 });
    }
  }
  private async formMutation(
    request: Request,
    manage: boolean,
    status: string,
    operation: (session: DashboardSession, form: FormData) => Promise<void>,
  ) {
    const session = await this.authorize(request, manage);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      await operation(session, await request.formData());
      return this.redirect(`/settings?status=${status}`);
    } catch (error) {
      return new Response(this.message(error), { status: 400 });
    }
  }
  private form(form: FormData): Readonly<Record<string, unknown>> {
    return Object.fromEntries(form.entries());
  }
  private async authorize(
    request: Request,
    manage: boolean,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) return this.redirect("/login");
    if (session.communityId === null) {
      return new Response("Select a community", { status: 403 });
    }
    if (manage && !roleAllows(session.role, "operators.manage")) {
      return new Response("Operator management is not authorized", {
        status: 403,
      });
    }
    return session;
  }
  private validOrigin(request: Request) {
    return isAllowedSameSiteOrigin(request);
  }
  private redirect(location: string) {
    return new Response(null, { status: 302, headers: { location } });
  }
  private message(error: unknown) {
    return error instanceof Error ? error.message : String(error);
  }
}
