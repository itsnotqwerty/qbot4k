import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { AuditWorkspace } from "../../components/AuditWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import type { DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";

export interface AuditService {
  list(
    query: URLSearchParams,
    timeZone?: string,
  ): Promise<readonly DatabaseRow[]>;
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

function ianaTimeZone(zone: unknown): string {
  const raw = String(zone ?? "").trim();
  if (!raw) return "UTC";
  const aliased = IANA_TIMEZONE_ALIAS[raw.toUpperCase()] ?? raw;
  return /^[A-Za-z_]+\/[A-Za-z_]+/u.test(aliased) ? aliased : "UTC";
}

export class PostgresAuditRepository implements AuditService {
  constructor(private readonly connection: DatabaseConnection) {}
  async list(
    query: URLSearchParams,
    timeZone = "UTC",
  ): Promise<readonly DatabaseRow[]> {
    const where: string[] = [];
    const parameters: (string | number)[] = [];
    const bind = (value: string | number) => {
      parameters.push(value);
      return `$${parameters.length}`;
    };
    for (const key of ["action_type", "entity_type"] as const) {
      const value = query.get(key)?.trim();
      if (value) where.push(`${key}=${bind(value)}`);
    }
    const actorId = query.get("actor_id")?.trim() ?? "";
    if (/^\d+$/u.test(actorId)) where.push(`actor_id=${bind(Number(actorId))}`);
    const startAt = query.get("start_at")?.trim();
    if (startAt) {
      where.push(`created_at::timestamptz>=${bind(startAt)}::timestamptz`);
    }
    const limit = Math.max(
      1,
      Math.min(Number.parseInt(query.get("limit") ?? "200") || 200, 500),
    );
    const offset = Math.max(
      0,
      Number.parseInt(query.get("offset") ?? "0") || 0,
    );
    const zone = ianaTimeZone(timeZone);
    const starred =
      `id,actor_type,actor_id,action_type,entity_type,entity_id,payload_json,
      to_char(created_at::timestamptz AT TIME ZONE '${zone}', 'YYYY-MM-DD HH24:MI:SS') AS created_at`;
    return await this.connection.query(
      `SELECT ${starred} FROM audit_log ${
        where.length ? `WHERE ${where.join(" AND ")}` : ""
      } ORDER BY created_at DESC,id DESC LIMIT ${bind(limit)} OFFSET ${
        bind(offset)
      }`,
      parameters,
    );
  }
}

export class WebAuditController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly audit: AuditService,
    private readonly timeZoneFor?: (communityId: number) => Promise<string>,
  ) {}
  async response(request: Request, json: boolean): Promise<Response> {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    const query = new URL(request.url).searchParams;
    const timeZone = session.communityId !== null && this.timeZoneFor
      ? await this.timeZoneFor(session.communityId)
      : "UTC";
    const items = await this.audit.list(query, timeZone);
    if (json) return Response.json({ items });
    return new Response(
      dashboardDocument(
        render(h(AuditWorkspace, { items, query })),
        "Audit | QBot4K",
      ),
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  private async authorize(
    request: Request,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (!roleAllows(session.role, "admin.manage")) {
      return new Response("Forbidden", { status: 403 });
    }
    return session;
  }
}
