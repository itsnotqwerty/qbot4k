import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";

export interface AnnouncementSender {
  send(
    platform: string,
    externalCommunityId: string,
    targetExternalId: string,
    body: string,
    source: Readonly<Record<string, unknown>>,
  ): Promise<string>;
}

export interface AnnouncementDispatchService {
  dispatch(
    now?: Date,
    limit?: number,
    perCommunityLimit?: number,
  ): Promise<number>;
}

export class PostgresAnnouncementDispatcher
  implements AnnouncementDispatchService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly sender: AnnouncementSender,
  ) {}

  async dispatch(
    now = new Date(),
    limit = 50,
    perCommunityLimit = 5,
  ): Promise<number> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    const rows = await this.connection.query(
      `WITH due AS (
         SELECT a.id,a.community_id,a.platform,a.target_external_id,a.body,
                i.id AS installation_id,i.external_community_id,a.source_json,
                i.capabilities_json,
                ROW_NUMBER() OVER (
                  PARTITION BY a.community_id ORDER BY a.scheduled_at,a.id
                ) AS community_rank,
                a.scheduled_at
           FROM community_announcements a
           JOIN community_installations i
             ON i.id=a.target_installation_id AND i.community_id=a.community_id
            AND i.platform=a.platform AND i.status='active'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=i.id AND lease.owner_runtime='deno'
                AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
          WHERE a.status='scheduled'
            AND a.scheduled_at::timestamptz<=$1::timestamptz
            AND (SELECT COUNT(*) FROM community_announcement_deliveries d
                  WHERE d.announcement_id=a.id)<3
       )
       SELECT * FROM due WHERE community_rank<=$2
        ORDER BY community_rank,scheduled_at,id LIMIT $3`,
      [
        now.toISOString(),
        Math.max(1, Math.min(Math.trunc(perCommunityLimit), 100)),
        Math.max(1, Math.min(Math.trunc(limit), 500)),
      ],
    );
    let delivered = 0;
    for (const row of rows) {
      const claimed = await this.claim(row);
      if (!claimed) continue;
      try {
        requireAnnouncementCapability(row);
        const providerMessageId = await this.sender.send(
          String(row.platform),
          String(row.external_community_id),
          String(row.target_external_id),
          String(row.body),
          jsonObject(row.source_json),
        );
        await this.finish(row, claimed, providerMessageId);
        delivered += 1;
      } catch (error) {
        await this.fail(row, claimed, error);
      }
    }
    return delivered;
  }

  private async claim(row: DatabaseRow): Promise<number | null> {
    return await this.connection.transaction(async (connection) => {
      const announcementId = positiveInteger(row.id, "announcement_id");
      const updated = await connection.query(
        `UPDATE community_announcements SET status='sending',updated_at=CURRENT_TIMESTAMP
          WHERE id=$1 AND status='scheduled' RETURNING id`,
        [announcementId],
      );
      if (!updated[0]) return null;
      const attempt = Number(
        (await connection.query(
          `SELECT COUNT(*)+1 AS attempt FROM community_announcement_deliveries
          WHERE announcement_id=$1`,
          [announcementId],
        ))[0]?.attempt ?? 1,
      );
      return positiveInteger(
        (await connection.query(
          `INSERT INTO community_announcement_deliveries(
           announcement_id,installation_id,attempt_number
         ) VALUES ($1,$2,$3) RETURNING id`,
          [
            announcementId,
            positiveInteger(row.installation_id, "installation_id"),
            attempt,
          ],
        ))[0]?.id,
        "delivery_id",
      );
    });
  }

  private async finish(
    row: DatabaseRow,
    deliveryId: number,
    providerMessageId: string,
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const announcementId = positiveInteger(row.id, "announcement_id");
      await connection.query(
        `UPDATE community_announcement_deliveries
            SET status='delivered',provider_message_id=$1,completed_at=CURRENT_TIMESTAMP
          WHERE id=$2`,
        [providerMessageId, deliveryId],
      );
      await connection.query(
        `UPDATE community_announcements
            SET status='delivered',delivered_at=CURRENT_TIMESTAMP,last_error=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=$1`,
        [announcementId],
      );
      await audit(connection, "announcement.delivered", announcementId, {
        provider_message_id: providerMessageId,
      });
    });
  }

  private async fail(
    row: DatabaseRow,
    deliveryId: number,
    error: unknown,
  ): Promise<void> {
    const message = (error instanceof Error ? error.message : String(error))
      .slice(0, 500);
    await this.connection.transaction(async (connection) => {
      const announcementId = positiveInteger(row.id, "announcement_id");
      await connection.query(
        `UPDATE community_announcement_deliveries
            SET status='failed',error_message=$1,completed_at=CURRENT_TIMESTAMP
          WHERE id=$2`,
        [message, deliveryId],
      );
      await connection.query(
        `UPDATE community_announcements SET status='failed',last_error=$1,
                updated_at=CURRENT_TIMESTAMP WHERE id=$2`,
        [message, announcementId],
      );
      await audit(connection, "announcement.delivery_failed", announcementId, {
        error: message,
      });
    });
  }
}

async function audit(
  connection: DatabaseConnection,
  action: string,
  announcementId: number,
  payload: Readonly<Record<string, unknown>>,
): Promise<void> {
  await connection.query(
    `INSERT INTO audit_log(
       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
     ) VALUES ('system',NULL,$1,'community_announcement',$2,$3)`,
    [action, announcementId, JSON.stringify(payload)],
  );
}

function requireAnnouncementCapability(row: DatabaseRow): void {
  const capabilities = jsonArray(row.capabilities_json);
  if (!capabilities.includes("announcements")) {
    throw new Deno.errors.PermissionDenied(
      `installation ${row.installation_id} does not support announcements`,
    );
  }
}

function jsonArray(value: unknown): string[] {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function jsonObject(value: unknown): Readonly<Record<string, unknown>> {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}
