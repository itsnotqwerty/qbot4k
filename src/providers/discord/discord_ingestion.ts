import type { DatabaseConnection } from "../../data/database.ts";
import { observationFromDiscordEvent } from "./discord_events.ts";
import {
  buildDiscordMessagePayload,
  normalizeDiscordMessage,
} from "./discord_message.ts";
import {
  type CollectedObservation,
  NormalizedMessage,
  observationFromMessage,
} from "../../core/models.ts";
import type { ObservationCollector } from "../../domain/observations.ts";
import type { DiscordInstallationHealthSink } from "./discord_gateway.ts";

export interface DiscordIngestionService {
  ingest(
    eventName: string,
    data: unknown,
  ): Promise<CollectedObservation | null>;
}

interface DiscordTenant {
  readonly communityId: number;
  readonly installationId: number;
  readonly allowBotMessages: boolean;
}

export class PostgresDiscordIngestionService
  implements DiscordIngestionService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly collector: ObservationCollector,
    private readonly channelName?: (
      channelId: string,
    ) => Promise<{ name: string; type: number } | null>,
  ) {}

  private async ensureChannel(channelId: string, guildId: string | null) {
    if (!this.channelName) return;
    try {
      const known = (await this.connection.query(
        "SELECT 1 FROM discord_channels WHERE channel_id=$1",
        [channelId],
      ))[0];
      if (known) return;
      const channel = await this.channelName(channelId);
      if (!channel) return;
      await this.connection.query(
        `INSERT INTO discord_channels(channel_id, guild_id, channel_name, channel_type)
         VALUES ($1,$2,$3,$4)
         ON CONFLICT(channel_id) DO UPDATE SET
           channel_name=EXCLUDED.channel_name, updated_at=CURRENT_TIMESTAMP`,
        [channelId, guildId ?? "", channel.name, channel.type],
      );
    } catch {
      // Channel name resolution is best-effort and must not block ingestion.
    }
  }

  async ingest(
    eventName: string,
    data: unknown,
  ): Promise<CollectedObservation | null> {
    const payload = record(data);
    if (!payload) return null;
    const guildId = text(payload.guild_id);
    if (!guildId) return null;
    const tenant = await this.resolveTenant(guildId);
    if (!tenant) return null;

    if (eventName === "MESSAGE_CREATE") {
      const message = normalizeDiscordMessage(
        buildDiscordMessagePayload(payload),
      );
      if (message.metadata.author_is_bot === true && !tenant.allowBotMessages) {
        return null;
      }
      await this.ensureChannel(
        message.channelId,
        message.guildOrChannelContext,
      );
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
            community_id: tenant.communityId,
            installation_id: tenant.installationId,
          },
        }),
      ));
    }

    const observation = await observationFromDiscordEvent(eventName, payload, {
      communityId: tenant.communityId,
      installationId: tenant.installationId,
    });
    return observation ? await this.collector.collect(observation) : null;
  }

  private async resolveTenant(guildId: string): Promise<DiscordTenant | null> {
    const row = (await this.connection.query(
      `SELECT installation.id AS installation_id,installation.community_id,
              policy.allow_bot_messages
         FROM community_installations AS installation
         JOIN community_policy_settings AS policy
           ON policy.community_id=installation.community_id
        WHERE installation.platform='discord'
          AND installation.external_community_id=$1
          AND installation.status='active'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=installation.id
              AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)`,
      [guildId],
    ))[0];
    if (!row) return null;
    return Object.freeze({
      communityId: integer(row.community_id, "community_id"),
      installationId: integer(row.installation_id, "installation_id"),
      allowBotMessages: truthy(row.allow_bot_messages),
    });
  }
}

export class PostgresDiscordInstallationHealth
  implements DiscordInstallationHealthSink {
  constructor(private readonly connection: DatabaseConnection) {}

  async ready(guildIds: readonly string[]): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const verified = await connection.query(
        `UPDATE community_installations SET
           status='active',health_status='ready',
           last_health_check_at=CURRENT_TIMESTAMP,
           last_verified_at=CURRENT_TIMESTAMP,reconnect_attempts=0,
           last_error=NULL,updated_at=CURRENT_TIMESTAMP
         WHERE platform='discord' AND status IN ('pending','active')
           AND external_community_id = ANY($1::text[])
         RETURNING id,community_id,external_community_id`,
        [textArray(guildIds)],
      );
      await connection.query(
        `UPDATE community_installations SET
           health_status='degraded',last_health_check_at=CURRENT_TIMESTAMP,
           reconnect_attempts=0,last_error='Guild missing from Discord READY',
           updated_at=CURRENT_TIMESTAMP
         WHERE platform='discord' AND status='active'
           AND NOT (external_community_id = ANY($1::text[]))`,
        [textArray(guildIds)],
      );
      for (const installation of verified) {
        await connection.query(
          `INSERT INTO audit_log(
             actor_type,action_type,entity_type,entity_id,payload_json
           ) VALUES ('system','integration.discord_verified',
                     'community_installation',$1,$2)`,
          [
            integer(installation.id, "installation_id"),
            JSON.stringify({
              community_id: integer(installation.community_id, "community_id"),
              guild_id: text(installation.external_community_id),
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
         reconnect_attempts=reconnect_attempts+1,last_error=$1,
         updated_at=CURRENT_TIMESTAMP
       WHERE platform='discord' AND status='active'`,
      [error.slice(0, 2000)],
    );
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function textArray(values: readonly string[]): string {
  return `{${
    values.map((value) =>
      `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`
    ).join(",")
  }}`;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
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
