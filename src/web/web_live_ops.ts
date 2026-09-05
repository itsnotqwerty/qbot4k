import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { LiveOpsWorkspace } from "../../components/LiveOpsWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import {
  constantTimeEqual,
  isAllowedSameSiteOrigin,
} from "../security/security.ts";
import type { DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";
import { queueIncidentNotifications } from "../domain/notifications.ts";

export type LiveOpsSnapshot = Readonly<Record<string, unknown>>;
export interface LiveOpsControlGateway {
  shield(
    communityId: number,
    operatorId: number,
    broadcaster: string,
    active: boolean,
  ): Promise<unknown>;
  chat(
    communityId: number,
    operatorId: number,
    broadcaster: string,
    settings: Readonly<Record<string, unknown>>,
  ): Promise<unknown>;
}
export interface LiveOpsService {
  snapshot(communityId: number): Promise<LiveOpsSnapshot>;
  context(
    communityId: number,
    observationId: number,
  ): Promise<Readonly<Record<string, unknown>>>;
  moderate(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number>;
  incident(
    communityId: number,
    operatorId: number,
    incidentId: number,
    action: string,
    input: Readonly<Record<string, unknown>>,
  ): Promise<Readonly<Record<string, unknown>>>;
  handoff(
    communityId: number,
    operatorId: number,
    incomingOperatorId: number,
    note: string,
  ): Promise<number>;
  shifts(communityId: number): Promise<readonly DatabaseRow[]>;
  schedule(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  playbook(
    communityId: number,
    operatorId: number,
    key: string,
    incidentId: number | null,
  ): Promise<Readonly<Record<string, unknown>>>;
  completePlaybook(runId: number, completed: readonly unknown[]): Promise<void>;
  createDestination(
    communityId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number>;
}

export class PostgresLiveOpsRepository implements LiveOpsService {
  constructor(private readonly connection: DatabaseConnection) {}
  async snapshot(communityId: number): Promise<LiveOpsSnapshot> {
    const community = (await this.connection.query(
      "SELECT id,name,slug FROM communities WHERE id=$1",
      [communityId],
    ))[0];
    if (!community) {
      throw new TypeError(`community ${communityId} was not found`);
    }
    const liveStreams = await this.connection.query(
      "SELECT id,platform,stream_key,external_stream_id,title,category,status,started_at,ended_at,updated_at FROM stream_sessions WHERE community_id=$1 AND status='live' ORDER BY started_at DESC LIMIT 20",
      [communityId],
    );
    const metrics = (await this.connection.query(
      "SELECT COUNT(*) AS messages,COUNT(DISTINCT platform_account_id) AS chatters,COUNT(DISTINCT channel_id) AS channels FROM messages WHERE community_id=$1 AND sent_at::timestamptz>=CURRENT_TIMESTAMP-INTERVAL '5 minutes'",
      [communityId],
    ))[0] ?? {};
    const openAlerts = await this.connection.query(
      "SELECT id,observation_id,alert_type,severity,status,title,summary,confidence,assigned_operator_id,created_at FROM intelligence_alerts WHERE community_id=$1 AND status IN ('open','triaged','acknowledged','in_case') AND (suppressed_until IS NULL OR suppressed_until::timestamptz<CURRENT_TIMESTAMP) ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at DESC LIMIT 50",
      [communityId],
    );
    const incidents = await this.connection.query(
      "SELECT id,incident_type,severity,status,title,summary,escalation_level,assigned_operator_id,playbook_key,campaign_id,opened_at,updated_at FROM operations_incidents WHERE community_id=$1 AND status IN ('open','active','monitoring') ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,updated_at DESC LIMIT 30",
      [communityId],
    );
    const controls = await this.connection.query(
      "SELECT id,control_type,status,provider_status,requested_json,requested_at,confirmed_at,error_message FROM twitch_control_actions WHERE community_id=$1 ORDER BY requested_at DESC LIMIT 12",
      [communityId],
    );
    const destinations = await this.connection.query(
      "SELECT id,destination_type,name,minimum_severity,enabled,created_at FROM notification_destinations WHERE community_id=$1 ORDER BY name",
      [communityId],
    );
    const playbooks = await this.connection.query(
      "SELECT playbook_key,name,description,severity,steps_json FROM raid_playbooks WHERE enabled=1 ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,name",
    );
    const counts = (await this.connection.query(
      "SELECT COUNT(*) FILTER (WHERE ma.status='pending') AS pending_actions,COUNT(*) FILTER (WHERE ma.status='failed') AS failed_actions,COUNT(*) FILTER (WHERE ma.provider_confirmed_at IS NOT NULL) AS confirmed_actions FROM moderation_actions ma LEFT JOIN messages m ON m.id=ma.message_id WHERE m.community_id=$1 AND ma.created_at::timestamptz>=CURRENT_TIMESTAMP-INTERVAL '1 day'",
      [communityId],
    ))[0] ?? {};
    const reviews = Number(
      (await this.connection.query(
        "SELECT COUNT(*) AS count FROM review_queue q JOIN messages m ON m.id=q.message_id WHERE q.status='open' AND m.community_id=$1",
        [communityId],
      ))[0]?.count ?? 0,
    );
    const deadLetters = Number(
      (await this.connection.query(
        "SELECT COUNT(*) AS count FROM dead_letter_events WHERE community_id=$1 AND status='open'",
        [communityId],
      ))[0]?.count ?? 0,
    );
    const messages = Number(metrics.messages ?? 0);
    const watermark = [
      openAlerts[0]?.id ?? 0,
      incidents[0]?.id ?? 0,
      controls[0]?.id ?? 0,
    ].join(":");
    return {
      generated_at: new Date().toISOString(),
      watermark,
      community,
      live_streams: liveStreams,
      last_5_minutes: {
        messages,
        unique_chatters: Number(metrics.chatters ?? 0),
        channels: Number(metrics.channels ?? 0),
        messages_per_minute: Math.round(messages * 2) / 10,
        current_velocity: 0,
      },
      velocity: [],
      timeline: [],
      open_alerts: openAlerts,
      active_incidents: incidents,
      active_campaigns: incidents,
      controls,
      playbooks,
      briefings: [],
      notification_destinations: destinations,
      cohorts: {},
      audience_graph: [],
      moderator_workload: {},
      operations: {
        pending_actions: Number(counts.pending_actions ?? 0),
        failed_actions: Number(counts.failed_actions ?? 0),
        provider_confirmed_actions: Number(counts.confirmed_actions ?? 0),
        open_reviews: reviews,
        dead_letters: deadLetters,
      },
    };
  }
  async context(
    communityId: number,
    observationId: number,
  ): Promise<Readonly<Record<string, unknown>>> {
    const finding = (await this.connection.query(
      "SELECT id,community_id,platform,COALESCE(context_id,container_id,'') AS context_id,occurred_at FROM observations WHERE id=$1 AND community_id=$2",
      [observationId, communityId],
    ))[0];
    if (!finding) throw new TypeError("observation_not_found");
    const common = `SELECT o.id AS observation_id,o.event_type,o.occurred_at,
                           COALESCE(o.text_raw,'') AS text,o.attributes_json AS attributes,
                           COALESCE(pa.username,'system') AS username,
                           COALESCE(pa.platform_user_id,'') AS platform_user_id,
                           pa.user_id,m.id AS message_id
                      FROM observations o
                      LEFT JOIN platform_accounts pa ON pa.id=o.actor_platform_account_id
                      LEFT JOIN messages m ON m.observation_id=o.id
                     WHERE o.community_id=$1 AND o.platform=$2
                       AND COALESCE(o.context_id,o.container_id,'')=$3`;
    const parameters = [
      communityId,
      String(finding.platform),
      String(finding.context_id),
      finding.occurred_at as string | Date,
      observationId,
    ] as const;
    const prior = await this.connection.query(
      `${common}
        AND (o.occurred_at::timestamptz<$4::timestamptz OR (o.occurred_at::timestamptz=$4::timestamptz AND o.id<=$5))
       ORDER BY o.occurred_at DESC,o.id DESC LIMIT 12`,
      parameters,
    );
    const following = await this.connection.query(
      `${common}
        AND (o.occurred_at::timestamptz>$4::timestamptz OR (o.occurred_at::timestamptz=$4::timestamptz AND o.id>$5))
       ORDER BY o.occurred_at,o.id LIMIT 6`,
      parameters,
    );
    return {
      finding_observation_id: observationId,
      community_id: communityId,
      platform: String(finding.platform),
      context_id: String(finding.context_id),
      items: [...prior.toReversed(), ...following].map((row) => ({
        ...row,
        attributes: decodeObject(row.attributes),
        is_finding: Number(row.observation_id) === observationId,
      })),
    };
  }
  async moderate(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const messageId = Number(input.message_id);
    const action = String(input.action_type ?? "").trim().toLocaleLowerCase();
    if (!["warn", "timeout", "ban"].includes(action)) {
      throw new TypeError("invalid_action_type");
    }
    const duration = Math.max(
      1,
      Math.min(Number(input.duration_seconds) || 600, 2_419_200),
    );
    const reason = String(input.reason ?? "Live operations keyboard action")
      .slice(0, 500);
    return await this.connection.transaction(async (connection) => {
      const message = (await connection.query(
        "SELECT id,platform,observation_id,platform_account_id FROM messages WHERE id=$1 AND community_id=$2",
        [messageId, communityId],
      ))[0];
      if (!message) throw new TypeError("message_not_found");
      const actionId = Number(
        (await connection.query(
          "INSERT INTO moderation_actions(platform,message_id,target_platform_account_id,action_type,reason,status,actor_type,actor_id,community_id,duration_seconds,assigned_operator_id) VALUES ($1,$2,$3,$4,$5,'pending','operator',$6,$7,$8,$6) RETURNING id",
          [
            String(message.platform),
            messageId,
            Number(message.platform_account_id),
            action,
            reason,
            operatorId,
            communityId,
            duration,
          ],
        ))[0]?.id,
      );
      await connection.query(
        "INSERT INTO processing_jobs(stage,job_type,observation_id,payload_json,idempotency_key,priority) VALUES ('action',$1,$2,$3,$4,5) ON CONFLICT(idempotency_key) DO NOTHING",
        [
          `${message.platform}.moderation.execute`,
          message.observation_id ? Number(message.observation_id) : null,
          JSON.stringify({ message_id: messageId }),
          `liveops:${actionId}:execute`,
        ],
      );
      return actionId;
    });
  }
  async incident(
    communityId: number,
    operatorId: number,
    incidentId: number,
    action: string,
    input: Readonly<Record<string, unknown>>,
  ): Promise<Readonly<Record<string, unknown>>> {
    return await this.connection.transaction(async (connection) => {
      const incident = (await connection.query(
        "SELECT escalation_level,status FROM operations_incidents WHERE id=$1 AND community_id=$2 FOR UPDATE",
        [incidentId, communityId],
      ))[0];
      if (!incident) throw new TypeError("incident_not_found");
      if (["closed", "resolved"].includes(String(incident.status))) {
        throw new TypeError("open incident was not found");
      }
      if (action === "assign") {
        const assigned = Number(input.operator_id) || operatorId;
        await connection.query(
          "UPDATE operations_incidents SET assigned_operator_id=$1,status='active',updated_at=CURRENT_TIMESTAMP WHERE id=$2",
          [assigned, incidentId],
        );
        await this.activity(
          connection,
          incidentId,
          operatorId,
          "assigned",
          "",
          { assigned_operator_id: assigned },
        );
        return { incident_id: incidentId, assigned_operator_id: assigned };
      }
      if (action === "escalate") {
        const level = Math.min(3, Number(incident.escalation_level) + 1);
        await connection.query(
          "UPDATE operations_incidents SET escalation_level=$1,severity=CASE WHEN severity IN ('info','low','medium') THEN 'high' ELSE 'critical' END,updated_at=CURRENT_TIMESTAMP WHERE id=$2",
          [level, incidentId],
        );
        await this.activity(
          connection,
          incidentId,
          operatorId,
          "escalated",
          String(input.note ?? ""),
          { level },
        );
        await queueIncidentNotifications(connection, incidentId, true);
        return { incident_id: incidentId, escalation_level: level };
      }
      if (action === "route-on-call") {
        const onCall = (await connection.query(
          "SELECT operator_id FROM moderation_shift_schedules WHERE community_id=$1 AND status IN ('scheduled','active') AND starts_at::timestamptz<=CURRENT_TIMESTAMP AND ends_at::timestamptz>CURRENT_TIMESTAMP ORDER BY starts_at DESC,id DESC LIMIT 1",
          [communityId],
        ))[0];
        if (!onCall) throw new TypeError("no operator is currently on call");
        const assigned = Number(onCall.operator_id);
        await connection.query(
          "UPDATE operations_incidents SET assigned_operator_id=$1,status='active',updated_at=CURRENT_TIMESTAMP WHERE id=$2",
          [assigned, incidentId],
        );
        await this.activity(
          connection,
          incidentId,
          operatorId,
          "routed_on_call",
          "",
          { assigned_operator_id: assigned },
        );
        return { incident_id: incidentId, assigned_operator_id: assigned };
      }
      throw new TypeError("unsupported_incident_action");
    });
  }
  async handoff(
    communityId: number,
    operatorId: number,
    incomingOperatorId: number,
    note: string,
  ): Promise<number> {
    return await this.connection.transaction(async (connection) => {
      await connection.query(
        "UPDATE moderation_shifts SET status='handed_off',ended_at=CURRENT_TIMESTAMP,incoming_operator_id=$1,handoff_note=$2,handoff_at=CURRENT_TIMESTAMP WHERE id=(SELECT id FROM moderation_shifts WHERE community_id=$3 AND status='active' ORDER BY started_at DESC LIMIT 1)",
        [incomingOperatorId, note.trim(), communityId],
      );
      const id = Number(
        (await connection.query(
          "INSERT INTO moderation_shifts(community_id,lead_operator_id,status,handoff_note) VALUES ($1,$2,'active',$3) RETURNING id",
          [communityId, incomingOperatorId, note.trim()],
        ))[0]?.id,
      );
      await connection.query(
        "UPDATE operations_incidents SET assigned_operator_id=$1,updated_at=CURRENT_TIMESTAMP WHERE community_id=$2 AND assigned_operator_id=$3 AND status IN ('open','active')",
        [incomingOperatorId, communityId, operatorId],
      );
      return id;
    });
  }
  shifts(communityId: number) {
    return this.connection.query(
      "SELECT s.id,s.operator_id,o.discord_username,s.starts_at,s.ends_at,s.status FROM moderation_shift_schedules s JOIN operator_accounts o ON o.id=s.operator_id WHERE s.community_id=$1 ORDER BY s.starts_at,s.id",
      [communityId],
    );
  }
  async schedule(
    communityId: number,
    operatorId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const assigned = Number(input.operator_id);
    const start = new Date(String(input.starts_at ?? ""));
    const end = new Date(String(input.ends_at ?? ""));
    if (Number.isNaN(start.valueOf()) || Number.isNaN(end.valueOf())) {
      throw new TypeError("invalid shift timestamp");
    }
    if (end <= start) throw new TypeError("shift end must be after its start");
    await this.connection.transaction(async (connection) => {
      if (
        !(await connection.query(
          "SELECT 1 FROM operator_community_roles WHERE operator_id=$1 AND community_id=$2",
          [assigned, communityId],
        ))[0]
      ) {
        throw new TypeError(
          "on-call operator is not assigned to this community",
        );
      }
      if (
        (await connection.query(
          "SELECT 1 FROM moderation_shift_schedules WHERE community_id=$1 AND status IN ('scheduled','active') AND starts_at::timestamptz<$2::timestamptz AND ends_at::timestamptz>$3::timestamptz",
          [communityId, end.toISOString(), start.toISOString()],
        ))[0]
      ) throw new TypeError("shift overlaps an existing on-call schedule");
      const id = Number(
        (await connection.query(
          "INSERT INTO moderation_shift_schedules(community_id,operator_id,starts_at,ends_at,created_by_operator_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
          [
            communityId,
            assigned,
            start.toISOString(),
            end.toISOString(),
            operatorId,
          ],
        ))[0]?.id,
      );
      await connection.query(
        "INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,'operations.shift_scheduled','moderation_shift_schedule',$2,$3)",
        [
          operatorId,
          id,
          JSON.stringify({
            community_id: communityId,
            operator_id: assigned,
            starts_at: start.toISOString(),
            ends_at: end.toISOString(),
          }),
        ],
      );
    });
  }
  async playbook(
    communityId: number,
    operatorId: number,
    key: string,
    incidentId: number | null,
  ): Promise<Readonly<Record<string, unknown>>> {
    return await this.connection.transaction(async (connection) => {
      const playbook = (await connection.query(
        "SELECT name,severity,steps_json FROM raid_playbooks WHERE playbook_key=$1 AND enabled=1",
        [key],
      ))[0];
      if (!playbook) throw new TypeError("enabled playbook was not found");
      if (
        incidentId !== null &&
        !(await connection.query(
          "SELECT 1 FROM operations_incidents WHERE id=$1 AND community_id=$2",
          [incidentId, communityId],
        ))[0]
      ) throw new TypeError("tenant incident was not found");
      const steps = JSON.parse(String(playbook.steps_json)) as unknown[];
      const runId = Number(
        (await connection.query(
          "INSERT INTO raid_playbook_runs(community_id,incident_id,playbook_key,activated_by_operator_id,state_json) VALUES ($1,$2,$3,$4,$5) RETURNING id",
          [
            communityId,
            incidentId,
            key,
            operatorId,
            JSON.stringify({ steps, completed: [] }),
          ],
        ))[0]?.id,
      );
      if (incidentId !== null) {
        await connection.query(
          "UPDATE operations_incidents SET playbook_key=$1,status='active',updated_at=CURRENT_TIMESTAMP WHERE id=$2 AND community_id=$3",
          [key, incidentId, communityId],
        );
      }
      return {
        run_id: runId,
        playbook_key: key,
        name: playbook.name,
        severity: playbook.severity,
        steps,
      };
    });
  }
  async completePlaybook(
    runId: number,
    completed: readonly unknown[],
  ): Promise<void> {
    await this.connection.query(
      "UPDATE raid_playbook_runs SET status='completed',current_step=$1,state_json=$2,completed_at=CURRENT_TIMESTAMP WHERE id=$3",
      [completed.length, JSON.stringify({ completed }), runId],
    );
  }
  async createDestination(
    communityId: number,
    input: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const type = String(input.destination_type ?? "").trim()
      .toLocaleLowerCase();
    const severity = String(input.minimum_severity ?? "high").trim()
      .toLocaleLowerCase();
    const target = String(input.target ?? "").trim();
    if (
      !["discord_webhook", "slack_webhook", "generic_webhook"].includes(type)
    ) throw new TypeError("unsupported notification destination type");
    if (!target.startsWith("https://")) {
      throw new TypeError("notification destinations must use HTTPS");
    }
    if (!["info", "low", "medium", "high", "critical"].includes(severity)) {
      throw new TypeError("unsupported minimum severity");
    }
    return Number(
      (await this.connection.query(
        "INSERT INTO notification_destinations(community_id,destination_type,name,target,minimum_severity) VALUES ($1,$2,$3,$4,$5) RETURNING id",
        [communityId, type, String(input.name ?? "").trim(), target, severity],
      ))[0]?.id,
    );
  }
  private async activity(
    connection: DatabaseConnection,
    incidentId: number,
    operatorId: number,
    activityType: string,
    note: string,
    payload: Readonly<Record<string, unknown>>,
  ) {
    await connection.query(
      "INSERT INTO incident_activity(incident_id,operator_id,activity_type,body,payload_json) VALUES ($1,$2,$3,$4,$5)",
      [incidentId, operatorId, activityType, note, JSON.stringify(payload)],
    );
  }
}

export class WebLiveOpsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: LiveOpsService,
    private readonly controls: LiveOpsControlGateway,
    private readonly defaultBroadcaster = "",
  ) {}
  async page(request: Request) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    const snapshot = await this.service.snapshot(session.communityId!);
    const url = new URL(request.url);
    return new Response(
      dashboardDocument(
        render(
          h(LiveOpsWorkspace, {
            snapshot,
            canManage: roleAllows(session.role, "admin.manage"),
            canGoLive: roleAllows(session.role, "operators.manage"),
            status: url.searchParams.get("status") ?? "",
          }),
        ),
        "Live operations | QBot4K",
      ),
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  async api(request: Request) {
    const session = await this.authorize(request);
    return session instanceof Response
      ? session
      : Response.json(await this.service.snapshot(session.communityId!));
  }
  async context(request: Request, observationId: number) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    if (!Number.isSafeInteger(observationId) || observationId <= 0) {
      return Response.json({ error: "invalid_observation_id" }, {
        status: 400,
      });
    }
    try {
      return Response.json(
        await this.service.context(session.communityId!, observationId),
      );
    } catch (error) {
      const message = this.message(error);
      return Response.json({ error: message }, {
        status: message === "observation_not_found" ? 404 : 400,
      });
    }
  }
  async stream(request: Request) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    const payload = await this.service.snapshot(session.communityId!);
    const wire = `event: snapshot\nid: ${
      String(payload.watermark ?? "")
    }\ndata: ${JSON.stringify(payload)}\n\n`;
    return new Response(wire, {
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache, no-transform",
        "x-accel-buffering": "no",
      },
    });
  }
  async moderate(request: Request) {
    return await this.mutate(request, async (session, payload) => {
      const action = String(payload.action_type ?? "").trim()
        .toLocaleLowerCase();
      if (
        action === "ban" &&
        !constantTimeEqual(String(payload.confirmation ?? ""), "PERMANENT BAN")
      ) {
        return Response.json({ error: "permanent_ban_confirmation_required" }, {
          status: 409,
        });
      }
      try {
        const actionId = await this.service.moderate(
          session.communityId!,
          Number(session.userId),
          payload,
        );
        return Response.json({
          action_id: actionId,
          status: "pending_provider_confirmation",
        }, { status: 202 });
      } catch (error) {
        const message = this.message(error);
        return Response.json({ error: message }, {
          status: message === "message_not_found" ? 404 : 400,
        });
      }
    });
  }
  async incident(request: Request, incidentId: number, action: string) {
    return await this.mutate(request, async (session, payload) => {
      try {
        return Response.json(
          await this.service.incident(
            session.communityId!,
            Number(session.userId),
            incidentId,
            action,
            payload,
          ),
        );
      } catch (error) {
        const message = this.message(error);
        return Response.json({ error: message }, {
          status: message === "incident_not_found" ||
              message === "unsupported_incident_action"
            ? 404
            : 400,
        });
      }
    });
  }
  async handoff(request: Request) {
    return await this.mutate(request, async (session, payload) => {
      try {
        const shiftId = await this.service.handoff(
          session.communityId!,
          Number(session.userId),
          Number(payload.incoming_operator_id),
          String(payload.note ?? ""),
        );
        return Response.json({ shift_id: shiftId, status: "handed_off" });
      } catch (error) {
        return Response.json({ error: this.message(error) }, { status: 400 });
      }
    });
  }
  async shifts(request: Request) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    if (request.method === "POST") {
      if (!this.origin(request)) {
        return new Response("Forbidden", { status: 403 });
      }
      try {
        await this.service.schedule(
          session.communityId!,
          Number(session.userId),
          await request.json() as Record<string, unknown>,
        );
      } catch (error) {
        return Response.json({ error: this.message(error) }, { status: 400 });
      }
    }
    return Response.json({
      shifts: await this.service.shifts(session.communityId!),
    });
  }
  async destination(request: Request) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    if (!roleAllows(session.role, "admin.manage")) {
      return new Response("Forbidden", { status: 403 });
    }
    if (!this.origin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const id = await this.service.createDestination(
        session.communityId!,
        await request.json() as Record<string, unknown>,
      );
      return Response.json({ destination_id: id }, { status: 201 });
    } catch (error) {
      return Response.json({ error: this.message(error) }, { status: 400 });
    }
  }
  async shield(request: Request) {
    return await this.control(
      request,
      (session, payload) =>
        this.controls.shield(
          session.communityId!,
          Number(session.userId),
          String(payload.broadcaster ?? this.defaultBroadcaster),
          Boolean(payload.active),
        ),
    );
  }
  async chat(request: Request) {
    return await this.control(request, (session, payload) => {
      const settings = payload.settings;
      if (
        !settings || typeof settings !== "object" || Array.isArray(settings)
      ) return Promise.reject(new TypeError("settings_object_required"));
      return this.controls.chat(
        session.communityId!,
        Number(session.userId),
        String(payload.broadcaster ?? this.defaultBroadcaster),
        settings as Record<string, unknown>,
      );
    });
  }
  async playbook(request: Request, key: string) {
    return await this.mutate(request, async (session, payload) => {
      try {
        const incidentId = payload.incident_id == null
          ? null
          : Number(payload.incident_id);
        const run = await this.service.playbook(
          session.communityId!,
          Number(session.userId),
          key,
          incidentId,
        );
        const completed = (run.steps as Array<Record<string, unknown>>).map((
          step,
        ) => ({ key: step.key, result: "recorded" }));
        await this.service.completePlaybook(Number(run.run_id), completed);
        return Response.json({ ...run, completed });
      } catch (error) {
        return Response.json({ error: this.message(error) }, { status: 502 });
      }
    });
  }
  private async control(
    request: Request,
    operation: (
      session: DashboardSession,
      payload: Record<string, unknown>,
    ) => Promise<unknown>,
  ) {
    return await this.mutate(request, async (session, payload) => {
      try {
        return Response.json(await operation(session, payload));
      } catch (error) {
        return Response.json({ error: this.message(error) }, { status: 502 });
      }
    });
  }
  private async mutate(
    request: Request,
    operation: (
      session: DashboardSession,
      payload: Record<string, unknown>,
    ) => Promise<Response>,
  ) {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    if (!this.origin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      return await operation(
        session,
        await request.json() as Record<string, unknown>,
      );
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
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
    if (session.communityId === null) {
      return new Response("Select a community", { status: 403 });
    }
    return session;
  }
  private origin(request: Request) {
    return isAllowedSameSiteOrigin(request);
  }
  private message(error: unknown) {
    return error instanceof Error ? error.message : String(error);
  }
}

function decodeObject(value: unknown): Readonly<Record<string, unknown>> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Readonly<Record<string, unknown>>;
  }
  try {
    const parsed = JSON.parse(String(value ?? "{}"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Readonly<Record<string, unknown>>
      : {};
  } catch {
    return {};
  }
}

export class UnavailableLiveOpsControlGateway implements LiveOpsControlGateway {
  shield(): Promise<unknown> {
    return Promise.reject(
      new TypeError("Twitch authorization is not configured"),
    );
  }
  chat(): Promise<unknown> {
    return Promise.reject(
      new TypeError("Twitch authorization is not configured"),
    );
  }
}
