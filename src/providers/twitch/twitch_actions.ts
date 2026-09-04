import type { DatabaseConnection } from "../../data/database.ts";
import { PermanentJobError, type ProcessingJob } from "../../jobs/jobs.ts";
import type { TwitchTokenManager } from "./twitch_auth.ts";

export const TWITCH_MODERATION_JOB_TYPE = "twitch.moderation.execute";

export class TwitchApiError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export interface TwitchModerationApi {
  moderate(
    broadcasterId: string,
    targetUserId: string,
    actionType: string,
    reason: string,
    durationSeconds: number,
  ): Promise<unknown>;
}

export class FetchTwitchModerationApi implements TwitchModerationApi {
  constructor(
    private readonly tokens: TwitchTokenManager,
    private readonly fetcher: typeof fetch = fetch,
    private readonly delay: (milliseconds: number) => Promise<void> = (
      milliseconds,
    ) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  ) {}

  async moderate(
    broadcasterId: string,
    targetUserId: string,
    actionType: string,
    reason: string,
    durationSeconds: number,
  ): Promise<unknown> {
    const validation = await this.tokens.validateToken();
    if (!validation.userId) {
      throw new PermanentJobError(
        "Twitch token validation omitted moderator user_id",
      );
    }
    const normalized = actionType.trim().toLocaleLowerCase();
    const endpoint = normalized === "warn" ? "warnings" : "bans";
    if (!new Set(["timeout", "ban", "warn"]).has(normalized)) {
      throw new PermanentJobError(`unsupported Twitch action: ${normalized}`);
    }
    const data: Record<string, unknown> = {
      user_id: targetUserId,
      reason: reason.slice(0, 500),
    };
    if (normalized === "timeout") {
      data.duration = Math.max(1, Math.min(durationSeconds, 1_209_600));
    }
    const url = new URL(`https://api.twitch.tv/helix/moderation/${endpoint}`);
    url.searchParams.set("broadcaster_id", broadcasterId);
    url.searchParams.set("moderator_id", validation.userId);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response: Response;
      try {
        response = await this.fetcher(url, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${validation.accessToken}`,
            "Client-Id": validation.clientId,
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ data }),
        });
      } catch (error) {
        if (attempt < 2) {
          await this.delay(2 ** attempt * 1000);
          continue;
        }
        throw new TwitchApiError(errorMessage(error), true);
      }
      const body = await response.text();
      if (response.ok) return parseJson(body);
      const retryable = response.status === 429 || response.status >= 500;
      if (retryable && attempt < 2) {
        await this.delay(retryMilliseconds(response, attempt));
        continue;
      }
      throw new TwitchApiError(
        `Twitch moderation failed: HTTP ${response.status}${
          body ? ` - ${body.slice(0, 500)}` : ""
        }`,
        retryable,
      );
    }
    throw new TwitchApiError("Twitch moderation failed", true);
  }
}

export class PostgresTwitchActionRepository {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly api: TwitchModerationApi,
  ) {}

  async moderate(job: ProcessingJob): Promise<void> {
    if (job.stage !== "action" || job.jobType !== TWITCH_MODERATION_JOB_TYPE) {
      throw new PermanentJobError(`unsupported Twitch job: ${job.jobType}`);
    }
    const messageId = positiveInteger(job.payload.message_id, "message_id");
    await this.connection.transaction(async (connection) => {
      const actions = await connection.query(
        `SELECT action.id,action.action_type,action.reason,
                action.duration_seconds,account.platform_user_id,
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
            AND installation.platform='twitch'
            AND installation.status='active'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=installation.id AND lease.owner_runtime='deno'
                AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
          WHERE action.community_id=$1 AND action.message_id=$2
            AND action.platform='twitch' AND action.status='pending'
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
          const broadcasterId = text(action.external_community_id);
          const userId = text(action.platform_user_id);
          if (!broadcasterId || !userId) {
            throw new PermanentJobError(
              "Twitch moderation target is incomplete",
            );
          }
          const actionType = text(action.action_type).toLocaleLowerCase();
          const response = await this.api.moderate(
            broadcasterId,
            userId,
            actionType,
            text(action.reason) || actionType,
            Number(action.duration_seconds) || 0,
          );
          await connection.query(
            `UPDATE moderation_actions SET
               status='completed',completed_at=CURRENT_TIMESTAMP,
               provider_status='accepted',provider_response_json=$2,
               error_message=NULL
             WHERE id=$1 AND status='pending'`,
            [actionId, JSON.stringify(response ?? {})],
          );
        } catch (error) {
          if (error instanceof TwitchApiError && error.retryable) throw error;
          await markFailed(connection, actionId, errorMessage(error));
        }
      }
    });
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

function retryMilliseconds(response: Response, attempt: number): number {
  const seconds = Number(response.headers.get("Retry-After"));
  return Math.min(
    5_000,
    Math.max(
      100,
      (Number.isFinite(seconds) && seconds > 0 ? seconds : 2 ** attempt) * 1000,
    ),
  );
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
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new PermanentJobError(`${name} must be a positive integer`);
  }
  return parsed;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
