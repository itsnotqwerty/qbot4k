import type { DatabaseConnection } from "../data/database.ts";
import { DISCORD_MESSAGE_JOB_TYPE } from "../providers/discord/discord_actions.ts";
import type { DashboardOperations } from "./web_dashboard.ts";

export class PostgresDashboardOperations implements DashboardOperations {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly serviceName: string,
    private readonly restartService: (serviceName: string) => void =
      scheduleSystemdRestart,
  ) {}

  async goLive(communityId: number, operatorId: number): Promise<number> {
    const targets = await this.connection.query(
      `SELECT session.id AS stream_id,session.stream_key,session.title,
              installation.id AS installation_id,channel.channel_id
         FROM stream_sessions session
         JOIN community_installations installation
           ON installation.community_id=session.community_id
          AND installation.platform='discord' AND installation.status='active'
          AND installation.capabilities_json::jsonb ? 'announcements'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=installation.id
              AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
         JOIN LATERAL (
           SELECT channel_id FROM discord_channels
            WHERE guild_id=installation.external_community_id
            ORDER BY CASE
              WHEN lower(channel_name) IN ('live','streams','announcements') THEN 0
              WHEN lower(channel_name)='general' THEN 1 ELSE 2 END,
              channel_name,channel_id LIMIT 1
         ) channel ON TRUE
        WHERE session.community_id=$1 AND session.platform='twitch'
          AND session.status='live'
        ORDER BY session.started_at,session.id,installation.id`,
      [communityId],
    );
    for (const target of targets) {
      await this.connection.transaction(async (connection) => {
        const externalEventId = `manual-live:${crypto.randomUUID()}`;
        const title = String(target.title ?? "").trim();
        const streamKey = String(target.stream_key);
        const body =
          `@here ${streamKey} is live: https://www.twitch.tv/${streamKey.toLocaleLowerCase()}${
            title ? ` - ${title}` : ""
          }`;
        const observation = (await connection.query(
          `INSERT INTO observations(
             platform,community_id,installation_id,event_type,external_event_id,
             container_id,context_id,text_raw,attributes_json,occurred_at
           ) VALUES ('discord',$1,$2,'stream.announcement.manual',$3,$4,$4,$5,$6,$7)
           RETURNING id`,
          [
            communityId,
            Number(target.installation_id),
            externalEventId,
            String(target.channel_id),
            body,
            JSON.stringify({
              stream_id: target.stream_id,
              stream_key: streamKey,
            }),
            new Date().toISOString(),
          ],
        ))[0];
        await connection.query(
          `INSERT INTO processing_jobs(
             community_id,stage,job_type,observation_id,payload_json,idempotency_key
           ) VALUES ($1,'action',$2,$3,$4,$5)`,
          [
            communityId,
            DISCORD_MESSAGE_JOB_TYPE,
            Number(observation.id),
            JSON.stringify({
              channel_id: String(target.channel_id),
              rendered_reply: { content: body },
            }),
            externalEventId,
          ],
        );
      });
    }
    await this.audit(operatorId, "dashboard.go_live_requested", communityId, {
      announcements: targets.length,
    });
    return targets.length;
  }

  async restart(operatorId: number): Promise<string> {
    await this.audit(operatorId, "dashboard.restart_requested", null, {
      service: this.serviceName,
    });
    this.restartService(this.serviceName);
    return this.serviceName;
  }

  async resetDatabase(
    operatorId: number,
  ): Promise<{ readonly rowsDeleted: number }> {
    return await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtext('qbot4k:dashboard-reset'))",
      );
      const tables = (await connection.query(
        `SELECT table_name FROM information_schema.tables
          WHERE table_schema=current_schema() AND table_type='BASE TABLE'
            AND table_name<>'schema_migrations' ORDER BY table_name`,
      )).map((row) => identifier(row.table_name));
      let rowsDeleted = 0;
      for (const table of tables) {
        rowsDeleted += Number(
          (await connection.query(
            `SELECT COUNT(*)::int AS count FROM "${table}"`,
          ))[0]?.count ?? 0,
        );
      }
      if (tables.length) {
        await connection.query(
          `TRUNCATE TABLE ${
            tables.map((table) => `"${table}"`).join(",")
          } RESTART IDENTITY CASCADE`,
        );
      }
      await connection.query(
        `INSERT INTO organizations(id,name,slug) VALUES (1,'Default Organization','default');
         INSERT INTO workspaces(id,organization_id,name,slug) VALUES (1,1,'Default Workspace','default');
         INSERT INTO communities(id,workspace_id,name,slug) VALUES (1,1,'Default Community','default');
         INSERT INTO community_policy_settings(community_id) VALUES (1)`,
      );
      await connection.query(
        `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
         VALUES ('operator',$1,'dashboard.database_reset','database',NULL,$2)`,
        [
          operatorId,
          JSON.stringify({
            rows_deleted: rowsDeleted,
            tables_cleared: tables.length,
          }),
        ],
      );
      return Object.freeze({ rowsDeleted });
    });
  }

  private async audit(
    operatorId: number,
    action: string,
    communityId: number | null,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    await this.connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
       VALUES ('operator',$1,$2,$3,$4,$5)`,
      [
        operatorId,
        action,
        communityId === null ? "system_service" : "community",
        communityId,
        JSON.stringify(payload),
      ],
    );
  }
}

function identifier(value: unknown): string {
  const name = String(value);
  if (!/^[a-z][a-z0-9_]*$/u.test(name)) {
    throw new TypeError("invalid database identifier");
  }
  return name;
}

function scheduleSystemdRestart(serviceName: string): void {
  setTimeout(() => {
    new Deno.Command("systemctl", { args: ["restart", serviceName] })
      .output().catch(() => undefined);
  }, 500);
}
