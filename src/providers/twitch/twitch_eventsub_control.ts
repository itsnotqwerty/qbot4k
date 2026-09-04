import type { DatabaseConnection } from "../../data/database.ts";
import type { TwitchTokenManager } from "./twitch_auth.ts";

export interface TwitchEventSubDefinition {
  readonly type: string;
  readonly version?: string;
  readonly condition: Readonly<Record<string, unknown>>;
}

export interface TwitchEventSubReport {
  readonly existing: number;
  readonly created: number;
  readonly desired: number;
}

export class TwitchEventSubApiError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export class PostgresTwitchEventSubReconciler {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly tokens: TwitchTokenManager,
    private readonly callbackUrl: string,
    private readonly secret: string,
    private readonly fetcher: typeof fetch = fetch,
    private readonly delay: (milliseconds: number) => Promise<void> = (
      milliseconds,
    ) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  ) {
    if (!callbackUrl.startsWith("https://")) {
      throw new TypeError("EventSub callback URL must use HTTPS");
    }
    if (secret.length < 16) {
      throw new TypeError("EventSub secret must be at least 16 characters");
    }
  }

  async reconcile(
    communityId: number,
    installationId: number,
    desired: readonly TwitchEventSubDefinition[],
  ): Promise<TwitchEventSubReport> {
    const validation = await this.tokens.validateToken();
    try {
      const installation = (await this.connection.query(
        `SELECT id FROM community_installations
          WHERE id=$1 AND community_id=$2 AND platform='twitch'
            AND status IN ('pending','active')
            AND capabilities_json::jsonb ? 'events'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=community_installations.id
                AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)`,
        [installationId, communityId],
      ))[0];
      if (!installation) {
        throw new TypeError(
          "EventSub installation is not active or capable for the tenant",
        );
      }
      const existing = await this.list(
        validation.accessToken,
        validation.clientId,
      );
      const existingKeys = new Set(existing.map(subscriptionKey));
      for (const subscription of existing) {
        await this.record(communityId, subscription);
      }
      let created = 0;
      for (const definition of desired) {
        const normalized = normalizeDefinition(definition);
        if (existingKeys.has(subscriptionKey(normalized))) continue;
        const subscription = await this.create(
          validation.accessToken,
          validation.clientId,
          normalized,
        );
        await this.record(communityId, subscription);
        existingKeys.add(subscriptionKey(normalized));
        created += 1;
      }
      await this.connection.query(
        `UPDATE community_installations SET health_status='ready',
           last_health_check_at=CURRENT_TIMESTAMP,last_verified_at=CURRENT_TIMESTAMP,
           reconnect_attempts=0,last_error=NULL,updated_at=CURRENT_TIMESTAMP
         WHERE id=$1 AND community_id=$2 AND platform='twitch'
           AND status IN ('pending','active')`,
        [installationId, communityId],
      );
      return Object.freeze({
        existing: existing.length,
        created,
        desired: desired.length,
      });
    } catch (error) {
      await this.connection.query(
        `UPDATE community_installations SET health_status='degraded',
           last_health_check_at=CURRENT_TIMESTAMP,
           reconnect_attempts=reconnect_attempts+1,last_error=$3,
           updated_at=CURRENT_TIMESTAMP
         WHERE id=$1 AND community_id=$2 AND platform='twitch'`,
        [installationId, communityId, errorMessage(error).slice(0, 2000)],
      );
      throw error;
    }
  }

  private async list(
    accessToken: string,
    clientId: string,
  ): Promise<Readonly<Record<string, unknown>>[]> {
    const subscriptions: Readonly<Record<string, unknown>>[] = [];
    let cursor = "";
    for (let page = 0; page < 20; page += 1) {
      const url = new URL(
        "https://api.twitch.tv/helix/eventsub/subscriptions",
      );
      if (cursor) url.searchParams.set("after", cursor);
      const payload = await this.request(url, "GET", accessToken, clientId);
      subscriptions.push(...recordArray(payload.data));
      cursor = text(record(payload.pagination)?.cursor);
      if (!cursor) break;
    }
    return subscriptions;
  }

  private async create(
    accessToken: string,
    clientId: string,
    definition: TwitchEventSubDefinition,
  ): Promise<Readonly<Record<string, unknown>>> {
    const payload = await this.request(
      new URL("https://api.twitch.tv/helix/eventsub/subscriptions"),
      "POST",
      accessToken,
      clientId,
      {
        ...definition,
        transport: {
          method: "webhook",
          callback: this.callbackUrl,
          secret: this.secret,
        },
      },
    );
    const subscription = recordArray(payload.data)[0];
    if (!subscription) {
      throw new TypeError(
        "Twitch EventSub create response omitted subscription data",
      );
    }
    return subscription;
  }

  private async request(
    url: URL,
    method: string,
    accessToken: string,
    clientId: string,
    payload?: Readonly<Record<string, unknown>>,
  ): Promise<Record<string, unknown>> {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response: Response;
      try {
        response = await this.fetcher(url, {
          method,
          headers: {
            Authorization: `Bearer ${accessToken}`,
            "Client-Id": clientId,
            Accept: "application/json",
            ...(payload ? { "Content-Type": "application/json" } : {}),
          },
          ...(payload ? { body: JSON.stringify(payload) } : {}),
        });
      } catch (error) {
        if (attempt < 2) {
          await this.delay(2 ** attempt * 1000);
          continue;
        }
        throw new TwitchEventSubApiError(errorMessage(error), true);
      }
      const body = await response.text();
      if (response.ok) return record(parseJson(body)) ?? {};
      const retryable = response.status === 429 || response.status >= 500;
      if (retryable && attempt < 2) {
        await this.delay(retryMilliseconds(response, attempt));
        continue;
      }
      throw new TwitchEventSubApiError(
        `Twitch EventSub failed: HTTP ${response.status}${
          body ? ` - ${body.slice(0, 500)}` : ""
        }`,
        retryable,
      );
    }
    throw new TwitchEventSubApiError("Twitch EventSub failed", true);
  }

  private async record(
    communityId: number,
    subscription: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const transport = record(subscription.transport) ?? {};
    await this.connection.query(
      `INSERT INTO twitch_eventsub_subscriptions(
         subscription_id,community_id,subscription_type,subscription_version,
         condition_json,transport_method,status,callback_url,cost
       ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       ON CONFLICT(subscription_id) DO UPDATE SET
         status=EXCLUDED.status,condition_json=EXCLUDED.condition_json,
         callback_url=EXCLUDED.callback_url,cost=EXCLUDED.cost,
         updated_at=CURRENT_TIMESTAMP`,
      [
        text(subscription.id),
        communityId,
        text(subscription.type) || "unknown",
        text(subscription.version) || "1",
        JSON.stringify(record(subscription.condition) ?? {}),
        text(transport.method) || "webhook",
        text(subscription.status) || "unknown",
        text(transport.callback) || null,
        Number(subscription.cost) || 0,
      ],
    );
  }
}

function normalizeDefinition(
  definition: TwitchEventSubDefinition,
): TwitchEventSubDefinition {
  const type = definition.type.trim();
  const version = definition.version?.trim() || "1";
  if (!type || !Object.keys(definition.condition).length) {
    throw new TypeError(
      "desired EventSub subscriptions require type and condition",
    );
  }
  return Object.freeze({ type, version, condition: definition.condition });
}

function subscriptionKey(
  subscription:
    | TwitchEventSubDefinition
    | Readonly<Record<string, unknown>>,
): string {
  return JSON.stringify([
    text(subscription.type),
    text(subscription.version) || "1",
    sortedRecord(record(subscription.condition) ?? {}),
  ]);
}

function sortedRecord(
  value: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  return Object.fromEntries(
    Object.entries(value).sort(([left], [right]) => left.localeCompare(right)),
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

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function recordArray(value: unknown): Readonly<Record<string, unknown>>[] {
  return Array.isArray(value)
    ? value.map(record).filter((item): item is Record<string, unknown> =>
      item !== null
    )
    : [];
}

function parseJson(value: string): unknown {
  try {
    return value ? JSON.parse(value) : {};
  } catch {
    return {};
  }
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
