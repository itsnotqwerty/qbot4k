import type { DatabaseConnection } from "../../data/database.ts";
import { PermanentJobError, type ProcessingJob } from "../../jobs/jobs.ts";

export const DISCORD_MESSAGE_JOB_TYPE = "discord.message.send";
export const DISCORD_MODERATION_JOB_TYPE = "discord.moderation.execute";

export interface DiscordApi {
  sendMessage(
    channelId: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<unknown>;
  deleteMessage(channelId: string, messageId: string): Promise<unknown>;
  timeoutMember(
    guildId: string,
    userId: string,
    reason: string,
    durationSeconds: number,
  ): Promise<unknown>;
  banMember(guildId: string, userId: string, reason: string): Promise<unknown>;
}

export class DiscordApiError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export class FetchDiscordApi implements DiscordApi {
  constructor(
    private readonly botToken: string,
    private readonly fetcher: typeof fetch = fetch,
    private readonly delay: (milliseconds: number) => Promise<void> = (
      milliseconds,
    ) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  ) {}

  sendMessage(
    channelId: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<unknown> {
    return this.request(
      "POST",
      `/channels/${segment(channelId)}/messages`,
      payload,
    );
  }

  deleteMessage(channelId: string, messageId: string): Promise<unknown> {
    return this.request(
      "DELETE",
      `/channels/${segment(channelId)}/messages/${segment(messageId)}`,
    );
  }

  timeoutMember(
    guildId: string,
    userId: string,
    reason: string,
    durationSeconds: number,
  ): Promise<unknown> {
    const boundedSeconds = Math.max(1, Math.min(durationSeconds, 2_419_200));
    return this.request(
      "PATCH",
      `/guilds/${segment(guildId)}/members/${segment(userId)}`,
      {
        communication_disabled_until: new Date(
          Date.now() + boundedSeconds * 1000,
        ).toISOString(),
      },
      reason,
    );
  }

  banMember(guildId: string, userId: string, reason: string): Promise<unknown> {
    return this.request(
      "PUT",
      `/guilds/${segment(guildId)}/bans/${segment(userId)}`,
      { delete_message_seconds: 86_400 },
      reason,
    );
  }

  private async request(
    method: string,
    path: string,
    payload?: Readonly<Record<string, unknown>>,
    reason?: string,
  ): Promise<unknown> {
    const token = this.botToken.replace(/^Bot\s+/iu, "").trim();
    if (!token) {
      throw new PermanentJobError("Discord bot token is not configured");
    }
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response: Response;
      try {
        response = await this.fetcher(`https://discord.com/api/v10${path}`, {
          method,
          headers: {
            Authorization: `Bot ${token}`,
            Accept: "application/json",
            "User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
            ...(payload ? { "Content-Type": "application/json" } : {}),
            ...(reason
              ? { "X-Audit-Log-Reason": encodeURIComponent(reason) }
              : {}),
          },
          ...(payload ? { body: JSON.stringify(payload) } : {}),
        });
      } catch (error) {
        if (attempt < 2) {
          await this.delay(2 ** attempt * 1000);
          continue;
        }
        throw new DiscordApiError(errorMessage(error), true);
      }
      const body = await response.text();
      if (response.ok) return body ? parseJson(body) : {};
      const retryable = response.status === 429 || response.status >= 500;
      if (retryable && attempt < 2) {
        await this.delay(retryMilliseconds(response, body, attempt));
        continue;
      }
      throw new DiscordApiError(
        `Discord API ${method} ${path} failed: HTTP ${response.status}${
          body ? ` - ${body.slice(0, 500)}` : ""
        }`,
        retryable,
      );
    }
    throw new DiscordApiError(`Discord API ${method} ${path} failed`, true);
  }
}

export class PostgresDiscordActionRepository {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly api: DiscordApi,
  ) {}

  async sendMessage(job: ProcessingJob): Promise<void> {
    requireJob(job, DISCORD_MESSAGE_JOB_TYPE);
    const channelId = text(job.payload.channel_id);
    const reply = record(job.payload.rendered_reply);
    if (!channelId) {
      throw new PermanentJobError("Discord message action has no channel_id");
    }
    if (!reply) {
      throw new PermanentJobError("Discord rendered_reply must be an object");
    }
    if (!job.observationId) {
      throw new PermanentJobError(
        "Discord message action has no originating observation",
      );
    }
    const installation = (await this.connection.query(
      `SELECT installation.capabilities_json
         FROM observations AS observation
         JOIN community_installations AS installation
           ON installation.id=observation.installation_id
          AND installation.community_id=observation.community_id
          AND installation.platform='discord' AND installation.status='active'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=installation.id AND lease.owner_runtime='deno'
              AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
        WHERE observation.id=$1 AND observation.community_id=$2`,
      [job.observationId, job.communityId],
    ))[0];
    if (!installation) {
      throw new PermanentJobError(
        "Discord message installation is not active for the job tenant",
      );
    }
    if (
      !stringArray(installation.capabilities_json).includes("announcements")
    ) {
      throw new PermanentJobError(
        "Discord message capability is disabled",
      );
    }
    await this.api.sendMessage(channelId, reply);
  }

  async moderate(job: ProcessingJob): Promise<void> {
    requireJob(job, DISCORD_MODERATION_JOB_TYPE);
    const messageId = positiveInteger(job.payload.message_id, "message_id");
    await this.connection.transaction(async (connection) => {
      const actions = await connection.query(
        `SELECT action.id,action.action_type,action.reason,
                action.duration_seconds,message.platform_message_id,
                message.channel_id,account.platform_user_id,
                installation.external_community_id,
                installation.capabilities_json
           FROM moderation_actions AS action
           JOIN messages AS message
             ON message.id=action.message_id
            AND message.community_id=action.community_id
           JOIN platform_accounts AS account
             ON account.id=action.target_platform_account_id
           JOIN community_installations AS installation
             ON installation.id=action.installation_id
            AND installation.community_id=action.community_id
            AND installation.platform='discord'
            AND installation.status='active'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=installation.id AND lease.owner_runtime='deno'
                AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
          WHERE action.community_id=$1 AND action.message_id=$2
            AND action.platform='discord' AND action.status='pending'
          ORDER BY action.id FOR UPDATE OF action`,
        [job.communityId, messageId],
      );
      for (const action of actions) {
        const actionId = positiveInteger(action.id, "action_id");
        if (
          !stringArray(action.capabilities_json).includes("moderation_actions")
        ) {
          await markFailed(
            connection,
            actionId,
            "moderation capability is disabled",
          );
          continue;
        }
        try {
          const response = await this.execute(action);
          await connection.query(
            `UPDATE moderation_actions SET
               status='completed',completed_at=CURRENT_TIMESTAMP,
               provider_status='accepted',provider_response_json=$2,
               error_message=NULL
             WHERE id=$1 AND status='pending'`,
            [actionId, JSON.stringify(response ?? {})],
          );
        } catch (error) {
          if (error instanceof DiscordApiError && error.retryable) throw error;
          await markFailed(connection, actionId, errorMessage(error));
        }
      }
    });
  }

  private async execute(
    action: Readonly<Record<string, unknown>>,
  ): Promise<unknown> {
    const actionType = text(action.action_type).toLocaleLowerCase();
    const channelId = text(action.channel_id);
    const platformMessageId = text(action.platform_message_id);
    const guildId = text(action.external_community_id);
    const userId = text(action.platform_user_id);
    const reason = text(action.reason) || actionType;
    if (actionType === "warn") return { outcome: "recorded" };
    if (!guildId || !userId) {
      throw new PermanentJobError("Discord moderation target is incomplete");
    }
    if (platformMessageId && channelId) {
      await this.api.deleteMessage(channelId, platformMessageId);
    }
    if (actionType === "timeout") {
      return await this.api.timeoutMember(
        guildId,
        userId,
        reason,
        positiveInteger(action.duration_seconds, "duration_seconds"),
      );
    }
    if (actionType === "ban") {
      return await this.api.banMember(guildId, userId, reason);
    }
    throw new PermanentJobError(`unsupported Discord action: ${actionType}`);
  }
}

function requireJob(job: ProcessingJob, jobType: string): void {
  if (job.stage !== "action" || job.jobType !== jobType) {
    throw new PermanentJobError(`unsupported Discord job: ${job.jobType}`);
  }
}

async function markFailed(
  connection: DatabaseConnection,
  actionId: number,
  error: string,
): Promise<void> {
  await connection.query(
    `UPDATE moderation_actions SET
       status='failed',completed_at=CURRENT_TIMESTAMP,error_message=$2,
       provider_status='failed'
     WHERE id=$1 AND status='pending'`,
    [actionId, error.slice(0, 2000)],
  );
}

function retryMilliseconds(
  response: Response,
  body: string,
  attempt: number,
): number {
  const header = Number(response.headers.get("Retry-After"));
  const parsed = record(parseJson(body));
  const retryAfter = Number(parsed?.retry_after ?? header);
  const seconds = Number.isFinite(retryAfter) && retryAfter > 0
    ? retryAfter
    : 2 ** attempt;
  return Math.min(5_000, Math.max(100, seconds * 1000));
}

function stringArray(value: unknown): readonly string[] {
  const parsed = typeof value === "string" ? parseJson(value) : value;
  return Array.isArray(parsed) ? parsed.map(String) : [];
}

function parseJson(value: string): unknown {
  try {
    return value ? JSON.parse(value) : {};
  } catch {
    return {};
  }
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new PermanentJobError(`${name} must be a positive integer`);
  }
  return parsed;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function segment(value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    throw new PermanentJobError("Discord resource ID is required");
  }
  return encodeURIComponent(normalized);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
