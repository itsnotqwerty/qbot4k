import type { DatabaseConnection } from "../../data/database.ts";
import type { TwitchTokenManager } from "./twitch_auth.ts";
import type { LiveOpsControlGateway } from "../../web/web_live_ops.ts";

const CHAT_FIELDS = new Set([
  "emote_mode",
  "follower_mode",
  "follower_mode_duration",
  "non_moderator_chat_delay",
  "non_moderator_chat_delay_duration",
  "slow_mode",
  "slow_mode_wait_time",
  "subscriber_mode",
  "unique_chat_mode",
]);

export class PostgresTwitchControlGateway implements LiveOpsControlGateway {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly tokens: TwitchTokenManager,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  shield(
    communityId: number,
    operatorId: number,
    broadcaster: string,
    active: boolean,
  ): Promise<unknown> {
    return this.execute(
      communityId,
      operatorId,
      broadcaster,
      "shield_mode",
      "moderation/shield_mode",
      "PUT",
      {
        is_active: active,
      },
    );
  }

  chat(
    communityId: number,
    operatorId: number,
    broadcaster: string,
    settings: Readonly<Record<string, unknown>>,
  ): Promise<unknown> {
    const payload = Object.fromEntries(
      Object.entries(settings).filter(([key]) => CHAT_FIELDS.has(key)),
    );
    if (!Object.keys(payload).length) {
      throw new TypeError("no supported Twitch chat settings were supplied");
    }
    for (
      const key of [
        "follower_mode_duration",
        "slow_mode_wait_time",
        "non_moderator_chat_delay_duration",
      ]
    ) {
      if (!(key in payload)) continue;
      const maximum = key === "follower_mode_duration" ? 129_600 : 120;
      payload[key] = Math.max(0, Math.min(Number(payload[key]), maximum));
    }
    return this.execute(
      communityId,
      operatorId,
      broadcaster,
      "chat_settings",
      "chat/settings",
      "PATCH",
      payload,
    );
  }

  private async execute(
    communityId: number,
    operatorId: number,
    broadcaster: string,
    controlType: string,
    endpoint: string,
    method: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<unknown> {
    const validation = await this.tokens.validateToken();
    if (!validation.userId) {
      throw new TypeError("Twitch token validation omitted moderator user_id");
    }
    const claimed = await this.connection.transaction(async (connection) => {
      const installation = (await connection.query(
        `SELECT id,external_community_id,capabilities_json
           FROM community_installations
          WHERE community_id=$1 AND platform='twitch' AND status='active'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=community_installations.id
                AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
            AND (external_community_id=$2 OR LOWER(display_name)=LOWER($2))
          FOR UPDATE`,
        [communityId, broadcaster.trim().replace(/^#/u, "")],
      ))[0];
      if (!installation) {
        throw new TypeError("active Twitch installation not found");
      }
      if (
        !stringArray(installation.capabilities_json).includes("live_controls")
      ) {
        throw new TypeError("Twitch live controls capability is disabled");
      }
      const broadcasterId = String(installation.external_community_id);
      const action = (await connection.query(
        `INSERT INTO twitch_control_actions(
           community_id,operator_id,broadcaster_id,control_type,requested_json
         ) VALUES ($1,$2,$3,$4,$5) RETURNING id`,
        [
          communityId,
          operatorId,
          broadcasterId,
          controlType,
          JSON.stringify(payload),
        ],
      ))[0];
      if (!action) throw new TypeError("Twitch control action was not created");
      return { actionId: Number(action.id), broadcasterId };
    });
    try {
      const url = new URL(`https://api.twitch.tv/helix/${endpoint}`);
      url.searchParams.set("broadcaster_id", claimed.broadcasterId);
      url.searchParams.set("moderator_id", validation.userId);
      const response = await this.request(
        url,
        method,
        validation.accessToken,
        validation.clientId,
        payload,
      );
      await this.connection.transaction(async (connection) => {
        await connection.query(
          `UPDATE twitch_control_actions SET status='confirmed',provider_status=$2,
             provider_response_json=$3,confirmed_at=CURRENT_TIMESTAMP,error_message=NULL
           WHERE id=$1`,
          [claimed.actionId, response.status, JSON.stringify(response.body)],
        );
        await connection.query(
          `INSERT INTO audit_log(
             actor_type,actor_id,action_type,entity_type,entity_id,payload_json
           ) VALUES ('operator',$1,'twitch.control_confirmed',
                     'twitch_control_action',$2,$3)`,
          [
            operatorId,
            claimed.actionId,
            JSON.stringify({
              control_type: controlType,
              provider_status: response.status,
            }),
          ],
        );
      });
      return Object.freeze({
        action_id: claimed.actionId,
        status: "confirmed",
        provider_status: response.status,
        response: response.body,
      });
    } catch (error) {
      await this.connection.transaction(async (connection) => {
        await connection.query(
          "UPDATE twitch_control_actions SET status='failed',error_message=$2 WHERE id=$1",
          [claimed.actionId, errorMessage(error).slice(0, 1000)],
        );
      });
      throw error;
    }
  }

  private async request(
    url: URL,
    method: string,
    accessToken: string,
    clientId: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<{ status: number; body: unknown }> {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const response = await this.fetcher(url, {
        method,
        headers: {
          Authorization: `Bearer ${accessToken}`,
          "Client-Id": clientId,
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const text = await response.text();
      if (response.ok) {
        return { status: response.status, body: parseJson(text) };
      }
      if ((response.status === 429 || response.status >= 500) && attempt < 2) {
        continue;
      }
      throw new TypeError(
        `Twitch control failed: HTTP ${response.status}${
          text ? ` ${text.slice(0, 500)}` : ""
        }`,
      );
    }
    throw new TypeError("Twitch control failed");
  }
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
