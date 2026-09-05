import {
  calculateCountSample,
  calculateFreshnessSample,
  calculateLatencySample,
  calculatePercentageSample,
  type TenantSloSample,
} from "../domain/slo.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../data/database.ts";

export type DashboardItem = Readonly<Record<string, unknown>>;

export interface UserLinkResult {
  readonly userId: number;
  readonly linkedUsernames: number;
  readonly linkedAccounts: number;
  readonly missingUsernames: readonly string[];
}

export interface DashboardQueryService {
  overview(communityId: number): Promise<DashboardItem>;
  users(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]>;
  search(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]>;
  signals(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]>;
  analytics(communityId: number): Promise<DashboardItem>;
  saveQuery(
    operatorId: number,
    name: string,
    query: string,
    filters: Readonly<Record<string, unknown>>,
  ): Promise<number>;
  observationPivots(
    communityId: number,
    observationId: number,
  ): Promise<DashboardItem | null>;
  userDetail(
    communityId: number,
    userId: number,
  ): Promise<DashboardItem | null>;
  linkUser(
    communityId: number,
    operatorId: number,
    userId: number,
    platform: string,
    platformUserId: string,
  ): Promise<"linked" | "user_not_found" | "platform_account_not_found">;
  linkUsersByName(
    communityId: number,
    operatorId: number,
    selectedUserId: number,
    platform: string,
    usernames: readonly string[],
  ): Promise<UserLinkResult | null>;
  addUserNote(
    communityId: number,
    operatorId: number,
    userId: number,
    body: string,
  ): Promise<boolean>;
  unlinkUser(
    communityId: number,
    operatorId: number,
    userId: number,
    platformAccountId: number,
  ): Promise<boolean>;
  reviewIdentitySuggestion(
    communityId: number,
    operatorId: number,
    suggestionId: number,
    decision: string,
  ): Promise<boolean>;
  slo(communityId: number): Promise<readonly TenantSloSample[]>;
}

const boundedInt = (
  value: string | null,
  fallback: number,
  minimum: number,
  maximum: number,
): number => {
  if (value === null || value.trim() === "") return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed)
    ? Math.max(minimum, Math.min(parsed, maximum))
    : fallback;
};

const numericFields = new Set([
  "id",
  "user_id",
  "actor_user_id",
  "target_user_id",
  "target_platform_account_id",
  "messages_total",
  "open_reviews",
  "pending_actions",
  "derived_signals",
  "count",
  "account_count",
  "message_count",
  "evidence_count",
  "action_count",
  "report_count",
  "appeal_count",
  "joins",
  "leaves",
  "net_growth",
  "value",
  "confidence",
  "unusualness",
  "influence_score",
  "z_score",
]);

const normalizedRow = (row: DatabaseRow): DashboardItem =>
  Object.freeze(Object.fromEntries(
    Object.entries(row).map(([key, value]) => [
      key,
      numericFields.has(key) && typeof value === "string" && value.trim() !== ""
        ? Number(value)
        : value,
    ]),
  ));

const frozenRows = (rows: readonly DatabaseRow[]): readonly DashboardItem[] =>
  Object.freeze(rows.map(normalizedRow));

async function mergeCanonicalUser(
  connection: DatabaseConnection,
  targetUserId: number,
  sourceUserId: number,
): Promise<void> {
  if (targetUserId === sourceUserId) return;
  const locked = await connection.query(
    "SELECT id FROM users WHERE id=$1 FOR UPDATE",
    [sourceUserId],
  );
  if (!locked.length) return;
  await connection.query(
    `UPDATE platform_accounts SET user_id=$1,detached_from_user_id=NULL,
       updated_at=CURRENT_TIMESTAMP WHERE user_id=$2`,
    [targetUserId, sourceUserId],
  );
  await connection.query(
    "UPDATE messages SET user_id=$1 WHERE user_id=$2",
    [targetUserId, sourceUserId],
  );
  await connection.query(
    "UPDATE user_notes SET user_id=$1 WHERE user_id=$2",
    [targetUserId, sourceUserId],
  );
  await connection.query(
    "UPDATE reputation_events SET user_id=$1 WHERE user_id=$2",
    [targetUserId, sourceUserId],
  );
  await connection.query("DELETE FROM users WHERE id=$1", [sourceUserId]);
}

const IANA_TIMEZONE_ALIAS: Readonly<Record<string, string>> = {
  CST: "America/Chicago",
  CDT: "America/Chicago",
  EST: "America/New_York",
  EDT: "America/New_York",
  MST: "America/Denver",
  MDT: "America/Denver",
  PST: "America/Los_Angeles",
  PDT: "America/Los_Angeles",
};

function ianaTimeZone(zone: unknown): string {
  const raw = String(zone ?? "").trim();
  if (!raw) return "UTC";
  const aliased = IANA_TIMEZONE_ALIAS[raw.toUpperCase()] ?? raw;
  return /^[A-Za-z_]+\/[A-Za-z_]+/u.test(aliased) ? aliased : "UTC";
}

export class DashboardQueryRepository implements DashboardQueryService {
  constructor(private readonly connection: DatabaseConnection) {}

  private async communityTimeZone(communityId: number): Promise<string> {
    const row = (await this.connection.query(
      "SELECT timezone FROM communities WHERE id=$1",
      [communityId],
    ))[0];
    return ianaTimeZone(row?.timezone);
  }

  async overview(communityId: number): Promise<DashboardItem> {
    const metrics = await this.connection.query(
      `SELECT
        (SELECT COUNT(*) FROM messages WHERE community_id = $1) AS messages_total,
        (SELECT COUNT(*) FROM messages WHERE community_id = $1
          AND sent_at::timestamptz >= CURRENT_TIMESTAMP - INTERVAL '24 hours') AS messages_24h,
        (SELECT COUNT(*) FROM messages WHERE community_id = $1
          AND sent_at::timestamptz >= CURRENT_TIMESTAMP - INTERVAL '48 hours'
          AND sent_at::timestamptz < CURRENT_TIMESTAMP - INTERVAL '24 hours') AS messages_prev_24h,
        (SELECT COUNT(*) FROM review_queue AS r JOIN messages AS m ON m.id = r.message_id
          WHERE r.status = 'open' AND m.community_id = $1) AS open_reviews,
        (SELECT COUNT(*) FROM review_queue AS r JOIN messages AS m ON m.id = r.message_id
          WHERE r.status = 'open' AND m.community_id = $1
          AND r.created_at::timestamptz >= CURRENT_TIMESTAMP - INTERVAL '24 hours') AS open_reviews_24h,
        (SELECT COUNT(*) FROM moderation_actions WHERE status = 'pending' AND community_id = $1) AS pending_actions,
        (SELECT COUNT(*) FROM intelligence_alerts WHERE community_id = $1 AND status = 'open') AS open_alerts,
        (SELECT COUNT(*) FROM intelligence_alerts WHERE community_id = $1 AND status = 'open'
          AND severity IN ('critical','high')) AS high_alerts,
        (SELECT COUNT(*) FROM community_derived_signal_windows
          WHERE community_id = $1 AND window_name = '24h') AS derived_signals`,
      [communityId],
    );
    const platforms = await this.connection.query(
      `SELECT platform, COUNT(*) AS count FROM messages WHERE community_id = $1
       GROUP BY platform ORDER BY count DESC, platform`,
      [communityId],
    );
    const channels = await this.connection.query(
      `SELECT m.platform, m.channel_id,
              CASE WHEN m.platform = 'discord' AND d.channel_name IS NOT NULL
                   THEN '#' || d.channel_name ELSE m.channel_id END AS channel,
              COUNT(*) AS count
         FROM messages AS m
         LEFT JOIN discord_channels AS d ON d.channel_id = m.channel_id AND m.platform = 'discord'
        WHERE m.community_id = $1
        GROUP BY m.platform, m.channel_id, d.channel_name
        ORDER BY count DESC, m.channel_id LIMIT 5`,
      [communityId],
    );
    return Object.freeze({
      ...normalizedRow(metrics[0] ?? {}),
      top_channels: frozenRows(channels),
      top_platforms: frozenRows(platforms),
    });
  }

  async users(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    const search = query.get("q")?.trim() ?? "";
    const sort = query.get("sort") ?? "score";
    const direction = query.get("dir") === "asc" ? "ASC" : "DESC";
    const order = {
      score: "current_reputation_score",
      messages: "message_count",
      poweruser: "candidate_flag",
      accounts: "account_count",
      name: "LOWER(primary_display_name)",
    }[sort] ?? "current_reputation_score";
    const rows = await this.connection.query(
      `SELECT u.id AS user_id, u.primary_display_name, u.current_reputation_score,
              u.candidate_flag, COUNT(DISTINCT m.platform_account_id) AS account_count,
              COUNT(DISTINCT m.id) AS message_count
         FROM messages AS m
         JOIN platform_accounts AS a ON a.id = m.platform_account_id
         JOIN users AS u ON u.id = COALESCE(a.user_id, m.user_id)
        WHERE m.community_id = $1
          AND (
            u.primary_display_name ILIKE $2
            OR EXISTS (
              SELECT 1 FROM platform_accounts AS p
               WHERE p.user_id = u.id AND p.username ILIKE $2
            )
          )
        GROUP BY u.id ORDER BY ${order} ${direction}, LOWER(u.primary_display_name)
        LIMIT $3 OFFSET $4`,
      [
        communityId,
        `%${search}%`,
        boundedInt(query.get("limit"), 25, 1, 500),
        boundedInt(query.get("offset"), 0, 0, 100_000),
      ],
    );
    return Object.freeze(
      frozenRows(rows).map((row) =>
        Object.freeze({
          ...row,
          candidate_flag: Boolean(row.candidate_flag),
        })
      ),
    );
  }

  async search(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    const conditions = ["o.community_id = $1"];
    const parameters: DatabaseParameter[] = [communityId];
    const add = (clause: string, value: string | null) => {
      if (!value) return;
      parameters.push(value);
      conditions.push(`${clause} $${parameters.length}`);
    };
    const textQuery = query.get("q")?.trim() ?? "";
    if (textQuery) {
      parameters.push(textQuery);
      conditions.push(
        `o.search_vector @@ plainto_tsquery('simple', $${parameters.length})`,
      );
    }
    add("o.occurred_at >=", query.get("start_at"));
    add("o.occurred_at <=", query.get("end_at"));
    add("o.platform =", query.get("platform"));
    add("o.event_type =", query.get("event_type"));
    add("o.container_id =", query.get("container_id"));
    add("o.context_id =", query.get("context_id"));
    const userId = Number(query.get("user_id"));
    if (Number.isInteger(userId) && userId > 0) {
      parameters.push(userId, userId);
      conditions.push(
        `(actor.user_id = $${
          parameters.length - 1
        } OR target.user_id = $${parameters.length})`,
      );
    }
    parameters.push(boundedInt(query.get("limit"), 100, 1, 500));
    const limitIndex = parameters.length;
    parameters.push(boundedInt(query.get("offset"), 0, 0, 100_000));
    const timeZone = await this.communityTimeZone(communityId);
    parameters.push(timeZone);
    const zoneIndex = parameters.length;
    const rows = await this.connection.query(
      `SELECT o.id,
              to_char(o.occurred_at::timestamptz AT TIME ZONE $${zoneIndex},
                      'YYYY-MM-DD HH24:MI:SS') AS occurred_at,
              o.platform, o.event_type, o.external_event_id,
              o.container_id, o.context_id, actor.user_id AS actor_user_id,
              target.user_id AS target_user_id,
              COALESCE(actor_user.primary_display_name, actor.username) AS actor_name,
              COALESCE(target_user.primary_display_name, target.username) AS target_name,
              ca.language_code, ca.sentiment_label,
              ca.intent_label, ca.threat_level, o.text_raw
         FROM observations AS o
         LEFT JOIN platform_accounts AS actor ON actor.id = o.actor_platform_account_id
         LEFT JOIN platform_accounts AS target ON target.id = o.target_platform_account_id
         LEFT JOIN users AS actor_user ON actor_user.id = actor.user_id
         LEFT JOIN users AS target_user ON target_user.id = target.user_id
         LEFT JOIN content_analysis AS ca ON ca.observation_id = o.id
        WHERE ${conditions.join(" AND ")}
        ORDER BY o.occurred_at DESC, o.id DESC LIMIT $${limitIndex} OFFSET $${
        limitIndex + 1
      }`,
      parameters,
    );
    return frozenRows(rows);
  }

  async signals(
    communityId: number,
    query: URLSearchParams,
  ): Promise<readonly DashboardItem[]> {
    const selected = query.getAll("signal").map((value) => value.trim()).filter(
      Boolean,
    );
    const parameters: DatabaseParameter[] = [communityId];
    const filter = selected.length
      ? ` AND s.signal_key IN (${
        selected.map((value) => {
          parameters.push(value);
          return `$${parameters.length}`;
        }).join(", ")
      })`
      : "";
    const sort = query.get("sort") ?? "value";
    const column = {
      signal: "s.signal_key",
      value: "s.value_real",
      confidence: "s.confidence",
      evidence: "s.evidence_count",
      timestamp: "s.calculated_at",
    }[sort] ?? "s.value_real";
    const direction = query.get("dir") === "asc" ? "ASC" : "DESC";
    const rows = await this.connection.query(
      `SELECT s.user_id, u.primary_display_name AS display_name, s.signal_key,
              s.value_real AS value, s.confidence, s.evidence_count, s.calculated_at
         FROM community_derived_signal_windows AS s JOIN users AS u ON u.id = s.user_id
        WHERE s.community_id = $1 AND s.window_name = '24h'${filter}
        ORDER BY ${column} ${direction}, LOWER(u.primary_display_name), s.signal_key LIMIT 500`,
      parameters,
    );
    return frozenRows(rows);
  }

  async analytics(communityId: number): Promise<DashboardItem> {
    const queries: Readonly<Record<string, string>> = {
      growth:
        `SELECT joined_at::date AS metric_date, COUNT(*) AS joins FROM community_memberships WHERE community_id = $1 GROUP BY joined_at::date ORDER BY metric_date DESC LIMIT 30`,
      repeat_offenses:
        `SELECT target_platform_account_id, COUNT(*) AS action_count FROM moderation_actions WHERE community_id = $1 AND status IN ('pending','completed','confirmed') GROUP BY target_platform_account_id HAVING COUNT(*) > 1 ORDER BY action_count DESC LIMIT 25`,
      report_outcomes:
        `SELECT COALESCE(resolution,'open') AS outcome, COUNT(*) AS report_count FROM member_reports WHERE community_id = $1 GROUP BY COALESCE(resolution,'open') ORDER BY report_count DESC`,
      appeal_outcomes:
        `SELECT COALESCE(disposition,'open') AS outcome, COUNT(*) AS appeal_count FROM member_appeals WHERE community_id = $1 GROUP BY COALESCE(disposition,'open') ORDER BY appeal_count DESC`,
      rule_precision:
        `SELECT id AS rule_id, name FROM moderation_rules WHERE community_id = $1 ORDER BY name LIMIT 25`,
      topics:
        `SELECT * FROM emerging_topics WHERE community_id = $1 ORDER BY unusualness DESC, id DESC LIMIT 25`,
      graph:
        `SELECT * FROM community_graph_metrics WHERE community_id = $1 ORDER BY influence_score DESC, user_id LIMIT 25`,
      identity_suggestions:
        `SELECT * FROM community_identity_link_suggestions WHERE community_id = $1 ORDER BY confidence DESC, id DESC LIMIT 25`,
      cohort_anomalies:
        `SELECT * FROM community_cohort_anomalies WHERE community_id = $1 ORDER BY z_score DESC, user_id LIMIT 25`,
    };
    const result: Record<string, unknown> = {};
    for (const [name, sql] of Object.entries(queries)) {
      result[name] = frozenRows(
        await this.connection.query(sql, [communityId]),
      );
    }
    result.evaluation = Object.freeze([]);
    result.sort = Object.freeze({});
    return Object.freeze(result);
  }

  async saveQuery(
    operatorId: number,
    name: string,
    query: string,
    filters: Readonly<Record<string, unknown>>,
  ): Promise<number> {
    const normalizedName = name.trim();
    if (!normalizedName) {
      throw new TypeError("saved query name must not be empty");
    }
    const rows = await this.connection.query(
      `INSERT INTO saved_queries(operator_id,name,query_text,filters_json)
       VALUES ($1,$2,$3,$4)
       ON CONFLICT(operator_id,name) DO UPDATE SET
         query_text=excluded.query_text,filters_json=excluded.filters_json,
         updated_at=CURRENT_TIMESTAMP RETURNING id`,
      [operatorId, normalizedName, query.trim(), JSON.stringify(filters)],
    );
    return Number(rows[0].id);
  }

  async observationPivots(
    communityId: number,
    observationId: number,
  ): Promise<DashboardItem | null> {
    const rows = await this.connection.query(
      `SELECT o.*,actor.user_id AS actor_user_id,target.user_id AS target_user_id
         FROM observations o
         LEFT JOIN platform_accounts actor ON actor.id=o.actor_platform_account_id
         LEFT JOIN platform_accounts target ON target.id=o.target_platform_account_id
        WHERE o.id=$1 AND o.community_id=$2`,
      [observationId, communityId],
    );
    const row = rows[0];
    if (!row) return null;
    const entities = frozenRows(
      await this.connection.query(
        `SELECT entity_type,entity_value,normalized_value,confidence
         FROM content_entities WHERE observation_id=$1 ORDER BY entity_type`,
        [observationId],
      ),
    );
    const related = await this.connection.query(
      `SELECT COUNT(DISTINCT o2.id) AS count FROM observations o2
       LEFT JOIN platform_accounts a2 ON a2.id=o2.actor_platform_account_id
       LEFT JOIN platform_accounts t2 ON t2.id=o2.target_platform_account_id
       WHERE o2.id<>$1 AND o2.community_id=$2 AND
         (a2.user_id IN ($3,$4) OR t2.user_id IN ($3,$4) OR o2.context_id=$5)`,
      [
        observationId,
        communityId,
        row.actor_user_id as DatabaseParameter,
        row.target_user_id as DatabaseParameter,
        row.context_id as DatabaseParameter,
      ],
    );
    const pivots: Record<string, unknown> = {
      observation_id: observationId,
      entities,
      related_observation_count: Number(related[0]?.count ?? 0),
    };
    for (
      const key of [
        "actor_user_id",
        "target_user_id",
        "platform",
        "event_type",
        "container_id",
        "context_id",
      ]
    ) {
      if (row[key] !== null && row[key] !== "") pivots[key] = row[key];
    }
    pivots.search_links = {
      actor: row.actor_user_id == null ? null : { user_id: row.actor_user_id },
      target: row.target_user_id == null
        ? null
        : { user_id: row.target_user_id },
      context: row.context_id ? { context_id: row.context_id } : null,
      container: row.container_id ? { container_id: row.container_id } : null,
      event: Object.fromEntries(
        ["platform", "event_type", "container_id", "context_id"]
          .filter((key) => row[key] !== null && row[key] !== "")
          .map((key) => [key, row[key]]),
      ),
      entities: entities.map((item) => ({
        entity_type: item.entity_type,
        entity_value: item.normalized_value,
      })),
    };
    return Object.freeze(pivots);
  }

  async userDetail(
    communityId: number,
    userId: number,
  ): Promise<DashboardItem | null> {
    const users = await this.connection.query(
      `SELECT u.id AS user_id,u.primary_display_name,u.current_reputation_score,
              u.candidate_flag,u.score_confidence,u.score_model_version
         FROM users u WHERE u.id=$1 AND EXISTS (
           SELECT 1 FROM messages m JOIN platform_accounts a
             ON a.id=m.platform_account_id
            WHERE m.community_id=$2 AND COALESCE(m.user_id,a.user_id)=u.id)`,
      [userId, communityId],
    );
    if (!users[0]) return null;
    const linkedAccounts = frozenRows(
      await this.connection.query(
        `SELECT id,platform,platform_user_id,username,guild_or_channel_context
         FROM platform_accounts WHERE user_id=$1 AND EXISTS (
           SELECT 1 FROM messages WHERE community_id=$2
             AND platform_account_id=platform_accounts.id) ORDER BY platform,id`,
        [userId, communityId],
      ),
    );
    const notes = frozenRows(
      await this.connection.query(
        `SELECT id,operator_id,body,created_at FROM user_notes
        WHERE community_id=$1 AND user_id=$2 ORDER BY created_at DESC,id DESC`,
        [communityId, userId],
      ),
    );
    const signals = frozenRows(
      await this.connection.query(
        `SELECT signal_key,value_real AS value,confidence,evidence_count,
              window_start,window_end,value_json,analyzer_version,calculated_at
         FROM community_derived_signal_windows
        WHERE community_id=$1 AND user_id=$2 AND window_name='24h'
        ORDER BY signal_key`,
        [communityId, userId],
      ),
    );
    const lifecycle = frozenRows(
      await this.connection.query(
        `SELECT occurred_at,event_type,attributes_json
         FROM observations WHERE community_id=$1
          AND target_platform_account_id IN (
            SELECT id FROM platform_accounts WHERE user_id=$2)
          AND event_type IN ('member.joined','member.left','member.roles_changed',
            'moderation.ban_added','moderation.ban_removed')
        ORDER BY occurred_at DESC LIMIT 50`,
        [communityId, userId],
      ),
    );
    return Object.freeze({
      user: Object.freeze({
        ...normalizedRow(users[0]),
        candidate_flag: Boolean(users[0].candidate_flag),
        linked_accounts: linkedAccounts,
        notes,
      }),
      signals,
      lifecycle,
    });
  }

  async linkUser(
    communityId: number,
    operatorId: number,
    userId: number,
    platform: string,
    platformUserId: string,
  ): Promise<"linked" | "user_not_found" | "platform_account_not_found"> {
    const visible = await this.connection.query(
      `SELECT 1 FROM users u WHERE u.id=$1 AND EXISTS (
         SELECT 1 FROM messages m JOIN platform_accounts a
           ON a.id=m.platform_account_id
          WHERE m.community_id=$2 AND COALESCE(m.user_id,a.user_id)=u.id)`,
      [userId, communityId],
    );
    if (!visible.length) return "user_not_found";
    const accounts = await this.connection.query(
      `UPDATE platform_accounts SET user_id=$1,detached_from_user_id=NULL,
         updated_at=CURRENT_TIMESTAMP
       WHERE platform=$2 AND platform_user_id=$3 AND EXISTS (
         SELECT 1 FROM messages WHERE community_id=$4
           AND platform_account_id=platform_accounts.id) RETURNING id`,
      [userId, platform, platformUserId, communityId],
    );
    if (!accounts.length) return "platform_account_not_found";
    await this.connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
       VALUES ('operator',$1,'identity.account_linked','user',$2,$3)`,
      [
        operatorId,
        userId,
        JSON.stringify({ platform, platform_user_id: platformUserId }),
      ],
    );
    return "linked";
  }

  async linkUsersByName(
    communityId: number,
    operatorId: number,
    selectedUserId: number,
    platform: string,
    usernames: readonly string[],
  ): Promise<UserLinkResult | null> {
    return await this.connection.transaction(async (connection) => {
      let userId = selectedUserId;
      if (userId < 0) {
        const account = (await connection.query(
          `SELECT id,username,user_id FROM platform_accounts
            WHERE id=$1 AND EXISTS (
              SELECT 1 FROM messages WHERE community_id=$2
                AND platform_account_id=platform_accounts.id) FOR UPDATE`,
          [-userId, communityId],
        ))[0];
        if (!account) return null;
        if (account.user_id === null) {
          userId = Number(
            (await connection.query(
              `INSERT INTO users(primary_display_name) VALUES ($1) RETURNING id`,
              [String(account.username)],
            ))[0]?.id,
          );
          await connection.query(
            `UPDATE platform_accounts SET user_id=$1,detached_from_user_id=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE id=$2`,
            [userId, Number(account.id)],
          );
        } else userId = Number(account.user_id);
      }
      const visible = await connection.query(
        `SELECT 1 FROM users AS u WHERE u.id=$1 AND EXISTS (
          SELECT 1 FROM messages AS m JOIN platform_accounts AS a
            ON a.id=m.platform_account_id
           WHERE m.community_id=$2 AND COALESCE(m.user_id,a.user_id)=u.id)`,
        [userId, communityId],
      );
      if (!visible.length) return null;
      let linkedUsernames = 0;
      let linkedAccounts = 0;
      const missingUsernames: string[] = [];
      const mergedSourceUsers = new Set<number>();
      for (const username of usernames) {
        const matches = await connection.query(
          `SELECT DISTINCT p.id,p.platform,p.platform_user_id,p.user_id AS source_user_id
             FROM platform_accounts AS p
             LEFT JOIN users AS u ON u.id=p.user_id
            WHERE (LOWER(p.username)=LOWER($1) OR LOWER(u.primary_display_name)=LOWER($1))
              AND ($2='any' OR p.platform=$2)
              AND EXISTS (SELECT 1 FROM messages AS m
                WHERE m.platform_account_id=p.id AND m.community_id=$3)
            ORDER BY p.id DESC`,
          [username, platform, communityId],
        );
        if (!matches.length) {
          missingUsernames.push(username);
          continue;
        }
        for (const account of matches) {
          let sourceUserId = account.source_user_id === null ||
              account.source_user_id === undefined
            ? null
            : Number(account.source_user_id);
          if (sourceUserId === null) {
            const messageOwners = await connection.query(
              `SELECT DISTINCT user_id FROM messages
                WHERE platform_account_id=$1 AND user_id IS NOT NULL`,
              [Number(account.id)],
            );
            if (messageOwners.length === 1) {
              sourceUserId = Number(messageOwners[0].user_id);
            }
          }
          await connection.query(
            `UPDATE platform_accounts SET user_id=$1,detached_from_user_id=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE id=$2`,
            [userId, Number(account.id)],
          );
          if (
            sourceUserId !== null && sourceUserId !== userId &&
            !mergedSourceUsers.has(sourceUserId)
          ) {
            mergedSourceUsers.add(sourceUserId);
            await mergeCanonicalUser(connection, userId, sourceUserId);
          }
          await connection.query(
            `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
             VALUES ('operator',$1,'identity.account_linked','user',$2,$3)`,
            [
              operatorId,
              userId,
              JSON.stringify({
                platform: account.platform,
                platform_user_id: account.platform_user_id,
              }),
            ],
          );
          linkedAccounts++;
        }
        linkedUsernames++;
      }
      return Object.freeze({
        userId,
        linkedUsernames,
        linkedAccounts,
        missingUsernames: Object.freeze(missingUsernames),
      });
    });
  }

  async addUserNote(
    communityId: number,
    operatorId: number,
    userId: number,
    body: string,
  ): Promise<boolean> {
    const rows = await this.connection.query(
      `INSERT INTO user_notes(user_id,community_id,operator_id,body)
       SELECT $1,$2,$3,$4 WHERE EXISTS (
         SELECT 1 FROM users u WHERE u.id=$1 AND EXISTS (
           SELECT 1 FROM messages m JOIN platform_accounts a
             ON a.id=m.platform_account_id
            WHERE m.community_id=$2 AND COALESCE(m.user_id,a.user_id)=u.id))
       RETURNING id`,
      [userId, communityId, operatorId, body.trim()],
    );
    return rows.length > 0;
  }

  async unlinkUser(
    communityId: number,
    operatorId: number,
    userId: number,
    platformAccountId: number,
  ): Promise<boolean> {
    const rows = await this.connection.query(
      `UPDATE platform_accounts SET user_id=NULL,detached_from_user_id=$1,
         updated_at=CURRENT_TIMESTAMP
       WHERE id=$2 AND user_id=$1 AND EXISTS (
         SELECT 1 FROM messages WHERE community_id=$3
           AND platform_account_id=platform_accounts.id)
       RETURNING platform,platform_user_id,username`,
      [userId, platformAccountId, communityId],
    );
    if (!rows[0]) return false;
    await this.connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
       VALUES ('operator',$1,'identity.account_unlinked','user',$2,$3)`,
      [
        operatorId,
        userId,
        JSON.stringify({
          platform: rows[0].platform,
          platform_user_id: rows[0].platform_user_id,
        }),
      ],
    );
    return true;
  }

  async reviewIdentitySuggestion(
    communityId: number,
    operatorId: number,
    suggestionId: number,
    decision: string,
  ): Promise<boolean> {
    const normalized = decision.trim().toLocaleLowerCase();
    if (!new Set(["approved", "rejected"]).has(normalized)) {
      throw new TypeError("decision must be approved or rejected");
    }
    return await this.connection.transaction(async (connection) => {
      const suggestion = (await connection.query(
        `SELECT s.id,l.id AS left_id,l.user_id AS left_user_id,
                r.id AS right_id,r.user_id AS right_user_id
           FROM community_identity_link_suggestions AS s
           JOIN platform_accounts AS l ON l.id=s.left_platform_account_id
           JOIN platform_accounts AS r ON r.id=s.right_platform_account_id
          WHERE s.id=$1 AND s.community_id=$2 AND s.status='pending'
          FOR UPDATE`,
        [suggestionId, communityId],
      ))[0];
      if (!suggestion) return false;
      if (normalized === "approved") {
        const targetUserId = suggestion.left_user_id ??
          suggestion.right_user_id;
        const accountId = suggestion.left_user_id === null
          ? suggestion.left_id
          : suggestion.right_id;
        if (targetUserId === null || targetUserId === undefined) {
          throw new TypeError(
            "accounts must have a canonical user before approval",
          );
        }
        await connection.query(
          "UPDATE platform_accounts SET user_id=$1,detached_from_user_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=$2",
          [Number(targetUserId), Number(accountId)],
        );
      }
      await connection.query(
        `UPDATE community_identity_link_suggestions
            SET status=$1,reviewed_by_operator_id=$2,reviewed_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP WHERE id=$3 AND community_id=$4`,
        [normalized, operatorId, suggestionId, communityId],
      );
      return true;
    });
  }

  async slo(communityId: number): Promise<readonly TenantSloSample[]> {
    return await this.connection.transaction(async (connection) => {
      const latency = async (
        query: string,
        params: readonly DatabaseParameter[],
      ) =>
        (await connection.query(query, params)).map((row) => Number(row.value));
      const samples = [
        calculateLatencySample(
          "webhook_acceptance_ms",
          1_000,
          await latency(
            `SELECT GREATEST(0,EXTRACT(EPOCH FROM (ingested_at::timestamptz-occurred_at::timestamptz))*1000) AS value
             FROM observations WHERE community_id=$1 AND ingested_at::timestamptz>=CURRENT_TIMESTAMP-INTERVAL '24 hours'`,
            [communityId],
          ),
        ),
        calculateLatencySample(
          "event_to_alert_ms",
          10_000,
          await latency(
            `SELECT GREATEST(0,EXTRACT(EPOCH FROM (a.created_at::timestamptz-o.ingested_at::timestamptz))*1000) AS value
             FROM intelligence_alerts AS a JOIN observations AS o ON o.id=a.observation_id
            WHERE a.community_id=$1 AND a.created_at::timestamptz>=CURRENT_TIMESTAMP-INTERVAL '24 hours'`,
            [communityId],
          ),
        ),
        calculateLatencySample(
          "moderation_confirmation_ms",
          30_000,
          await latency(
            `SELECT GREATEST(0,EXTRACT(EPOCH FROM (provider_confirmed_at::timestamptz-created_at::timestamptz))*1000) AS value
             FROM moderation_actions WHERE community_id=$1 AND provider_confirmed_at IS NOT NULL
               AND created_at::timestamptz>=CURRENT_TIMESTAMP-INTERVAL '24 hours'`,
            [communityId],
          ),
        ),
        calculateLatencySample(
          "queue_age_seconds",
          900,
          await latency(
            `SELECT GREATEST(0,EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-created_at))) AS value FROM (
             SELECT q.created_at FROM review_queue AS q JOIN messages AS m ON m.id=q.message_id
              WHERE m.community_id=$1 AND q.status='open'
             UNION ALL SELECT created_at FROM member_reports WHERE community_id=$1 AND status='open'
             UNION ALL SELECT created_at FROM member_appeals WHERE community_id=$1 AND status='open') AS work`,
            [communityId],
          ),
        ),
        calculatePercentageSample(
          "connector_health_percent",
          99,
          (await connection.query(
            `SELECT CASE WHEN status='active' AND health_status='healthy' THEN 1 ELSE 0 END AS value
               FROM community_installations WHERE community_id=$1`,
            [communityId],
          )).map((row) => Number(row.value)),
        ),
        calculatePercentageSample(
          "dashboard_availability_percent",
          99.5,
          (await connection.query(
            `SELECT is_up AS value FROM service_reliability_buckets
              WHERE service_name='web' AND bucket_start>=CURRENT_TIMESTAMP-INTERVAL '24 hours'`,
          )).map((row) => Number(row.value)),
        ),
        calculateCountSample(
          "open_dead_letters",
          0,
          Number(
            (await connection.query(
              "SELECT COUNT(*) AS value FROM dead_letter_events WHERE community_id=$1 AND status='open'",
              [communityId],
            ))[0]?.value ?? 0,
          ),
        ),
        calculateFreshnessSample(
          Number(
            (await connection.query(
              `SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MAX(observed_at))) AS value
             FROM operational_metrics WHERE metric_name='backup.success'`,
            ))[0]?.value,
          ) || null,
        ),
      ];
      for (const sample of samples) {
        await connection.query(
          `INSERT INTO tenant_slo_samples(
             community_id,metric_name,value,target_value,status,details_json,observed_at)
           VALUES ($1,$2,$3,$4,$5,$6,CURRENT_TIMESTAMP)`,
          [
            communityId,
            sample.metricName,
            sample.value,
            sample.targetValue,
            sample.status,
            JSON.stringify({ evidence_count: sample.evidenceCount }),
          ],
        );
      }
      return Object.freeze(samples);
    });
  }
}
