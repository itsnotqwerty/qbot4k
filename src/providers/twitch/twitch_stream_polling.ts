import type { DatabaseConnection } from "../../data/database.ts";
import { coerceTimestamp, type Observation } from "../../core/models.ts";
import type { ObservationCollector } from "../../domain/observations.ts";
import type { TwitchTokenManager } from "./twitch_auth.ts";

export interface TwitchStreamPollReport {
  readonly checked: number;
  readonly transitions: number;
}

interface TwitchStream {
  readonly id: string;
  readonly title: string;
  readonly gameName: string;
  readonly startedAt: string;
  readonly url: string;
}

export class TwitchStreamApiError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
  }
}

export class PostgresTwitchStreamPoller {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly collector: ObservationCollector,
    private readonly tokens: TwitchTokenManager,
    private readonly fetcher: typeof fetch = fetch,
    private readonly delay: (milliseconds: number) => Promise<void> = (
      milliseconds,
    ) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  ) {}

  async poll(now = new Date()): Promise<TwitchStreamPollReport> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    const installations = await this.connection.query(
      `SELECT id,community_id,external_community_id,display_name,capabilities_json
         FROM community_installations
        WHERE platform='twitch' AND status='active'
          AND capabilities_json::jsonb ? 'events'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=community_installations.id
              AND lease.owner_runtime='deno' AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
        ORDER BY community_id,id`,
    );
    const validation = await this.tokens.validateToken();
    let transitions = 0;
    for (const installation of installations) {
      const login = text(installation.display_name) ||
        text(installation.external_community_id);
      if (!login) continue;
      const stream = await this.fetchStream(
        login,
        validation.accessToken,
        validation.clientId,
      );
      const observation = await this.transition(
        Number(installation.community_id),
        Number(installation.id),
        login,
        stream,
        now,
      );
      if (!observation) continue;
      await this.collector.collect(observation);
      transitions += 1;
    }
    return Object.freeze({ checked: installations.length, transitions });
  }

  private async fetchStream(
    login: string,
    accessToken: string,
    clientId: string,
  ): Promise<TwitchStream | null> {
    const url = new URL("https://api.twitch.tv/helix/streams");
    url.searchParams.set("user_login", login.trim().toLocaleLowerCase());
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response: Response;
      try {
        response = await this.fetcher(url, {
          headers: {
            Authorization: `Bearer ${accessToken}`,
            "Client-Id": clientId,
            Accept: "application/json",
          },
        });
      } catch (error) {
        if (attempt < 2) {
          await this.delay(2 ** attempt * 1000);
          continue;
        }
        throw new TwitchStreamApiError(errorMessage(error), true);
      }
      const body = await response.text();
      if (response.ok) {
        const item = recordArray(record(parseJson(body))?.data)[0];
        if (!item || !text(item.id)) return null;
        return Object.freeze({
          id: text(item.id),
          title: text(item.title),
          gameName: text(item.game_name),
          startedAt: text(item.started_at),
          url: `https://www.twitch.tv/${login.trim().toLocaleLowerCase()}`,
        });
      }
      const retryable = response.status === 429 || response.status >= 500;
      if (retryable && attempt < 2) {
        await this.delay(retryMilliseconds(response, attempt));
        continue;
      }
      throw new TwitchStreamApiError(
        `Twitch streams failed: HTTP ${response.status}${
          body ? ` - ${body.slice(0, 500)}` : ""
        }`,
        retryable,
      );
    }
    throw new TwitchStreamApiError("Twitch streams failed", true);
  }

  private async transition(
    communityId: number,
    installationId: number,
    login: string,
    stream: TwitchStream | null,
    now: Date,
  ): Promise<Observation | null> {
    const latest = (await this.connection.query(
      `SELECT event_type,attributes_json
         FROM observations
        WHERE platform='twitch' AND context_id=$1 AND community_id=$2
          AND event_type LIKE 'stream.%'
        ORDER BY occurred_at DESC,id DESC LIMIT 1`,
      [login, communityId],
    ))[0];
    const previous = record(parseJson(text(latest?.attributes_json))) ?? {};
    let eventType: string;
    let streamId: string;
    let attributes: Readonly<Record<string, unknown>>;
    if (!stream) {
      if (!latest || text(latest.event_type) === "stream.ended") return null;
      eventType = "stream.ended";
      streamId = text(previous.stream_id) || "unknown";
      attributes = { stream_id: streamId, channel_name: login };
    } else {
      streamId = stream.id;
      if (
        !latest || text(latest.event_type) === "stream.ended" ||
        text(previous.stream_id) !== stream.id
      ) {
        eventType = "stream.started";
      } else if (
        text(previous.title) !== stream.title ||
        text(previous.game_name) !== stream.gameName
      ) {
        eventType = "stream.updated";
      } else {
        return null;
      }
      attributes = {
        stream_id: stream.id,
        channel_name: login,
        title: stream.title,
        game_name: stream.gameName,
        url: stream.url,
      };
    }
    const digest = await sha256(JSON.stringify(sortedRecord(attributes)));
    return Object.freeze({
      platform: "twitch",
      eventType,
      occurredAt: coerceTimestamp(stream?.startedAt || now),
      communityId,
      installationId,
      externalEventId: `poll:${login}:${streamId}:${eventType}:${
        digest.slice(0, 12)
      }`,
      actorPlatformUserId: login,
      actorUsername: login,
      targetPlatformUserId: null,
      containerId: login,
      contextId: login,
      text: stream?.title || null,
      attributes: Object.freeze(attributes),
      rawPayload: Object.freeze({}),
      schemaVersion: 1,
    });
  }
}

async function sha256(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(
    new Uint8Array(bytes),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
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
