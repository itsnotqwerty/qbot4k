import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { OnboardingWorkspace } from "../../components/OnboardingWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import type { DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";

export interface OnboardingSnapshot {
  readonly installations: readonly DatabaseRow[];
  readonly settings: DatabaseRow | null;
  readonly members: readonly DatabaseRow[];
  readonly resources: readonly DatabaseRow[];
}
export interface OnboardingService {
  snapshot(communityId: number): Promise<OnboardingSnapshot>;
  configure(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  saveResource(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number>;
  deleteResource(
    communityId: number,
    operatorId: number,
    resourceId: number,
  ): Promise<void>;
  verify(
    communityId: number,
    operatorId: number,
    platformUserId: string,
    evidence: string,
  ): Promise<void>;
}

export class PostgresOnboardingRepository implements OnboardingService {
  constructor(private readonly connection: DatabaseConnection) {}
  async snapshot(communityId: number): Promise<OnboardingSnapshot> {
    const installations = await this.connection.query(
      "SELECT id,display_name,external_community_id FROM community_installations WHERE community_id=$1 AND platform='discord' AND status='active' ORDER BY LOWER(display_name),id",
      [communityId],
    );
    const settings = (await this.connection.query(
      "SELECT * FROM community_onboarding_settings WHERE community_id=$1",
      [communityId],
    ))[0] ?? null;
    const members = await this.connection.query(
      "SELECT platform_user_id,username,status,role_assignment_status,role_assignment_attempts,joined_at,checkpoint_due_at,reminder_sent_at,verification_evidence,verified_at FROM community_onboarding_members WHERE community_id=$1 ORDER BY CASE status WHEN 'newcomer' THEN 0 ELSE 1 END,joined_at DESC",
      [communityId],
    );
    const resources = await this.connection.query(
      "SELECT id,title,resource_url,message_template,enabled,sort_order FROM community_onboarding_resources WHERE community_id=$1 ORDER BY sort_order,LOWER(title),id",
      [communityId],
    );
    return Object.freeze({ installations, settings, members, resources });
  }
  async configure(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const installationId = Number(input.discord_installation_id);
    const channel = String(input.welcome_channel_id ?? "").trim();
    const template = String(input.welcome_template ?? "").trim();
    const roleId = String(input.newcomer_role_id ?? "").trim() || null;
    const dueHours = Math.max(
      1,
      Math.min(Number(input.checkpoint_due_hours) || 24, 720),
    );
    const reminderTemplate = String(input.checkpoint_reminder_template ?? "")
      .trim();
    const resourceUrl = String(input.verification_resource_url ?? "").trim() ||
      null;
    const resourceTemplate = String(input.verification_resource_template ?? "")
      .trim();
    if (!channel || !template) {
      throw new TypeError("welcome channel and template are required");
    }
    if (!template.includes("{mention}")) {
      throw new TypeError("welcome template must include {mention}");
    }
    if (input.newcomer_role_enabled && !roleId) {
      throw new TypeError(
        "newcomer role is required when role routing is enabled",
      );
    }
    if (
      input.checkpoint_reminder_enabled &&
      !reminderTemplate.includes("{mention}")
    ) {
      throw new TypeError(
        "checkpoint reminder template must include {mention}",
      );
    }
    if (
      input.verification_resource_enabled &&
      (!resourceUrl || !resourceTemplate.includes("{mention}") ||
        !resourceTemplate.includes("{resource_url}"))
    ) {
      throw new TypeError(
        !resourceUrl
          ? "resource URL is required when verified-member resources are enabled"
          : "resource template must include {mention} and {resource_url}",
      );
    }
    await this.connection.transaction(async (connection) => {
      const installation = (await connection.query(
        "SELECT id FROM community_installations WHERE id=$1 AND community_id=$2 AND platform='discord' AND status='active'",
        [installationId, communityId],
      ))[0];
      if (!installation) {
        throw new TypeError("active Discord installation not found");
      }
      await connection.query(
        `INSERT INTO community_onboarding_settings(community_id,discord_installation_id,welcome_channel_id,welcome_template,welcome_enabled,newcomer_role_id,newcomer_role_enabled,updated_by_operator_id,checkpoint_due_hours,checkpoint_reminder_enabled,checkpoint_reminder_template,verification_resource_enabled,verification_resource_url,verification_resource_template,verification_evidence_required,self_service_verification_enabled) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) ON CONFLICT(community_id) DO UPDATE SET discord_installation_id=EXCLUDED.discord_installation_id,welcome_channel_id=EXCLUDED.welcome_channel_id,welcome_template=EXCLUDED.welcome_template,welcome_enabled=EXCLUDED.welcome_enabled,newcomer_role_id=EXCLUDED.newcomer_role_id,newcomer_role_enabled=EXCLUDED.newcomer_role_enabled,checkpoint_due_hours=EXCLUDED.checkpoint_due_hours,checkpoint_reminder_enabled=EXCLUDED.checkpoint_reminder_enabled,checkpoint_reminder_template=EXCLUDED.checkpoint_reminder_template,verification_resource_enabled=EXCLUDED.verification_resource_enabled,verification_resource_url=EXCLUDED.verification_resource_url,verification_resource_template=EXCLUDED.verification_resource_template,verification_evidence_required=EXCLUDED.verification_evidence_required,self_service_verification_enabled=EXCLUDED.self_service_verification_enabled,updated_by_operator_id=EXCLUDED.updated_by_operator_id,updated_at=CURRENT_TIMESTAMP`,
        [
          communityId,
          installationId,
          channel,
          template,
          this.flag(input.enabled),
          roleId,
          this.flag(input.newcomer_role_enabled),
          operatorId,
          dueHours,
          this.flag(input.checkpoint_reminder_enabled),
          reminderTemplate,
          this.flag(input.verification_resource_enabled),
          resourceUrl,
          resourceTemplate,
          this.flag(input.verification_evidence_required),
          this.flag(input.self_service_verification_enabled),
        ],
      );
      await this.audit(
        connection,
        operatorId,
        "onboarding.welcome_configured",
        "community",
        communityId,
        {
          enabled: Boolean(input.enabled),
          installation_id: installationId,
          checkpoint_due_hours: dueHours,
        },
      );
    });
  }
  async saveResource(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const title = String(input.title ?? "").trim();
    const resourceUrl = String(input.resource_url ?? "").trim();
    const template = String(input.message_template ?? "").trim();
    if (!title || title.length > 120) {
      throw new TypeError(
        "resource title is required and must be 120 characters or fewer",
      );
    }
    try {
      const url = new URL(resourceUrl);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error();
    } catch {
      throw new TypeError("resource URL must be an absolute HTTP or HTTPS URL");
    }
    if (
      !template.includes("{mention}") || !template.includes("{resource_url}")
    ) {
      throw new TypeError(
        "resource template must include {mention} and {resource_url}",
      );
    }
    const resourceId = Number(input.resource_id) || null;
    const sortOrder = Math.max(
      -1000,
      Math.min(Number(input.sort_order) || 0, 1000),
    );
    return await this.connection.transaction(async (connection) => {
      let savedId: number;
      if (resourceId === null) {
        savedId = Number(
          (await connection.query(
            "INSERT INTO community_onboarding_resources(community_id,title,resource_url,message_template,enabled,sort_order,created_by_operator_id) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            [
              communityId,
              title,
              resourceUrl,
              template,
              this.flag(input.enabled),
              sortOrder,
              operatorId,
            ],
          ))[0]?.id,
        );
      } else {
        const row = (await connection.query(
          "UPDATE community_onboarding_resources SET title=$1,resource_url=$2,message_template=$3,enabled=$4,sort_order=$5,updated_at=CURRENT_TIMESTAMP WHERE id=$6 AND community_id=$7 RETURNING id",
          [
            title,
            resourceUrl,
            template,
            this.flag(input.enabled),
            sortOrder,
            resourceId,
            communityId,
          ],
        ))[0];
        if (!row) throw new TypeError("Onboarding resource not found");
        savedId = resourceId;
      }
      await this.audit(
        connection,
        operatorId,
        resourceId === null
          ? "onboarding.resource_created"
          : "onboarding.resource_updated",
        "onboarding_resource",
        savedId,
        { community_id: communityId, enabled: Boolean(input.enabled) },
      );
      return savedId;
    });
  }
  async deleteResource(
    communityId: number,
    operatorId: number,
    resourceId: number,
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const row = (await connection.query(
        "DELETE FROM community_onboarding_resources WHERE id=$1 AND community_id=$2 RETURNING id",
        [resourceId, communityId],
      ))[0];
      if (!row) throw new TypeError("Onboarding resource not found");
      await this.audit(
        connection,
        operatorId,
        "onboarding.resource_deleted",
        "onboarding_resource",
        resourceId,
        { community_id: communityId },
      );
    });
  }
  async verify(
    communityId: number,
    operatorId: number,
    platformUserId: string,
    evidence: string,
  ): Promise<void> {
    const normalizedEvidence = evidence.trim();
    if (normalizedEvidence.length > 2000) {
      throw new TypeError(
        "verification evidence must be 2000 characters or fewer",
      );
    }
    await this.connection.transaction(async (connection) => {
      const member = (await connection.query(
        `SELECT m.discord_installation_id,m.username,m.joined_at,s.welcome_channel_id,s.verification_resource_enabled,s.verification_resource_url,s.verification_resource_template,s.verification_evidence_required FROM community_onboarding_members m JOIN community_onboarding_settings s ON s.community_id=m.community_id WHERE m.community_id=$1 AND m.platform_user_id=$2 AND m.status='newcomer' FOR UPDATE`,
        [communityId, platformUserId],
      ))[0];
      if (!member) throw new TypeError("Newcomer checkpoint not found");
      if (member.verification_evidence_required && !normalizedEvidence) {
        throw new TypeError("verification evidence is required");
      }
      const verifiedAt = new Date().toISOString();
      await connection.query(
        "UPDATE community_onboarding_members SET status='verified',verification_evidence=$1,verified_at=$2,verified_by_operator_id=$3,updated_at=CURRENT_TIMESTAMP WHERE community_id=$4 AND platform_user_id=$5 AND status='newcomer'",
        [
          normalizedEvidence || null,
          verifiedAt,
          operatorId,
          communityId,
          platformUserId,
        ],
      );
      await this.audit(
        connection,
        operatorId,
        "onboarding.member_verified",
        "community",
        communityId,
        {
          platform_user_id: platformUserId,
          evidence: normalizedEvidence || null,
        },
      );
      const resources = await connection.query(
        "SELECT id,title,resource_url,message_template FROM community_onboarding_resources WHERE community_id=$1 AND enabled=1 ORDER BY sort_order,id",
        [communityId],
      );
      const announcements: Array<
        {
          title: unknown;
          resource_url: unknown;
          message_template: unknown;
          dedupe_key: string;
          source: Readonly<Record<string, unknown>>;
        }
      > = resources.map((resource) => ({
        title: resource.title,
        resource_url: resource.resource_url,
        message_template: resource.message_template,
        dedupe_key:
          `member-verification-resource:${resource.id}:${platformUserId}:${member.joined_at}`,
        source: {
          type: "member_verification_catalog_resource",
          resource_id: resource.id,
          user_id: platformUserId,
        },
      }));
      if (member.verification_resource_enabled) {
        announcements.unshift({
          title: "",
          resource_url: member.verification_resource_url,
          message_template: member.verification_resource_template,
          dedupe_key:
            `member-verification:${platformUserId}:${member.joined_at}`,
          source: {
            type: "member_verification_resource",
            user_id: platformUserId,
          },
        });
      }
      for (const resource of announcements) {
        const body = String(resource.message_template).replaceAll(
          "{mention}",
          `<@${platformUserId}>`,
        ).replaceAll("{username}", String(member.username)).replaceAll(
          "{title}",
          String(resource.title),
        ).replaceAll("{resource_url}", String(resource.resource_url));
        await connection.query(
          `INSERT INTO community_announcements(community_id,target_installation_id,platform,target_external_id,body,dedupe_key,source_json,status,scheduled_at,timezone) VALUES ($1,$2,'discord',$3,$4,$5,$6,'scheduled',$7,'UTC') ON CONFLICT (community_id,dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING`,
          [
            communityId,
            Number(member.discord_installation_id),
            String(member.welcome_channel_id),
            body,
            resource.dedupe_key,
            JSON.stringify(resource.source),
            verifiedAt,
          ],
        );
      }
    });
  }
  private flag(value: unknown) {
    return value ? 1 : 0;
  }
  private async audit(
    connection: DatabaseConnection,
    operatorId: number,
    action: string,
    entityType: string,
    entityId: number,
    payload: Readonly<Record<string, unknown>>,
  ) {
    await connection.query(
      "INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,$3,$4,$5)",
      [operatorId, action, entityType, entityId, JSON.stringify(payload)],
    );
  }
}

export class WebOnboardingController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly onboarding: OnboardingService,
  ) {}
  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request, false);
    if (session instanceof Response) return session;
    const snapshot = await this.onboarding.snapshot(session.communityId!);
    return new Response(
      `<!doctype html>${
        render(
          h(OnboardingWorkspace, {
            ...snapshot,
            canManage: roleAllows(session.role, "admin.manage"),
            status: new URL(request.url).searchParams.get("status") ?? "",
          }),
        )
      }`,
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  async configure(request: Request) {
    return await this.mutate(
      request,
      "Welcome%20automation%20saved",
      (session, form) =>
        this.onboarding.configure(
          session.communityId!,
          Number(session.userId),
          this.form(form),
        ),
    );
  }
  async saveResource(request: Request) {
    return await this.mutate(
      request,
      "Resource%20saved",
      (session, form) =>
        this.onboarding.saveResource(
          session.communityId!,
          Number(session.userId),
          this.form(form),
        ),
    );
  }
  async deleteResource(request: Request, resourceId: number) {
    return await this.mutate(
      request,
      "Resource%20deleted",
      (session) =>
        this.onboarding.deleteResource(
          session.communityId!,
          Number(session.userId),
          resourceId,
        ),
    );
  }
  async verify(request: Request) {
    return await this.mutate(
      request,
      "Member%20verified",
      async (session, form) => {
        const userId = String(form.get("platform_user_id") ?? "").trim();
        if (!userId) throw new TypeError("Member is required");
        await this.onboarding.verify(
          session.communityId!,
          Number(session.userId),
          userId,
          String(form.get("verification_evidence") ?? ""),
        );
      },
    );
  }
  private async mutate(
    request: Request,
    status: string,
    operation: (session: DashboardSession, form: FormData) => Promise<unknown>,
  ) {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      await operation(session, await request.formData());
      return new Response(null, {
        status: 302,
        headers: { location: `/onboarding?status=${status}` },
      });
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        { status: 400 },
      );
    }
  }
  private form(form: FormData): Readonly<Record<string, unknown>> {
    return Object.fromEntries(
      [...form.entries()].map((
        [key, value],
      ) => [key, value === "1" ? true : value]),
    );
  }
  private async authorize(
    request: Request,
    manage: boolean,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (session.communityId === null) {
      return new Response("Select a community", { status: 403 });
    }
    if (manage && !roleAllows(session.role, "admin.manage")) {
      return new Response("Onboarding management is not authorized", {
        status: 403,
      });
    }
    return session;
  }
  private validOrigin(request: Request) {
    const origin = request.headers.get("origin")?.replace(/\/$/u, "");
    return !origin || origin === new URL(request.url).origin;
  }
}
