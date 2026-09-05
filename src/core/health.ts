import { SERVICE_ROLES, type ServiceRole } from "./config.ts";
import type { DatabaseHealth, DatabaseHealthSource } from "../data/database.ts";

export type ServiceStatus =
  | "starting"
  | "ready"
  | "degraded"
  | "stopping"
  | "down"
  | "disabled";

export interface HealthSnapshot {
  readonly status: "ready" | "degraded";
  readonly database: Readonly<Record<string, unknown>>;
  readonly services: Readonly<Record<ServiceRole, ServiceStatus>>;
  readonly services_detail: Readonly<
    Record<ServiceRole, Readonly<Record<string, unknown>>>
  >;
  readonly dependencies: Readonly<Record<string, "ready" | "degraded">>;
  readonly uptime: Readonly<Record<string, string | number>>;
}

export interface RoleHeartbeatStore {
  writeRoleHeartbeat(
    role: string,
    status: string,
    detail?: Readonly<Record<string, unknown>>,
  ): Promise<void>;
  roleHeartbeats(): Promise<
    Readonly<Record<string, { status: string; updatedAt: string }>>
  >;
}

const HEARTBEAT_STALE_SECONDS = 120;

export class RoleHealthMonitor {
  private status: ServiceStatus = "starting";
  private readonly startedAt: Date;

  constructor(
    private readonly database: DatabaseHealthSource,
    readonly role: ServiceRole,
    startedAt = new Date(),
    private readonly heartbeats?: RoleHeartbeatStore,
  ) {
    this.startedAt = startedAt;
  }

  setStatus(status: ServiceStatus): void {
    this.status = status;
  }

  async recordHeartbeat(): Promise<void> {
    if (!this.heartbeats) return;
    try {
      await this.heartbeats.writeRoleHeartbeat(this.role, this.status);
    } catch {
      // Heartbeats are best-effort and must not fail the role.
    }
  }

  async snapshot(now = new Date()): Promise<HealthSnapshot> {
    const database = await this.database.health();
    const remote: Readonly<
      Record<string, { status: string; updatedAt: string }>
    > = this.heartbeats
      ? await this.heartbeats.roleHeartbeats().catch(
        () => ({} as Record<string, { status: string; updatedAt: string }>),
      )
      : {};
    const allowed: readonly ServiceStatus[] = [
      "starting",
      "ready",
      "degraded",
      "stopping",
      "down",
    ];
    const services = Object.fromEntries(
      SERVICE_ROLES.map((role) => {
        if (role === this.role) return [role, this.status];
        const heartbeat = remote[role];
        if (!heartbeat) return [role, "disabled"];
        const ageSeconds = (now.valueOf() - new Date(heartbeat.updatedAt)
          .valueOf()) / 1000;
        if (ageSeconds > HEARTBEAT_STALE_SECONDS) return [role, "down"];
        const status = heartbeat.status as ServiceStatus;
        return [role, allowed.includes(status) ? status : "down"];
      }),
    ) as Record<ServiceRole, ServiceStatus>;
    const uptimeSeconds = Math.max(
      0,
      Math.floor((now.valueOf() - this.startedAt.valueOf()) / 1000),
    );
    const servicesDetail = {} as Record<
      ServiceRole,
      Readonly<Record<string, unknown>>
    >;
    for (const role of SERVICE_ROLES) {
      servicesDetail[role] = Object.freeze({
        status: services[role],
        started_at: role === this.role ? this.startedAt.toISOString() : null,
        uptime_seconds: role === this.role ? uptimeSeconds : null,
      });
    }
    const ready = database.status === "ready" && this.status === "ready";
    return Object.freeze({
      status: ready ? "ready" : "degraded",
      database: databasePayload(database),
      services: Object.freeze(services),
      services_detail: Object.freeze(servicesDetail),
      dependencies: Object.freeze({
        configuration: "ready",
        postgresql: database.status,
        migrations: database.integrity === "ok" ? "ready" : "degraded",
      }),
      uptime: Object.freeze({
        app_started_at: this.startedAt.toISOString(),
        app_uptime_seconds: uptimeSeconds,
      }),
    });
  }
}

function databasePayload(
  database: DatabaseHealth,
): Readonly<Record<string, unknown>> {
  return Object.freeze({
    status: database.status,
    path: database.path,
    backend: database.backend,
    table_count: database.tableCount,
    integrity: database.integrity,
    schema_version: database.schemaVersion,
    ...(database.error ? { error: database.error } : {}),
  });
}

export async function healthResponse(
  path: string,
  monitor: RoleHealthMonitor,
): Promise<Response> {
  const snapshot = await monitor.snapshot();
  const status = path === "/health/ready" && snapshot.status !== "ready"
    ? 503
    : 200;
  return Response.json(snapshot, { status });
}
