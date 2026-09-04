import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import { constantTimeEqual, type DashboardSession } from "../security/security.ts";
import { roleAllows } from "../security/permissions.ts";
import { WebAuthController } from "./web_auth.ts";

export type IntelligenceItem = Readonly<Record<string, unknown>>;

export interface IntelligenceSnapshot {
  readonly summary: IntelligenceItem;
  readonly sort: IntelligenceItem;
  readonly alerts: readonly IntelligenceItem[];
  readonly cases: readonly IntelligenceItem[];
  readonly relationships: readonly IntelligenceItem[];
  readonly reports: readonly IntelligenceItem[];
}

export interface IntelligenceCaseDetail {
  readonly case: IntelligenceItem;
  readonly entities: readonly IntelligenceItem[];
  readonly evidence: readonly IntelligenceItem[];
  readonly activity: readonly IntelligenceItem[];
}

export interface IntelligenceService {
  snapshot(
    communityId: number,
    query: URLSearchParams,
  ): Promise<IntelligenceSnapshot>;
  caseDetail(
    communityId: number,
    caseId: number,
  ): Promise<IntelligenceCaseDetail | null>;
  caseAction(
    communityId: number,
    operatorId: number,
    caseId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  caseFromAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
  ): Promise<number>;
  disposeAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
    disposition: string,
  ): Promise<void>;
  updateAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  generateReport(
    communityId: number,
    reportType: string,
    userId: number | null,
  ): Promise<number>;
  report(
    communityId: number,
    reportId: number,
  ): Promise<IntelligenceItem | null>;
}

const frozenRows = (
  rows: readonly DatabaseRow[],
): readonly IntelligenceItem[] =>
  Object.freeze(rows.map((row) => Object.freeze({ ...row })));

const numberOrNull = (value: unknown): number | null => {
  if (value === null || value === undefined || String(value).trim() === "") {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError("invalid identifier");
  }
  return parsed;
};

export class PostgresIntelligenceRepository implements IntelligenceService {
  constructor(private readonly connection: DatabaseConnection) {}

  async snapshot(
    communityId: number,
    query: URLSearchParams,
  ): Promise<IntelligenceSnapshot> {
    const alertSorts: Record<string, string> = {
      severity:
        "CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END",
      subject: "LOWER(u.primary_display_name)",
      finding: "LOWER(a.title)",
      confidence: "a.confidence",
      status: "a.status",
      created: "a.created_at",
    };
    const caseSorts: Record<string, string> = {
      case: "LOWER(c.title)",
      priority:
        "CASE c.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END",
      status: "c.status",
      entities: "entity_count",
      evidence: "evidence_count",
      updated: "c.updated_at",
    };
    const relationshipSorts: Record<string, string> = {
      source: "LOWER(source.primary_display_name)",
      relationship: "r.relationship_type",
      target: "LOWER(target.primary_display_name)",
      strength: "r.strength",
      evidence: "r.evidence_count",
      last_observed: "r.last_observed_at",
    };
    const normalizedSort = (
      key: string,
      values: Record<string, string>,
      fallback: string,
    ) => values[query.get(key) ?? ""] ? query.get(key)! : fallback;
    const direction = (key: string) =>
      query.get(key) === "asc" ? "ASC" : "DESC";
    const alertSort = normalizedSort("alert_sort", alertSorts, "created");
    const caseSort = normalizedSort("case_sort", caseSorts, "updated");
    const relationshipSort = normalizedSort(
      "relationship_sort",
      relationshipSorts,
      "strength",
    );
    const [summaryRows, alerts, cases, relationships, reports] = await Promise
      .all([
        this.connection.query(
          `SELECT
          (SELECT COUNT(*) FROM intelligence_alerts WHERE community_id=$1 AND status='open') AS open_alerts,
          (SELECT COUNT(*) FROM investigation_cases WHERE community_id=$1 AND status!='closed') AS open_cases,
          (SELECT COUNT(*) FROM entity_relationships WHERE community_id=$1) AS relationships,
          (SELECT COUNT(*) FROM intelligence_reports WHERE community_id=$1) AS reports`,
          [communityId],
        ),
        this.connection.query(
          `SELECT a.*,u.primary_display_name FROM intelligence_alerts AS a
          LEFT JOIN users AS u ON u.id=a.user_id WHERE a.community_id=$1
          ORDER BY ${alertSorts[alertSort]} ${
            direction("alert_dir")
          },a.id DESC LIMIT 500`,
          [communityId],
        ),
        this.connection.query(
          `SELECT c.*,COUNT(DISTINCT ce.user_id) AS entity_count,
                COUNT(DISTINCT ev.id) AS evidence_count
           FROM investigation_cases AS c LEFT JOIN case_entities AS ce ON ce.case_id=c.id
           LEFT JOIN case_evidence AS ev ON ev.case_id=c.id WHERE c.community_id=$1
          GROUP BY c.id ORDER BY ${caseSorts[caseSort]} ${
            direction("case_dir")
          },c.id DESC LIMIT 500`,
          [communityId],
        ),
        this.connection.query(
          `SELECT r.*,source.primary_display_name AS source_name,
                target.primary_display_name AS target_name
           FROM entity_relationships AS r JOIN users AS source ON source.id=r.source_user_id
           JOIN users AS target ON target.id=r.target_user_id WHERE r.community_id=$1
          ORDER BY ${relationshipSorts[relationshipSort]} ${
            direction("relationship_dir")
          },r.id DESC LIMIT 500`,
          [communityId],
        ),
        this.connection.query(
          `SELECT id,report_type,subject_user_id,title,summary,generated_at,generator_version
           FROM intelligence_reports WHERE community_id=$1 ORDER BY generated_at DESC LIMIT 500`,
          [communityId],
        ),
      ]);
    return Object.freeze({
      summary: Object.freeze({ ...(summaryRows[0] ?? {}) }),
      sort: Object.freeze({
        alerts: {
          by: alertSort,
          dir: direction("alert_dir").toLocaleLowerCase(),
        },
        cases: { by: caseSort, dir: direction("case_dir").toLocaleLowerCase() },
        relationships: {
          by: relationshipSort,
          dir: direction("relationship_dir").toLocaleLowerCase(),
        },
      }),
      alerts: frozenRows(alerts),
      cases: frozenRows(cases),
      relationships: frozenRows(relationships),
      reports: frozenRows(reports),
    });
  }

  async caseDetail(
    communityId: number,
    caseId: number,
  ): Promise<IntelligenceCaseDetail | null> {
    const cases = await this.connection.query(
      "SELECT * FROM investigation_cases WHERE id=$1 AND community_id=$2",
      [caseId, communityId],
    );
    if (!cases[0]) return null;
    const [entities, evidence, activity] = await Promise.all([
      this.connection.query(
        `SELECT ce.*,u.primary_display_name FROM case_entities AS ce JOIN users AS u ON u.id=ce.user_id
          WHERE ce.case_id=$1 ORDER BY ce.added_at`,
        [caseId],
      ),
      this.connection.query(
        "SELECT * FROM case_evidence WHERE case_id=$1 ORDER BY added_at,id",
        [caseId],
      ),
      this.connection.query(
        "SELECT * FROM case_activity WHERE case_id=$1 ORDER BY created_at,id",
        [caseId],
      ),
    ]);
    return Object.freeze({
      case: Object.freeze({ ...cases[0] }),
      entities: frozenRows(entities),
      evidence: frozenRows(evidence),
      activity: frozenRows(activity),
    });
  }

  async caseAction(
    communityId: number,
    operatorId: number,
    caseId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const current = (await connection.query(
        "SELECT * FROM investigation_cases WHERE id=$1 AND community_id=$2 FOR UPDATE",
        [caseId, communityId],
      ))[0];
      if (!current) throw new TypeError("case not found");
      const action = String(input.action ?? "update").trim()
        .toLocaleLowerCase();
      let activityType: string;
      let body = "";
      if (action === "update") {
        const priority = String(input.priority ?? current.priority).trim()
          .toLocaleLowerCase();
        const status = String(input.status ?? current.status).trim()
          .toLocaleLowerCase();
        const title = String(input.title ?? current.title).trim();
        if (!new Set(["low", "medium", "high", "critical"]).has(priority)) {
          throw new TypeError("invalid case priority");
        }
        if (!new Set(["open", "active", "pending", "closed"]).has(status)) {
          throw new TypeError("invalid case status");
        }
        if (!title) throw new TypeError("case title must not be empty");
        await connection.query(
          `UPDATE investigation_cases SET title=$1,summary=$2,priority=$3,status=$4,
             owner_operator_id=COALESCE($5,owner_operator_id),updated_at=CURRENT_TIMESTAMP,
             closed_at=CASE WHEN $4='closed' THEN COALESCE(closed_at,CURRENT_TIMESTAMP) ELSE NULL END WHERE id=$6`,
          [
            title,
            String(input.summary ?? current.summary),
            priority,
            status,
            numberOrNull(input.owner_operator_id),
            caseId,
          ],
        );
        activityType = "case.updated";
      } else if (action === "add_note") {
        body = String(input.body ?? "").trim();
        if (!body) throw new TypeError("case note must not be empty");
        activityType = "note.added";
      } else if (action === "add_entity") {
        const userId = numberOrNull(input.user_id);
        if (!userId) throw new TypeError("user not found");
        const visible = await connection.query(
          `SELECT 1 FROM users WHERE id=$1 AND (EXISTS (SELECT 1 FROM messages
            WHERE messages.user_id=users.id AND messages.community_id=$2) OR EXISTS (
            SELECT 1 FROM intelligence_alerts WHERE intelligence_alerts.user_id=users.id
              AND intelligence_alerts.community_id=$2))`,
          [userId, communityId],
        );
        if (!visible.length) throw new TypeError("user not found");
        await connection.query(
          `INSERT INTO case_entities(case_id,user_id,role) VALUES ($1,$2,$3)
           ON CONFLICT(case_id,user_id) DO UPDATE SET role=EXCLUDED.role`,
          [
            caseId,
            userId,
            String(input.role ?? "subject").trim().toLocaleLowerCase() ||
            "subject",
          ],
        );
        activityType = "entity.added";
      } else if (action === "add_evidence") {
        const references = [
          "observation_id",
          "message_id",
          "signal_history_id",
          "alert_id",
        ]
          .map((key) => numberOrNull(input[key]));
        if (references.every((value) => value === null)) {
          throw new TypeError("case evidence requires an evidence reference");
        }
        await connection.query(
          `INSERT INTO case_evidence(case_id,observation_id,message_id,signal_history_id,alert_id,note)
           VALUES ($1,$2,$3,$4,$5,$6)`,
          [caseId, ...references, String(input.note ?? "").trim()],
        );
        body = String(input.note ?? "").trim();
        activityType = "evidence.added";
      } else throw new TypeError("unsupported case action");
      await connection.query(
        `INSERT INTO case_activity(case_id,operator_id,activity_type,body,payload_json)
         VALUES ($1,$2,$3,$4,'{}')`,
        [caseId, operatorId, activityType, body],
      );
      await connection.query(
        "UPDATE investigation_cases SET updated_at=CURRENT_TIMESTAMP WHERE id=$1",
        [caseId],
      );
    });
  }

  async caseFromAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
  ): Promise<number> {
    return await this.connection.transaction(async (connection) => {
      const existing = (await connection.query(
        `SELECT e.case_id FROM case_evidence AS e JOIN investigation_cases AS c ON c.id=e.case_id
          WHERE e.alert_id=$1 AND c.community_id=$2 ORDER BY e.id LIMIT 1`,
        [alertId, communityId],
      ))[0];
      if (existing) return Number(existing.case_id);
      const alert = (await connection.query(
        "SELECT user_id,title,summary,severity,signal_history_id FROM intelligence_alerts WHERE id=$1 AND community_id=$2 FOR UPDATE",
        [alertId, communityId],
      ))[0];
      if (!alert) throw new TypeError("alert not found");
      const caseId = Number(
        (await connection.query(
          `INSERT INTO investigation_cases(community_id,title,summary,priority,owner_operator_id)
         VALUES ($1,$2,$3,$4,$5) RETURNING id`,
          [
            communityId,
            String(alert.title),
            String(alert.summary),
            String(alert.severity),
            operatorId,
          ],
        ))[0]?.id,
      );
      if (alert.user_id !== null) {
        await connection.query(
          "INSERT INTO case_entities(case_id,user_id,role) VALUES ($1,$2,'subject') ON CONFLICT DO NOTHING",
          [caseId, Number(alert.user_id)],
        );
      }
      await connection.query(
        `INSERT INTO case_evidence(case_id,signal_history_id,alert_id,note)
         VALUES ($1,$2,$3,'Originating alert and signal')`,
        [
          caseId,
          alert.signal_history_id === null
            ? null
            : Number(alert.signal_history_id),
          alertId,
        ],
      );
      await connection.query(
        "UPDATE intelligence_alerts SET status='in_case',updated_at=CURRENT_TIMESTAMP WHERE id=$1",
        [alertId],
      );
      return caseId;
    });
  }

  async disposeAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
    disposition: string,
  ): Promise<void> {
    const normalized = disposition.trim().toLocaleLowerCase();
    if (
      !new Set(["confirmed", "benign", "unresolved", "escalated"]).has(
        normalized,
      )
    ) throw new TypeError("invalid disposition");
    const rows = await this.connection.query(
      `UPDATE intelligence_alerts SET status='resolved',disposition=$1,assigned_operator_id=$2,
         resolved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=$3 AND community_id=$4 RETURNING id`,
      [normalized, operatorId, alertId, communityId],
    );
    if (!rows.length) throw new TypeError("alert not found");
  }

  async updateAlert(
    communityId: number,
    operatorId: number,
    alertId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const status = input.status
      ? String(input.status).trim().toLocaleLowerCase()
      : null;
    if (
      status &&
      !new Set(["open", "acknowledged", "in_case", "resolved", "suppressed"])
        .has(status)
    ) throw new TypeError("invalid alert status");
    const rows = await this.connection.query(
      `UPDATE intelligence_alerts SET status=COALESCE($1,status),
         assigned_operator_id=COALESCE($2,assigned_operator_id),
         acknowledged_at=CASE WHEN $1='acknowledged' THEN COALESCE(acknowledged_at,CURRENT_TIMESTAMP) ELSE acknowledged_at END,
         suppressed_until=CASE WHEN $1='suppressed' THEN $3 ELSE suppressed_until END,
         updated_at=CURRENT_TIMESTAMP WHERE id=$4 AND community_id=$5 RETURNING id`,
      [
        status,
        numberOrNull(input.assigned_operator_id),
        input.suppress_until ? String(input.suppress_until) : null,
        alertId,
        communityId,
      ],
    );
    if (!rows.length) throw new TypeError("alert not found");
    await this.connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
       VALUES ('operator',$1,'alert.workflow_updated','intelligence_alert',$2,$3)`,
      [operatorId, alertId, JSON.stringify(input)],
    );
  }

  async generateReport(
    communityId: number,
    reportType: string,
    userId: number | null,
  ): Promise<number> {
    if (!new Set(["daily_summary", "entity_profile"]).has(reportType)) {
      throw new TypeError("unsupported report type");
    }
    if (reportType === "entity_profile" && userId === null) {
      throw new TypeError("Entity profile requires a user ID");
    }
    let title = "Daily Intelligence Summary";
    let content: IntelligenceItem;
    if (userId === null) {
      const [alerts, cases, relationships] = await Promise.all([
        this.connection.query(
          "SELECT id,severity,title,summary,user_id,created_at FROM intelligence_alerts WHERE status='open' AND community_id=$1 ORDER BY created_at DESC LIMIT 50",
          [communityId],
        ),
        this.connection.query(
          "SELECT id,title,priority,status,updated_at FROM investigation_cases WHERE status!='closed' AND community_id=$1 ORDER BY updated_at DESC LIMIT 50",
          [communityId],
        ),
        this.connection.query(
          "SELECT COUNT(*) AS count FROM entity_relationships WHERE community_id=$1",
          [communityId],
        ),
      ]);
      content = {
        alerts,
        cases,
        relationship_count: Number(relationships[0]?.count ?? 0),
      };
    } else {
      const user = (await this.connection.query(
        `SELECT primary_display_name,current_reputation_score FROM users WHERE id=$1 AND
          (EXISTS (SELECT 1 FROM messages WHERE messages.user_id=users.id AND messages.community_id=$2)
           OR EXISTS (SELECT 1 FROM intelligence_alerts WHERE intelligence_alerts.user_id=users.id AND intelligence_alerts.community_id=$2))`,
        [userId, communityId],
      ))[0];
      if (!user) throw new TypeError("user not found");
      title = `Entity Profile: ${user.primary_display_name}`;
      content = {
        user_id: userId,
        display_name: user.primary_display_name,
        reputation_score: user.current_reputation_score,
      };
    }
    const summary = userId === null
      ? "Current tenant intelligence summary."
      : `Intelligence profile for ${title.slice(16)}.`;
    return Number(
      (await this.connection.query(
        `INSERT INTO intelligence_reports(community_id,report_type,subject_user_id,title,summary,content_json,evidence_json)
       VALUES ($1,$2,$3,$4,$5,$6,'[]') RETURNING id`,
        [
          communityId,
          reportType,
          userId,
          title,
          summary,
          JSON.stringify(content),
        ],
      ))[0]?.id,
    );
  }

  async report(
    communityId: number,
    reportId: number,
  ): Promise<IntelligenceItem | null> {
    const row = (await this.connection.query(
      "SELECT * FROM intelligence_reports WHERE id=$1 AND community_id=$2",
      [reportId, communityId],
    ))[0];
    if (!row) return null;
    const parsed = (value: unknown) =>
      typeof value === "string" ? JSON.parse(value) : value;
    const { content_json, evidence_json, ...fields } = row;
    return Object.freeze({
      ...fields,
      content: parsed(content_json),
      evidence: parsed(evidence_json),
    });
  }
}

export class WebIntelligenceController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: IntelligenceService,
  ) {}

  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    const data = await this.service.snapshot(
      session.communityId!,
      new URL(request.url).searchParams,
    );
    const rows = data.alerts.map((item) =>
      `<li>${escapeHtml(item.severity)}: ${escapeHtml(item.title)}</li>`
    ).join("");
    return html(
      `<!doctype html><html><body><main><h1>Intelligence workspace</h1>
      <form method="get" action="/intelligence"><input name="alert_q"><select name="severity"><option value="">All severities</option></select><button>Filter alerts</button></form>
      <ul>${rows}</ul><form method="post" action="/intelligence/reports/generate"><select name="report_type"><option value="daily_summary">Daily summary</option><option value="entity_profile">Entity profile</option></select><input name="user_id" type="number"><button>Generate report</button></form></main></body></html>`,
    );
  }

  async api(request: Request): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    return Response.json(
      await this.service.snapshot(
        session.communityId!,
        new URL(request.url).searchParams,
      ),
    );
  }

  async caseResponse(
    request: Request,
    caseId: number,
    api: boolean,
  ): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    try {
      if (request.method === "POST") {
        if (!this.validOrigin(request)) {
          return Response.json({ error: "origin_mismatch" }, { status: 403 });
        }
        await this.service.caseAction(
          session.communityId!,
          Number(session.userId),
          caseId,
          await request.json() as Record<string, unknown>,
        );
      }
      const detail = await this.service.caseDetail(
        session.communityId!,
        caseId,
      );
      if (!detail) {
        return api
          ? Response.json({ error: "case not found" }, { status: 400 })
          : new Response("Case not found", { status: 404 });
      }
      if (api) return Response.json(detail);
      return html(
        `<!doctype html><html><body><main><h1>${
          escapeHtml(detail.case.title)
        }</h1>
        <form method="post" action="/intelligence/cases/${caseId}/action"><input name="action" value="add_note" type="hidden"><input name="body"><button>Add note</button></form></main></body></html>`,
      );
    } catch (error) {
      return Response.json({ error: message(error) }, { status: 400 });
    }
  }

  async caseFormAction(request: Request, caseId: number): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      await this.service.caseAction(
        session.communityId!,
        Number(session.userId),
        caseId,
        Object.fromEntries(await request.formData()),
      );
      return redirect(`/intelligence/cases/${caseId}`);
    } catch {
      return new Response("Invalid case update", { status: 400 });
    }
  }

  async alertCase(request: Request, alertId: number): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const caseId = await this.service.caseFromAlert(
        session.communityId!,
        Number(session.userId),
        alertId,
      );
      return redirect(`/intelligence/cases/${caseId}`);
    } catch (error) {
      return new Response(message(error), { status: 404 });
    }
  }

  async alertDisposition(request: Request, alertId: number): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.service.disposeAlert(
        session.communityId!,
        Number(session.userId),
        alertId,
        String(form.get("disposition") ?? ""),
      );
      return redirect("/intelligence?status=Alert+resolved");
    } catch {
      return new Response("Invalid disposition", { status: 400 });
    }
  }

  async alertWorkflow(
    request: Request,
    alertId: number,
    api: boolean,
  ): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return api
        ? Response.json({ error: "origin_mismatch" }, { status: 403 })
        : new Response("Forbidden", { status: 403 });
    }
    try {
      const input = api
        ? await request.json() as Record<string, unknown>
        : Object.fromEntries(await request.formData());
      await this.service.updateAlert(
        session.communityId!,
        Number(session.userId),
        alertId,
        input,
      );
      return api
        ? Response.json({ status: "updated", alert_id: alertId })
        : redirect("/intelligence?status=Alert+updated");
    } catch (error) {
      return api
        ? Response.json({ error: message(error) }, { status: 400 })
        : new Response("Invalid alert update", { status: 400 });
    }
  }

  async generateReport(request: Request): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    const form = await request.formData();
    try {
      const reportId = await this.service.generateReport(
        session.communityId!,
        String(form.get("report_type") ?? "daily_summary"),
        numberOrNull(form.get("user_id")),
      );
      return redirect(`/api/intelligence/reports/${reportId}`);
    } catch (error) {
      return redirect(
        `/intelligence?status=${encodeURIComponent(message(error))}`,
      );
    }
  }

  async report(request: Request, reportId: number): Promise<Response> {
    const session = await this.authorize(request, "analytics.read");
    if (session instanceof Response) return session;
    const report = await this.service.report(session.communityId!, reportId);
    return report
      ? Response.json(report)
      : Response.json({ error: "report_not_found" }, { status: 404 });
  }

  async caseExport(request: Request, caseId: number): Promise<Response> {
    const session = await this.authorize(request, "exports.create");
    if (session instanceof Response) return session;
    const detail = await this.service.caseDetail(session.communityId!, caseId);
    if (!detail) {
      return Response.json({ error: "case_not_found" }, { status: 404 });
    }
    return new Response(
      JSON.stringify(
        {
          ...detail,
          exported_at: new Date().toISOString(),
          exported_by_operator_id: Number(session.userId),
        },
        null,
        2,
      ),
      {
        headers: {
          "content-type": "application/json",
          "content-disposition":
            `attachment; filename="qbot4k-case-${caseId}.json"`,
        },
      },
    );
  }

  private async authorize(
    request: Request,
    capability: string,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) return redirect("/login");
    if (session.communityId === null || !roleAllows(session.role, capability)) {
      return new Response("Forbidden", { status: 403 });
    }
    return session;
  }

  private validOrigin(request: Request): boolean {
    const origin = request.headers.get("origin");
    return origin === null ||
      constantTimeEqual(origin, new URL(request.url).origin);
  }
}

const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const redirect = (location: string) =>
  new Response(null, { status: 302, headers: { location } });
const html = (body: string) =>
  new Response(body, {
    headers: { "content-type": "text/html; charset=utf-8" },
  });
const escapeHtml = (value: unknown) =>
  String(value ?? "").replace(/[&<>"']/gu, (character) =>
    ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]!);
