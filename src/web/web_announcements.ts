import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { AnnouncementsWorkspace } from "../../components/AnnouncementsWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import type { DashboardSession } from "../security/security.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import { consumeTenantQuota } from "../domain/quota.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";

export interface AnnouncementService {
  list(
    communityId: number,
  ): Promise<
    {
      readonly community: DatabaseRow;
      readonly installations: readonly DatabaseRow[];
      readonly items: readonly DatabaseRow[];
    }
  >;
  create(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number>;
  transition(
    communityId: number,
    operatorId: number,
    announcementId: number,
    action: string,
    scheduledAt?: string,
  ): Promise<void>;
}

export class PostgresAnnouncementRepository implements AnnouncementService {
  constructor(private readonly connection: DatabaseConnection) {}
  async list(communityId: number) {
    const community = (await this.connection.query(
      "SELECT name,timezone FROM communities WHERE id=$1 AND status='active'",
      [communityId],
    ))[0];
    if (!community) throw new TypeError("Community not found");
    const installations = await this.connection.query(
      "SELECT id,display_name,external_community_id FROM community_installations WHERE community_id=$1 AND platform='discord' AND status='active' ORDER BY LOWER(display_name),id",
      [communityId],
    );
    const items = await this.connection.query(
      `SELECT a.id,a.platform,a.target_external_id,a.body,a.status,a.scheduled_at,a.last_error,a.timezone,i.display_name AS installation_name,(SELECT COUNT(*) FROM community_announcement_deliveries d WHERE d.announcement_id=a.id) AS attempt_count FROM community_announcements a LEFT JOIN community_installations i ON i.id=a.target_installation_id AND i.community_id=a.community_id WHERE a.community_id=$1 ORDER BY a.created_at DESC,a.id DESC`,
      [communityId],
    );
    return Object.freeze({ community, installations, items });
  }
  async create(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const platform = String(input.platform ?? "").trim().toLocaleLowerCase();
    const target = String(input.target_external_id ?? "").trim();
    const body = String(input.body ?? "").trim();
    if (platform !== "discord" && platform !== "twitch") {
      throw new TypeError("unsupported announcement platform");
    }
    if (!target || !body) {
      throw new TypeError("announcement target and body are required");
    }
    return await this.connection.transaction(async (connection) => {
      await consumeTenantQuota(connection, communityId, "announcements");
      const requested = Number(input.target_installation_id) || null;
      const installations = await connection.query(
        `SELECT id FROM community_installations WHERE community_id=$1 AND platform=$2 AND status='active' ${
          requested ? "AND id=$3" : ""
        } ORDER BY id LIMIT 2`,
        requested
          ? [communityId, platform, requested]
          : [communityId, platform],
      );
      if (requested && !installations[0]) {
        throw new TypeError("active target installation not found");
      }
      const installationId = requested ??
        (installations.length === 1 ? Number(installations[0].id) : null);
      const community = (await connection.query(
        "SELECT timezone FROM communities WHERE id=$1",
        [communityId],
      ))[0];
      if (!community) throw new TypeError("community not found");
      const announcementId = Number(
        (await connection.query(
          `INSERT INTO community_announcements(community_id,target_installation_id,platform,target_external_id,body,created_by_operator_id,timezone) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`,
          [
            communityId,
            installationId,
            platform,
            target,
            body,
            operatorId,
            String(community.timezone),
          ],
        ))[0]?.id,
      );
      await this.audit(
        connection,
        operatorId,
        "announcement.created",
        announcementId,
        { community_id: communityId, platform },
      );
      return announcementId;
    });
  }
  async transition(
    communityId: number,
    operatorId: number,
    announcementId: number,
    action: string,
    scheduledAt = "",
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const item = (await connection.query(
        "SELECT status,timezone FROM community_announcements WHERE id=$1 AND community_id=$2 FOR UPDATE",
        [announcementId, communityId],
      ))[0];
      if (!item) throw new TypeError("Announcement not found");
      if (action === "approve") {
        if (item.status !== "draft") {
          throw new TypeError("Draft announcement not found");
        }
        if (!scheduledAt.trim() || Number.isNaN(Date.parse(scheduledAt))) {
          throw new TypeError("invalid announcement schedule");
        }
        const scheduled = (await connection.query(
          "UPDATE community_announcements SET status='scheduled',scheduled_at=($1::timestamp AT TIME ZONE $2),approved_by_operator_id=$3,approved_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=$4 AND community_id=$5 RETURNING scheduled_at",
          [
            scheduledAt,
            String(item.timezone),
            operatorId,
            announcementId,
            communityId,
          ],
        ))[0]?.scheduled_at;
        await this.audit(
          connection,
          operatorId,
          "announcement.approved",
          announcementId,
          { community_id: communityId, scheduled_at: scheduled },
        );
      } else if (action === "cancel") {
        if (!["draft", "scheduled", "failed"].includes(String(item.status))) {
          throw new TypeError("cancellable announcement not found");
        }
        await connection.query(
          "UPDATE community_announcements SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND community_id=$2",
          [announcementId, communityId],
        );
        await this.audit(
          connection,
          operatorId,
          "announcement.cancelled",
          announcementId,
          { community_id: communityId },
        );
      } else if (action === "retry") {
        if (item.status !== "failed") {
          throw new TypeError("failed announcement not found");
        }
        const attempts = Number(
          (await connection.query(
            "SELECT COUNT(*) AS count FROM community_announcement_deliveries WHERE announcement_id=$1",
            [announcementId],
          ))[0]?.count ?? 0,
        );
        if (attempts >= 3) {
          throw new TypeError("announcement delivery attempt limit reached");
        }
        await connection.query(
          "UPDATE community_announcements SET status='scheduled',scheduled_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND community_id=$2",
          [announcementId, communityId],
        );
        await this.audit(
          connection,
          operatorId,
          "announcement.retry_scheduled",
          announcementId,
          { community_id: communityId },
        );
      } else throw new TypeError("unsupported announcement transition");
    });
  }
  private async audit(
    connection: DatabaseConnection,
    operatorId: number,
    action: string,
    id: number,
    payload: Readonly<Record<string, unknown>>,
  ) {
    await connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,'community_announcement',$3,$4)`,
      [operatorId, action, id, JSON.stringify(payload)],
    );
  }
}

export class WebAnnouncementsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly announcements: AnnouncementService,
  ) {}
  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request, false);
    if (session instanceof Response) return session;
    const data = await this.announcements.list(session.communityId!);
    return new Response(
      dashboardDocument(
        render(
          h(AnnouncementsWorkspace, {
            ...data,
            canManage: roleAllows(session.role, "admin.manage"),
            status: new URL(request.url).searchParams.get("status") ?? "",
          }),
        ),
        "Announcements | QBot4K",
      ),
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  async create(request: Request): Promise<Response> {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.announcements.create(
        session.communityId!,
        Number(session.userId),
        Object.fromEntries(form),
      );
      return this.redirect("/announcements?status=Draft%20saved");
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        { status: 400 },
      );
    }
  }
  async transition(
    request: Request,
    id: number,
    action: string,
  ): Promise<Response> {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.announcements.transition(
        session.communityId!,
        Number(session.userId),
        id,
        action,
        String(form.get("scheduled_at") ?? ""),
      );
      return this.redirect(
        `/announcements?status=${
          action === "approve"
            ? "Announcement%20scheduled"
            : action === "cancel"
            ? "Announcement%20cancelled"
            : "Retry%20scheduled"
        }`,
      );
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        { status: 409 },
      );
    }
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
    if (manage && !roleAllows(session.role, "admin.manage")) {
      return new Response("Announcement management is not authorized", {
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
}
