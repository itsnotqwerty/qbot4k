import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";

const SEVERITY_RANK: Readonly<Record<string, number>> = Object.freeze({
  info: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
});

export interface NotificationGateway {
  post(
    target: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<number>;
}

export class FetchNotificationGateway implements NotificationGateway {
  async post(
    target: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const response = await fetch(target, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": "qbot4k/2.0",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10_000),
    });
    await response.body?.cancel();
    return response.status;
  }
}

export interface NotificationService {
  queueIncident(incidentId: number, force?: boolean): Promise<number>;
  dispatchPending(communityId: number, limit?: number): Promise<number>;
}

export class PostgresNotificationRepository implements NotificationService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly gateway: NotificationGateway =
      new FetchNotificationGateway(),
  ) {}

  async queueIncident(incidentId: number, force = false): Promise<number> {
    positiveInteger(incidentId, "incident_id");
    return await this.connection.transaction((connection) =>
      queueIncidentNotifications(connection, incidentId, force)
    );
  }

  async dispatchPending(communityId: number, limit = 25): Promise<number> {
    positiveInteger(communityId, "community_id");
    const boundedLimit = Math.max(1, Math.trunc(limit));
    return await this.connection.transaction(async (connection) => {
      const rows = await connection.query(
        `SELECT d.id,d.payload_json,n.destination_type,n.target
           FROM notification_deliveries d
           JOIN notification_destinations n ON n.id=d.destination_id
          WHERE d.status IN ('pending','retry') AND n.enabled=1
            AND d.attempts<5 AND n.community_id=$1
          ORDER BY d.created_at,d.id
          FOR UPDATE OF d SKIP LOCKED
          LIMIT $2`,
        [communityId, boundedLimit],
      );
      let delivered = 0;
      for (const row of rows) {
        try {
          const providerStatus = await this.gateway.post(
            String(row.target),
            wirePayload(String(row.destination_type), jsonObject(row)),
          );
          if (providerStatus < 200 || providerStatus >= 300) {
            throw new Error(
              `notification provider returned HTTP ${providerStatus}`,
            );
          }
          await connection.query(
            `UPDATE notification_deliveries
                SET status='delivered',attempts=attempts+1,
                    delivered_at=CURRENT_TIMESTAMP,last_error=NULL
              WHERE id=$1`,
            [positiveInteger(row.id, "delivery_id")],
          );
          delivered += 1;
        } catch (error) {
          await connection.query(
            `UPDATE notification_deliveries
                SET status='retry',attempts=attempts+1,last_error=$1
              WHERE id=$2`,
            [boundedError(error), positiveInteger(row.id, "delivery_id")],
          );
        }
      }
      return delivered;
    });
  }
}

export async function queueIncidentNotifications(
  connection: DatabaseConnection,
  incidentId: number,
  force = false,
): Promise<number> {
  positiveInteger(incidentId, "incident_id");
  await connection.query(
    "SELECT pg_advisory_xact_lock(hashtext('qbot4k:incident_notification:' || $1))",
    [incidentId],
  );
  const incident = (await connection.query(
    `SELECT id,community_id,severity,title,summary,status,escalation_level
       FROM operations_incidents WHERE id=$1`,
    [incidentId],
  ))[0];
  if (!incident) return 0;
  const destinations = await connection.query(
    `SELECT id,minimum_severity FROM notification_destinations
      WHERE community_id=$1 AND enabled=1`,
    [positiveInteger(incident.community_id, "community_id")],
  );
  const incidentSeverity = String(incident.severity);
  let queued = 0;
  for (const destination of destinations) {
    const minimumSeverity = String(destination.minimum_severity);
    if (
      !force && severityRank(incidentSeverity, 0) <
        severityRank(minimumSeverity, 3)
    ) continue;
    const destinationId = positiveInteger(destination.id, "destination_id");
    const payload = JSON.stringify(canonicalValue({
      incident_id: incidentId,
      severity: incidentSeverity,
      title: String(incident.title),
      summary: String(incident.summary),
      status: String(incident.status),
      escalation_level: Number(incident.escalation_level),
    }));
    const inserted = await connection.query(
      `INSERT INTO notification_deliveries(
         destination_id,incident_id,payload_json
       )
       SELECT $1,$2,$3 WHERE NOT EXISTS (
         SELECT 1 FROM notification_deliveries
          WHERE destination_id=$1 AND incident_id=$2
            AND status IN ('pending','delivered')
       )
       RETURNING id`,
      [destinationId, incidentId, payload],
    );
    if (inserted[0]) queued += 1;
  }
  return queued;
}

function jsonObject(row: DatabaseRow): Readonly<Record<string, unknown>> {
  try {
    const value = typeof row.payload_json === "string"
      ? JSON.parse(row.payload_json)
      : row.payload_json;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value;
    }
  } catch {
    // Match Python's empty-object fallback for malformed persisted JSON.
  }
  return {};
}

function wirePayload(
  destinationType: string,
  payload: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const severity = String(payload.severity ?? "high").toLocaleUpperCase();
  const title = String(payload.title ?? "Incident");
  if (destinationType === "discord_webhook") {
    return {
      content: `[${severity}] ${title}`,
      embeds: [{
        description: String(payload.summary ?? "").slice(0, 4000),
        fields: [{
          name: "Incident",
          value: String(payload.incident_id ?? "unknown"),
        }],
      }],
    };
  }
  if (destinationType === "slack_webhook") {
    return {
      text: `[${severity}] ${title}\n${String(payload.summary ?? "")}`,
    };
  }
  return payload;
}

function severityRank(severity: string, fallback: number): number {
  return SEVERITY_RANK[severity] ?? fallback;
}

function canonicalValue(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(canonicalValue);
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalValue(item)]),
  );
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}

function boundedError(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(
    0,
    1000,
  );
}
