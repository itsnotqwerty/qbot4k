import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { DashboardDataView } from "../../components/DashboardDataView.tsx";
import { UserProfileWorkspace } from "../../components/UserProfileWorkspace.tsx";
import { SearchWorkspace } from "../../components/SearchWorkspace.tsx";
import type { DashboardSession } from "../security/security.ts";
import {
  constantTimeEqual,
  isAllowedSameSiteOrigin,
} from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { dashboardDocument } from "./web_document.ts";
import type { RoleHealthMonitor } from "../core/health.ts";
import type { DashboardItem, DashboardQueryService } from "./web_queries.ts";
export { roleAllows } from "../security/permissions.ts";
import { roleAllows } from "../security/permissions.ts";

type Surface = "overview" | "users" | "search" | "signals" | "analytics";

const capabilities: Readonly<Record<Surface, string>> = {
  overview: "dashboard.access",
  users: "members.read",
  search: "dashboard.access",
  signals: "analytics.read",
  analytics: "analytics.read",
};

export interface DashboardOperations {
  goLive(communityId: number, operatorId: number): Promise<number>;
  restart(operatorId: number): Promise<string>;
  resetDatabase(operatorId: number): Promise<{ readonly rowsDeleted: number }>;
}

export class WebDashboardController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly queries: DashboardQueryService,
    private readonly operations?: DashboardOperations,
  ) {}

  async goLive(request: Request): Promise<Response> {
    const session = await this.authorizeCapability(request, "operators.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const count = await this.requireOperations().goLive(
        session.communityId!,
        Number(session.userId),
      );
      return this.redirect(
        `/dashboard?status=${
          encodeURIComponent(`Go Live sent ${count} pings`)
        }`,
      );
    } catch {
      return this.redirect("/dashboard?status=Go%20Live%20failed");
    }
  }

  async restart(request: Request): Promise<Response> {
    const session = await this.authorizeCapability(request, "operators.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    const service = await this.requireOperations().restart(
      Number(session.userId),
    );
    return this.redirect(
      `/dashboard?status=${
        encodeURIComponent(`Restart requested for ${service}`)
      }`,
    );
  }

  async resetDatabase(request: Request): Promise<Response> {
    const session = await this.authorizeCapability(request, "operators.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const form = await request.formData();
      if (
        !constantTimeEqual(
          String(form.get("confirmation") ?? "").trim(),
          "RESET",
        )
      ) {
        return this.redirect("/dashboard?status=Database%20reset%20cancelled");
      }
      const report = await this.requireOperations().resetDatabase(
        Number(session.userId),
      );
      return this.redirect(
        `/dashboard?status=${
          encodeURIComponent(
            `Database reset complete; deleted ${report.rowsDeleted} rows`,
          )
        }`,
      );
    } catch {
      return this.redirect("/dashboard?status=Database%20reset%20failed");
    }
  }

  async page(request: Request, surface: Surface): Promise<Response> {
    const authorized = await this.authorize(request, surface);
    if (authorized instanceof Response) return authorized;
    const url = new URL(request.url);
    const data = await this.load(
      surface,
      authorized.communityId!,
      url.searchParams,
    );
    if (surface === "search") {
      const html = render(h(SearchWorkspace, {
        items: this.items(surface, data),
        query: url.searchParams.get("q") ?? "",
      }));
      return new Response(dashboardDocument(html, "Search | QBot4K"), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    const items = surface === "overview"
      ? undefined
      : this.items(surface, data);
    const html = render(h(DashboardDataView, {
      title: surface === "overview"
        ? "Overview"
        : surface[0].toUpperCase() + surface.slice(1),
      eyebrow: surface === "overview"
        ? "Community workspace"
        : "Community data",
      description: `Tenant-scoped ${surface} for the active community.`,
      ...(items ? { items } : { metrics: data as DashboardItem }),
      query: url.searchParams.get("q") ?? "",
      activePath: surface === "overview" ? "/dashboard" : `/${surface}`,
    }));
    return new Response(dashboardDocument(html), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }

  async api(request: Request, surface: Surface): Promise<Response> {
    const authorized = await this.authorize(request, surface);
    if (authorized instanceof Response) return authorized;
    const query = new URL(request.url).searchParams;
    const data = await this.load(surface, authorized.communityId!, query);
    if (surface === "overview" || surface === "analytics") {
      return Response.json(data);
    }
    if (surface === "signals") {
      return Response.json({
        filters: { signals: query.getAll("signal") },
        sort: {
          by: query.get("sort") ?? "value",
          dir: query.get("dir") ?? "desc",
        },
        items: data,
      });
    }
    return Response.json({ items: data });
  }

  async searchExport(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "exports.create",
    );
    if (authorized instanceof Response) return authorized;
    const query = new URL(request.url).searchParams;
    query.set("limit", "500");
    query.set("offset", "0");
    const rows = await this.queries.search(authorized.communityId!, query);
    const fields = [
      "id",
      "occurred_at",
      "platform",
      "event_type",
      "external_event_id",
      "container_id",
      "context_id",
      "actor_user_id",
      "target_user_id",
      "language_code",
      "sentiment_label",
      "intent_label",
      "threat_level",
      "text_raw",
    ] as const;
    const body = [
      fields.join(","),
      ...rows.map((row) =>
        fields.map((field) => csvCell(row[field])).join(",")
      ),
    ].join("\r\n") +
      "\r\n";
    return new Response(body, {
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": 'attachment; filename="qbot4k-observations.csv"',
      },
    });
  }

  async analyticsExport(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "analytics.export",
    );
    if (authorized instanceof Response) return authorized;
    const data = await this.queries.analytics(authorized.communityId!);
    return new Response(
      JSON.stringify(
        {
          ...data,
          exported_at: new Date().toISOString(),
          community_id: authorized.communityId,
        },
        null,
        2,
      ),
      {
        headers: {
          "content-type": "application/json",
          "content-disposition":
            `attachment; filename="qbot4k-community-${authorized.communityId}-analytics.json"`,
        },
      },
    );
  }

  async saveQuery(request: Request, formResponse = false): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "dashboard.access",
    );
    if (authorized instanceof Response) return authorized;
    const contentType = request.headers.get("content-type") ?? "";
    const input = contentType.includes("application/json")
      ? await request.json() as Record<string, unknown>
      : Object.fromEntries(await request.formData());
    const name = String(input.name ?? "");
    const query = String(input.query ?? input.q ?? "");
    const filters = typeof input.filters === "object" && input.filters !== null
      ? input.filters as Record<string, unknown>
      : Object.fromEntries(
        Object.entries(input).filter(([key, value]) =>
          !["name", "query", "q"].includes(key) && String(value).trim()
        ),
      );
    try {
      const id = await this.queries.saveQuery(
        Number(authorized.userId),
        name,
        query,
        filters,
      );
      if (formResponse) {
        return new Response(null, {
          status: 302,
          headers: { location: `/search?q=${encodeURIComponent(query)}` },
        });
      }
      return Response.json({ id, status: "saved" });
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        {
          status: 400,
          headers: { "content-type": "text/plain; charset=utf-8" },
        },
      );
    }
  }

  async observationPivots(
    request: Request,
    observationId: number,
  ): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "dashboard.access",
    );
    if (authorized instanceof Response) return authorized;
    const payload = await this.queries.observationPivots(
      authorized.communityId!,
      observationId,
    );
    return payload
      ? Response.json(payload)
      : Response.json({ error: "observation_not_found" }, { status: 404 });
  }

  async userDetail(request: Request, userId: number): Promise<Response> {
    const authorized = await this.authorizeCapability(request, "members.read");
    if (authorized instanceof Response) return authorized;
    const payload = await this.queries.userDetail(
      authorized.communityId!,
      userId,
    );
    return payload
      ? Response.json(payload)
      : Response.json({ error: "user_not_found" }, { status: 404 });
  }

  async userPage(request: Request, userId: number): Promise<Response> {
    const authorized = await this.authorizeCapability(request, "members.read");
    if (authorized instanceof Response) return authorized;
    const payload = await this.queries.userDetail(
      authorized.communityId!,
      userId,
    );
    if (!payload) return new Response("User not found", { status: 404 });
    const rawUser = payload.user as DashboardItem;
    const { linked_accounts, notes, ...user } = rawUser;
    const url = new URL(request.url);
    const html = render(h(UserProfileWorkspace, {
      user,
      linkedAccounts: (linked_accounts ?? []) as readonly DashboardItem[],
      notes: (notes ?? []) as readonly DashboardItem[],
      status: url.searchParams.get("status") ?? "",
      accountStatus: url.searchParams.get("account_status") ?? "",
      canManage: roleAllows(authorized.role, "settings.manage"),
    }));
    return new Response(
      dashboardDocument(
        html,
        `${String(user.primary_display_name ?? `User ${userId}`)} | QBot4K`,
      ),
      {
        headers: { "content-type": "text/html; charset=utf-8" },
      },
    );
  }

  async lifecycleExport(request: Request, userId: number): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "exports.create",
    );
    if (authorized instanceof Response) return authorized;
    const payload = await this.queries.userDetail(
      authorized.communityId!,
      userId,
    );
    if (!payload) return new Response("User not found", { status: 404 });
    const fields = ["occurred_at", "event_type", "summary", "detail"] as const;
    const labels: Record<string, string> = {
      "member.joined": "Joined community",
      "member.left": "Left community",
      "member.roles_changed": "Discord roles changed",
      "moderation.ban_added": "Discord ban added",
      "moderation.ban_removed": "Discord ban removed",
    };
    const events = payload.lifecycle as readonly DashboardItem[];
    const body = [
      fields.join(","),
      ...events.map((event) =>
        [
          event.occurred_at,
          event.event_type,
          labels[String(event.event_type)] ?? event.event_type,
          event.attributes_json ?? "",
        ].map(csvCell).join(",")
      ),
    ].join("\r\n") + "\r\n";
    return new Response(body, {
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition":
          `attachment; filename="qbot4k-user-${userId}-lifecycle.csv"`,
      },
    });
  }

  async unlinkUser(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "settings.manage",
    );
    if (authorized instanceof Response) return authorized;
    const form = await request.formData();
    const userId = Number(form.get("user_id"));
    const accountId = Number(form.get("platform_account_id"));
    const valid = Number.isSafeInteger(userId) && userId > 0 &&
      Number.isSafeInteger(accountId) && accountId > 0 &&
      form.get("confirmation") === "UNLINK";
    if (!valid) {
      return new Response(null, {
        status: 302,
        headers: {
          location: `/users/${userId}?account_status=${
            encodeURIComponent("Unlink confirmation failed")
          }`,
        },
      });
    }
    const removed = await this.queries.unlinkUser(
      authorized.communityId!,
      Number(authorized.userId),
      userId,
      accountId,
    );
    const status = removed
      ? "Platform account unlinked"
      : "Platform account does not belong to this user";
    return new Response(null, {
      status: 302,
      headers: {
        location: `/users/${userId}?account_status=${
          encodeURIComponent(status)
        }`,
      },
    });
  }

  async health(
    request: Request,
    monitor?: RoleHealthMonitor,
  ): Promise<Response> {
    const authorized = await this.authorizeCapability(request, "members.read");
    if (authorized instanceof Response) return authorized;
    return Response.json(
      monitor ? await monitor.snapshot() : { status: "ready" },
    );
  }

  async reviewIdentitySuggestion(
    request: Request,
    suggestionId: number,
  ): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "settings.manage",
    );
    if (authorized instanceof Response) return authorized;
    try {
      const payload = await request.json() as Record<string, unknown>;
      const reviewed = await this.queries.reviewIdentitySuggestion(
        authorized.communityId!,
        Number(authorized.userId),
        suggestionId,
        String(payload.decision ?? ""),
      );
      return reviewed
        ? Response.json({ status: "reviewed" })
        : Response.json({ error: "pending suggestion not found" }, {
          status: 400,
        });
    } catch (error) {
      return Response.json({
        error: error instanceof Error ? error.message : String(error),
      }, { status: 400 });
    }
  }

  async linkUser(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "settings.manage",
    );
    if (authorized instanceof Response) return authorized;
    let payload: Record<string, unknown>;
    try {
      payload = await request.json();
    } catch {
      return Response.json({ error: "invalid_payload" }, { status: 400 });
    }
    const userId = Number(payload.user_id);
    const discordUserId = String(payload.discord_user_id ?? "");
    if (!Number.isSafeInteger(userId) || userId < 1 || !discordUserId) {
      return Response.json({ error: "invalid_payload" }, { status: 400 });
    }
    const result = await this.queries.linkUser(
      authorized.communityId!,
      Number(authorized.userId),
      userId,
      String(payload.platform ?? "discord"),
      String(payload.platform_user_id ?? discordUserId),
    );
    return result === "linked"
      ? Response.json({ status: "linked" })
      : Response.json({ error: result }, { status: 404 });
  }

  async linkUsersForm(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "settings.manage",
    );
    if (authorized instanceof Response) return authorized;
    const form = await request.formData();
    const selectedUserId = Number(form.get("selected_user_id"));
    const search = String(form.get("q") ?? "").trim();
    const sort = new Set(["score", "name", "messages", "accounts"]).has(
        String(form.get("sort") ?? "score"),
      )
      ? String(form.get("sort") ?? "score")
      : "score";
    const direction = new Set(["asc", "desc"]).has(String(form.get("dir")))
      ? String(form.get("dir"))
      : sort === "name"
      ? "asc"
      : "desc";
    const usersUrl = (status: string, userId?: number) => {
      const params = new URLSearchParams({ q: search, sort, dir: direction });
      if (userId !== undefined) params.set("link_user_id", String(userId));
      params.set("link_status", status);
      return `/users?${params}`;
    };
    if (!Number.isSafeInteger(selectedUserId)) {
      return redirect(usersUrl("Invalid selected user"));
    }
    const platformInput = String(form.get("platform") ?? "any")
      .toLocaleLowerCase();
    const platform = new Set(["any", "discord", "twitch"]).has(platformInput)
      ? platformInput
      : "any";
    const usernames = String(form.get("usernames") ?? "").replaceAll("\n", ",")
      .split(",").map((value) => value.trim().replace(/ \(unlinked\)$/iu, ""))
      .filter(Boolean);
    if (!usernames.length) {
      return redirect(usersUrl("No usernames provided", selectedUserId));
    }
    const result = await this.queries.linkUsersByName(
      authorized.communityId!,
      Number(authorized.userId),
      selectedUserId,
      platform,
      usernames,
    );
    if (!result) return redirect(usersUrl("Selected user not found"));
    let status =
      `Linked ${result.linkedUsernames} username(s), ${result.linkedAccounts} account(s).`;
    if (result.missingUsernames.length) {
      status += ` Missing: ${result.missingUsernames.slice(0, 3).join(", ")}`;
      if (result.missingUsernames.length > 3) status += ", ...";
    }
    return redirect(usersUrl(status, result.userId));
  }

  async addUserNote(request: Request, userId: number): Promise<Response> {
    const authorized = await this.authorizeCapability(request, "members.read");
    if (authorized instanceof Response) return authorized;
    let payload: Record<string, unknown>;
    try {
      payload = await request.json();
    } catch {
      return Response.json({ error: "invalid_payload" }, { status: 400 });
    }
    const body = String(payload.body ?? "").trim();
    if (!body) {
      return Response.json({ error: "invalid_payload" }, { status: 400 });
    }
    const inserted = await this.queries.addUserNote(
      authorized.communityId!,
      Number(authorized.userId),
      userId,
      body,
    );
    return inserted
      ? Response.json({ status: "noted" })
      : Response.json({ error: "user_not_found" }, { status: 404 });
  }

  async slo(request: Request): Promise<Response> {
    const authorized = await this.authorizeCapability(
      request,
      "analytics.read",
    );
    if (authorized instanceof Response) return authorized;
    const items = (await this.queries.slo(authorized.communityId!)).map((
      sample,
    ) => ({
      metric_name: sample.metricName,
      value: sample.value,
      target_value: sample.targetValue,
      status: sample.status,
      evidence_count: sample.evidenceCount,
    }));
    return Response.json({ community_id: authorized.communityId, items });
  }

  private async authorize(
    request: Request,
    surface: Surface,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (
      session.communityId === null ||
      !roleAllows(session.role, capabilities[surface])
    ) {
      return new Response("Forbidden", {
        status: 403,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    return session;
  }

  private async authorizeCapability(
    request: Request,
    capability: string,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (
      session.communityId === null || !roleAllows(session.role, capability)
    ) {
      return new Response("Forbidden", {
        status: 403,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    return session;
  }

  private validOrigin(request: Request): boolean {
    return isAllowedSameSiteOrigin(request);
  }

  private redirect(location: string): Response {
    return new Response(null, { status: 302, headers: { location } });
  }

  private requireOperations(): DashboardOperations {
    if (!this.operations) {
      throw new TypeError("dashboard operations are unavailable");
    }
    return this.operations;
  }

  private load(
    surface: Surface,
    communityId: number,
    query: URLSearchParams,
  ): Promise<DashboardItem | readonly DashboardItem[]> {
    if (surface === "overview") return this.queries.overview(communityId);
    if (surface === "users") return this.queries.users(communityId, query);
    if (surface === "search") return this.queries.search(communityId, query);
    if (surface === "signals") return this.queries.signals(communityId, query);
    return this.queries.analytics(communityId);
  }

  private items(
    surface: Surface,
    data: DashboardItem | readonly DashboardItem[],
  ): readonly DashboardItem[] {
    if (Array.isArray(data)) return data;
    if (surface === "analytics") {
      return Object.entries(data).flatMap(([section, rows]) =>
        Array.isArray(rows) ? rows.map((row) => ({ section, ...row })) : []
      );
    }
    return [];
  }
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = String(value);
  return /[",\r\n]/u.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function redirect(location: string): Response {
  return new Response(null, { status: 302, headers: { location } });
}
