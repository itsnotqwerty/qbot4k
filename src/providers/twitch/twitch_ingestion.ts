import type { DatabaseConnection } from "../../data/database.ts";
import type { TwitchInstallationHealthSink } from "./twitch_irc.ts";
import {
  type CollectedObservation,
  NormalizedMessage,
  observationFromMessage,
} from "../../core/models.ts";
import type { ObservationCollector } from "../../domain/observations.ts";
import { normalizeTwitchMessage } from "./twitch_message.ts";

export interface TwitchIngestionService {
  ingest(
    payload: Readonly<Record<string, unknown>>,
  ): Promise<CollectedObservation | null>;
  channels(): Promise<readonly string[]>;
}

export class PostgresTwitchIngestionService implements TwitchIngestionService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly collector: ObservationCollector,
  ) {}

  async channels(): Promise<readonly string[]> {
    const rows = await this.connection.query(
      `SELECT external_community_id FROM community_installations
        WHERE platform='twitch' AND status='active'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=community_installations.id
              AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
        ORDER BY LOWER(external_community_id)`,
    );
    return Object.freeze(rows.map((row) => String(row.external_community_id)));
  }

  async ingest(
    payload: Readonly<Record<string, unknown>>,
  ): Promise<CollectedObservation | null> {
    const message = normalizeTwitchMessage(payload);
    const installation = (await this.connection.query(
      `SELECT installation.id AS installation_id,installation.community_id,
              policy.allow_bot_messages
         FROM community_installations AS installation
         JOIN community_policy_settings AS policy
           ON policy.community_id=installation.community_id
        WHERE installation.platform='twitch' AND installation.status='active'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=installation.id
              AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
          AND (LOWER(installation.display_name)=LOWER($1)
            OR LOWER(installation.metadata_json::jsonb->>'broadcaster_login')=LOWER($1)
            OR installation.external_community_id=$1)`,
      [message.channelId],
    ))[0];
    if (!installation) return null;
    if (
      message.metadata.author_is_bot === true &&
      !truthy(installation.allow_bot_messages)
    ) {
      return null;
    }
    return await this.collector.collect(observationFromMessage(
      new NormalizedMessage({
        platform: message.platform,
        platformMessageId: message.platformMessageId,
        platformUserId: message.platformUserId,
        username: message.username,
        channelId: message.channelId,
        guildOrChannelContext: message.guildOrChannelContext,
        contentRaw: message.contentRaw,
        sentAt: message.sentAt,
        roleNames: message.roleNames,
        isModerator: message.isModerator,
        metadata: {
          ...message.metadata,
          community_id: integer(installation.community_id, "community_id"),
          installation_id: integer(
            installation.installation_id,
            "installation_id",
          ),
        },
      }),
    ));
  }
}

export class PostgresTwitchInstallationHealth
  implements TwitchInstallationHealthSink {
  constructor(private readonly connection: DatabaseConnection) {}

  async ready(channels: readonly string[]): Promise<void> {
    const normalized = channels.map((channel) =>
      channel.trim().replace(/^#/u, "").toLocaleLowerCase()
    );
    await this.connection.transaction(async (connection) => {
      const verified = await connection.query(
        `UPDATE community_installations SET
           status='active',health_status='ready',last_health_check_at=CURRENT_TIMESTAMP,
           last_verified_at=CURRENT_TIMESTAMP,reconnect_attempts=0,last_error=NULL,
           updated_at=CURRENT_TIMESTAMP
         WHERE platform='twitch' AND status IN ('pending','active')
           AND LOWER(display_name) = ANY($1::text[])
         RETURNING id,community_id,external_community_id`,
        [textArray(normalized)],
      );
      await connection.query(
        `UPDATE community_installations SET
           health_status='degraded',last_health_check_at=CURRENT_TIMESTAMP,
           last_error='Channel missing from Twitch IRC joins',updated_at=CURRENT_TIMESTAMP
         WHERE platform='twitch' AND status='active'
           AND NOT (LOWER(display_name) = ANY($1::text[]))`,
        [textArray(normalized)],
      );
      for (const installation of verified) {
        await connection.query(
          `INSERT INTO audit_log(actor_type,action_type,entity_type,entity_id,payload_json)
           VALUES ('system','integration.twitch_verified','community_installation',$1,$2)`,
          [
            Number(installation.id),
            JSON.stringify({
              community_id: Number(installation.community_id),
              broadcaster_id: String(installation.external_community_id),
            }),
          ],
        );
      }
    });
  }

  async failed(error: string): Promise<void> {
    await this.connection.query(
      `UPDATE community_installations SET
         health_status='degraded',last_health_check_at=CURRENT_TIMESTAMP,
         reconnect_attempts=reconnect_attempts+1,last_error=$1,updated_at=CURRENT_TIMESTAMP
       WHERE platform='twitch' AND status IN ('pending','active','degraded')`,
      [error.slice(0, 2000)],
    );
  }
}

function textArray(values: readonly string[]): string {
  const escaped = values.map((value) =>
    `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`
  );
  return `{${escaped.join(",")}}`;
}

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new TypeError(`${name} must be a positive integer`);
  }
  return parsed;
}

function truthy(value: unknown): boolean {
  return value === true || value === 1 || value === "1";
}
