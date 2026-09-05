import {
  calculateBaselineAnomaly,
  calculateCohortBaseline,
  calculateEmergingTopics,
  calculateGraphMetrics,
  calculateIdentitySuggestions,
  calculatePeerAnomaly,
  COHORT_MIN_SAMPLE_SIZE,
  type CohortValue,
  type IdentityAccount,
  type RelationshipEdge,
  type TopicObservation,
} from "./analytics.ts";
import type { DatabaseConnection } from "../data/database.ts";
import { calculateDerivedSignals, type SignalEvidence } from "./signals.ts";
import { PostgresNotificationRepository } from "./notifications.ts";

export interface AnalyticsRefreshService {
  refresh(now: Date): Promise<AnalyticsRefreshReport>;
}

export interface AnalyticsRefreshReport {
  readonly topicCount: number;
  readonly graphNodeCount: number;
  readonly identitySuggestionCount: number;
  readonly cohortBaselineCount: number;
  readonly evaluationRunId: number;
}

export class PostgresAnalyticsRepository implements AnalyticsRefreshService {
  constructor(private readonly connection: DatabaseConnection) {}

  async refresh(now: Date): Promise<AnalyticsRefreshReport> {
    requireDate(now);
    await this.refreshDerivedSignals(now);
    await this.refreshLiveStreamCohorts();
    const communities = await this.connection.query(
      "SELECT id,status FROM communities ORDER BY id",
    );
    let topicCount = 0;
    let graphNodeCount = 0;
    let identitySuggestionCount = 0;
    let cohortBaselineCount = 0;
    for (const community of communities) {
      const communityId = requirePositiveInteger(community.id, "community.id");
      topicCount += await this.refreshEmergingTopics(communityId, now);
      graphNodeCount += await this.refreshGraphAnalytics(communityId, now);
      identitySuggestionCount += await this.refreshIdentitySuggestions(
        communityId,
      );
      cohortBaselineCount += await this.refreshCommunityCohorts(
        communityId,
        now,
      );
    }
    const evaluationRunId = await this.runModelEvaluation(now);
    for (const community of communities) {
      if (community.status !== "active") continue;
      const communityId = requirePositiveInteger(community.id, "community.id");
      await this.emitAnalyticsAlerts(communityId, now);
      await new PostgresNotificationRepository(this.connection)
        .dispatchPending(communityId);
    }
    await this.recordSuccessMetric("analytics.refresh.success", 1, now);
    return Object.freeze({
      topicCount,
      graphNodeCount,
      identitySuggestionCount,
      cohortBaselineCount,
      evaluationRunId,
    });
  }

  async emitAnalyticsAlerts(communityId: number, now: Date): Promise<void> {
    requirePositiveInteger(communityId, "communityId");
    requireDate(now);
    const timestamp = now.toISOString();
    const baselineStart = new Date(now.getTime() - 8 * 86_400_000)
      .toISOString();
    const currentStart = new Date(now.getTime() - 86_400_000).toISOString();
    const baseline = await this.connection.query(
      `SELECT COUNT(DISTINCT DATE(occurred_at::timestamptz)) AS active_days
         FROM observations
        WHERE community_id=$1 AND occurred_at::timestamptz>=$2::timestamptz
          AND occurred_at::timestamptz<$3::timestamptz
          AND text_raw IS NOT NULL AND BTRIM(text_raw)<>''`,
      [communityId, baselineStart, currentStart],
    );
    await this.connection.transaction(async (connection) => {
      if (Number(baseline[0]?.active_days ?? 0) >= 3) {
        await connection.query(
          `INSERT INTO intelligence_alerts(
             community_id,observation_id,alert_type,severity,title,summary,
             confidence,dedupe_key,created_at,updated_at
           ) SELECT t.community_id,
               (SELECT e.observation_id FROM topic_evidence e
                 WHERE e.community_id=t.community_id AND e.topic_key=t.topic_key
                 ORDER BY e.occurred_at DESC,e.observation_id DESC LIMIT 1),
               'emerging_topic',CASE WHEN t.unusualness>=18 THEN 'high' ELSE 'medium' END,
               'Emerging Topic',t.label || ' rose to ' || t.current_count ||
                 ' observations across ' || t.context_count || ' contexts (unusualness ' ||
                 ROUND(t.unusualness::numeric,2) || ').',LEAST(0.99,t.unusualness/20.0),
               'topic:' || t.topic_key,$2,$2
             FROM emerging_topics t WHERE t.community_id=$1 AND (
               (t.topic_kind='term' AND t.current_count>=8 AND t.context_count>=3 AND t.unusualness>=12) OR
               (t.topic_kind='phrase' AND t.current_count>=5 AND t.context_count>=2 AND t.unusualness>=10) OR
               (t.topic_kind='domain' AND t.current_count>=3 AND t.context_count>=2 AND t.unusualness>=8))
             ORDER BY t.unusualness DESC,t.current_count DESC,t.topic_key LIMIT 10
           ON CONFLICT(dedupe_key) DO UPDATE SET
             observation_id=COALESCE(excluded.observation_id,intelligence_alerts.observation_id),
             severity=excluded.severity,summary=excluded.summary,
             confidence=excluded.confidence,
             status=CASE WHEN intelligence_alerts.status='resolved' AND intelligence_alerts.disposition='expired' THEN 'open' ELSE intelligence_alerts.status END,
             disposition=CASE WHEN intelligence_alerts.status='resolved' AND intelligence_alerts.disposition='expired' THEN NULL ELSE intelligence_alerts.disposition END,
             resolved_at=CASE WHEN intelligence_alerts.status='resolved' AND intelligence_alerts.disposition='expired' THEN NULL ELSE intelligence_alerts.resolved_at END,
             updated_at=excluded.updated_at`,
          [communityId, timestamp],
        );
      }
      await connection.query(
        `INSERT INTO intelligence_alerts(
           community_id,user_id,alert_type,severity,title,summary,confidence,
           dedupe_key,created_at,updated_at
         ) SELECT community_id,user_id,'cohort_anomaly','high','Cohort Anomaly',
             signal_key || ' deviates ' || ROUND(z_score::numeric,2) ||
               ' sigma from ' || cohort_type || ':' || cohort_key || ' peers.',
             confidence,'cohort:' || user_id || ':' || cohort_type || ':' ||
               cohort_key || ':' || signal_key,$2,$2
           FROM community_cohort_anomalies
          WHERE community_id=$1 AND ABS(z_score)>=3 AND confidence>=0.7
         ON CONFLICT(dedupe_key) DO UPDATE SET summary=excluded.summary,
           confidence=excluded.confidence,updated_at=excluded.updated_at`,
        [communityId, timestamp],
      );
      await connection.query(
        `INSERT INTO intelligence_alerts(
           community_id,user_id,alert_type,severity,title,summary,confidence,
           dedupe_key,created_at,updated_at
         ) SELECT community_id,user_id,'graph_bridge','medium','Network Bridge',
             'Entity is a high-influence bridge (' || ROUND(influence_score::numeric,3) || ').',
             LEAST(0.95,influence_score),'graph-bridge:' || community_id || ':' || user_id,$2,$2
           FROM community_graph_metrics
          WHERE community_id=$1 AND is_bridge=1 AND influence_score>=0.45
         ON CONFLICT(dedupe_key) DO UPDATE SET summary=excluded.summary,
           confidence=excluded.confidence,updated_at=excluded.updated_at`,
        [communityId, timestamp],
      );
      for (
        const alertType of ["emerging_topic", "cohort_anomaly", "graph_bridge"]
      ) {
        await connection.query(
          `UPDATE intelligence_alerts SET status='resolved',disposition='expired',
             resolved_at=$2,updated_at=$2
           WHERE community_id=$1 AND alert_type=$3
             AND status IN ('open','acknowledged','suppressed')
             AND updated_at::timestamptz<$2::timestamptz`,
          [communityId, timestamp, alertType],
        );
      }
      await connection.query(
        `UPDATE intelligence_alerts SET status='resolved',disposition='expired',
           resolved_at=$2,updated_at=$2
         WHERE community_id=$1 AND alert_type='coordination_pattern'
           AND status IN ('open','acknowledged','suppressed')
           AND dedupe_key NOT IN (
             SELECT 'relationship:' || id || ':coordination'
               FROM entity_relationships
              WHERE community_id=$1 AND evidence_count>=6
           )`,
        [communityId, timestamp],
      );
    });
  }

  async recordSuccessMetric(
    name: string,
    value: number,
    now: Date,
  ): Promise<void> {
    await this.connection.query(
      `INSERT INTO operational_metrics(metric_name,value,observed_at)
       VALUES ($1,$2,$3)`,
      [name, value, now.toISOString()],
    );
  }

  async refreshDerivedSignals(now: Date): Promise<number> {
    requireDate(now);
    const rows = await this.connection.query(
      `SELECT u.id AS user_id,
              COUNT(DISTINCT m.id) AS message_count,
              COUNT(DISTINCT m.platform || ':' || m.channel_id) AS channel_count,
              COUNT(DISTINCT m.platform) AS platform_count,
              COUNT(DISTINCT linked.id) AS account_count,
              MIN(m.sent_at) AS window_start,MAX(m.sent_at) AS window_end,
              COUNT(DISTINCT re.source_id) FILTER (WHERE re.source_type='message'
                AND re.reason_code IN ('message_sent','positive_message','very_negative_content','reply_to_non_bot')) AS eligible_message_count,
              COUNT(re.id) FILTER (WHERE re.reason_code='positive_message') AS positive_count,
              COUNT(re.id) FILTER (WHERE re.reason_code='very_negative_content') AS negative_count,
              COALESCE(SUM(ABS(re.delta)) FILTER (WHERE re.reason_code='very_negative_content'),0) AS negative_points,
              COUNT(re.id) FILTER (WHERE re.reason_code='reply_to_non_bot') AS reply_count,
              COUNT(re.id) FILTER (WHERE re.reason_code='welcome_new_user') AS welcome_positive_count,
              COUNT(re.id) FILTER (WHERE re.reason_code='welcome_spam_duplicate') AS welcome_duplicate_count,
              COUNT(DISTINCT we.id) AS welcome_count,
              COUNT(DISTINCT rm.id) AS finding_count,
              COALESCE(SUM(DISTINCT CASE rm.severity WHEN 'high' THEN 1.0 WHEN 'medium' THEN 0.6 ELSE 0.25 END),0) AS severity_points,
              COALESCE(SUM(ABS(re.delta)) FILTER (WHERE re.source_type='moderation'),0) AS moderation_penalty_points
         FROM users u
         LEFT JOIN platform_accounts linked ON linked.user_id=u.id
         LEFT JOIN messages m ON COALESCE(m.user_id,linked.user_id)=u.id
           AND m.platform_account_id=linked.id
         LEFT JOIN reputation_events re ON re.user_id=u.id
         LEFT JOIN welcome_events we ON we.message_id=m.id
         LEFT JOIN rule_matches rm ON rm.message_id=m.id
        GROUP BY u.id ORDER BY u.id`,
    );
    for (const row of rows) {
      const evidence: SignalEvidence = {
        userId: requirePositiveInteger(row.user_id, "signal.userId"),
        messageCount: Number(row.message_count),
        channelCount: Number(row.channel_count),
        platformCount: Number(row.platform_count),
        accountCount: Number(row.account_count),
        eligibleMessageCount: Number(row.eligible_message_count),
        positiveCount: Number(row.positive_count),
        negativeCount: Number(row.negative_count),
        negativePoints: Number(row.negative_points),
        replyCount: Number(row.reply_count),
        welcomePositiveCount: Number(row.welcome_positive_count),
        welcomeCount: Number(row.welcome_count),
        welcomeDuplicateCount: Number(row.welcome_duplicate_count),
        findingCount: Number(row.finding_count),
        severityPoints: Number(row.severity_points),
        moderationPenaltyPoints: Number(row.moderation_penalty_points),
        windowStart: row.window_start == null ? null : String(row.window_start),
        windowEnd: row.window_end == null ? null : String(row.window_end),
      };
      for (
        const signal of calculateDerivedSignals(evidence, now.toISOString())
      ) {
        await this.connection.query(
          `INSERT INTO derived_signals(
             user_id,signal_key,analyzer_version,value_real,value_json,
             confidence,evidence_count,window_start,window_end,calculated_at,updated_at
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)
           ON CONFLICT(user_id,signal_key,analyzer_version) DO UPDATE SET
             value_real=excluded.value_real,value_json=excluded.value_json,
             confidence=excluded.confidence,evidence_count=excluded.evidence_count,
             window_start=excluded.window_start,window_end=excluded.window_end,
             calculated_at=excluded.calculated_at,updated_at=excluded.updated_at`,
          [
            signal.userId,
            signal.signalKey,
            signal.analyzerVersion,
            signal.value,
            JSON.stringify(signal.details),
            signal.confidence,
            signal.evidenceCount,
            signal.windowStart,
            signal.windowEnd,
            signal.calculatedAt,
          ],
        );
      }
    }
    return rows.length;
  }

  async refreshLiveStreamCohorts(): Promise<number> {
    const sessions = await this.connection.query(
      "SELECT id,community_id,started_at,COALESCE(ended_at::timestamptz,CURRENT_TIMESTAMP) AS ended_at FROM stream_sessions WHERE status='live' ORDER BY id",
    );
    for (const session of sessions) {
      const sessionId = requirePositiveInteger(session.id, "streamSession.id");
      const members = await this.connection.query(
        `SELECT a.id,COUNT(m.id) AS message_count,
                EXISTS(SELECT 1 FROM messages prior
                  WHERE prior.community_id=$1 AND prior.platform_account_id=a.id
                    AND prior.sent_at::timestamptz<$2::timestamptz) AS returning,
                BOOL_OR(COALESCE((o.attributes_json::jsonb->>'subscriber')::boolean,false)) AS subscriber,
                BOOL_OR(LOWER(o.attributes_json) LIKE '%vip%') AS vip,
                BOOL_OR(COALESCE((o.attributes_json::jsonb->>'is_moderator')::boolean,false)
                  OR LOWER(o.attributes_json) LIKE '%moderator%') AS moderator
           FROM messages m JOIN platform_accounts a ON a.id=m.platform_account_id
           LEFT JOIN observations o ON o.id=m.observation_id
          WHERE m.community_id=$1 AND m.sent_at::timestamptz>=$2::timestamptz
            AND m.sent_at::timestamptz<=$3::timestamptz
          GROUP BY a.id ORDER BY a.id`,
        [
          requirePositiveInteger(
            session.community_id,
            "streamSession.communityId",
          ),
          String(session.started_at),
          String(session.ended_at),
        ],
      );
      const counts = new Map(
        ["unique", "new", "returning", "subscriber", "vip", "moderator"]
          .map((key) => [key, { members: 0, messages: 0 }]),
      );
      for (const member of members) {
        const keys = ["unique", member.returning ? "returning" : "new"];
        for (const key of ["subscriber", "vip", "moderator"]) {
          if (member[key]) keys.push(key);
        }
        for (const key of keys) {
          const count = counts.get(key)!;
          count.members += 1;
          count.messages += Number(member.message_count);
        }
      }
      for (const [key, count] of counts) {
        await this.connection.query(
          `INSERT INTO stream_cohort_snapshots(
             stream_session_id,cohort_key,member_count,message_count
           ) VALUES ($1,$2,$3,$4)
           ON CONFLICT(stream_session_id,cohort_key) DO UPDATE SET
             member_count=excluded.member_count,message_count=excluded.message_count,
             calculated_at=CURRENT_TIMESTAMP`,
          [sessionId, key, count.members, count.messages],
        );
      }
    }
    return sessions.length;
  }

  async refreshGraphAnalytics(communityId: number, now: Date): Promise<number> {
    requirePositiveInteger(communityId, "communityId");
    requireDate(now);
    const calculatedAt = now.toISOString();
    const rows = await this.connection.query(
      `SELECT source_user_id,target_user_id,strength,last_observed_at
         FROM entity_relationships WHERE community_id=$1`,
      [communityId],
    );
    const metrics = calculateGraphMetrics(
      rows.map((row) => ({
        sourceUserId: requirePositiveInteger(
          row.source_user_id,
          "sourceUserId",
        ),
        targetUserId: requirePositiveInteger(
          row.target_user_id,
          "targetUserId",
        ),
        strength: Number(row.strength),
        lastObservedAt: String(row.last_observed_at),
      } satisfies RelationshipEdge)),
      calculatedAt,
    );
    await this.connection.transaction(async (connection) => {
      await connection.query(
        "DELETE FROM community_graph_metrics WHERE community_id=$1",
        [communityId],
      );
      for (const metric of metrics) {
        const parameters = [
          communityId,
          metric.userId,
          metric.inDegree,
          metric.outDegree,
          metric.weightedDegree,
          metric.betweenness,
          metric.pagerank,
          metric.clusterId,
          metric.isBridge,
          metric.influenceScore,
          calculatedAt,
        ] as const;
        await connection.query(
          `INSERT INTO community_graph_metrics(
             community_id,user_id,in_degree,out_degree,weighted_degree,
             betweenness,pagerank,cluster_id,is_bridge,influence_score,calculated_at
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
          parameters,
        );
        await connection.query(
          `INSERT INTO community_graph_metric_history(
             community_id,user_id,in_degree,out_degree,weighted_degree,
             betweenness,pagerank,cluster_id,is_bridge,influence_score,calculated_at
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
          parameters,
        );
      }
    });
    return metrics.length;
  }

  async refreshIdentitySuggestions(communityId: number): Promise<number> {
    requirePositiveInteger(communityId, "communityId");
    const rows = await this.connection.query(
      `SELECT id,platform,platform_user_id,username,user_id,
              guild_or_channel_context
         FROM platform_accounts
        WHERE EXISTS (
          SELECT 1 FROM messages
           WHERE messages.platform_account_id=platform_accounts.id
             AND messages.community_id=$1
        ) ORDER BY id`,
      [communityId],
    );
    const suggestions = calculateIdentitySuggestions(rows.map((row) => ({
      accountId: requirePositiveInteger(row.id, "account.id"),
      platform: String(row.platform),
      platformUserId: String(row.platform_user_id),
      username: String(row.username),
      userId: row.user_id === null || row.user_id === undefined
        ? null
        : requirePositiveInteger(row.user_id, "account.userId"),
      context: String(row.guild_or_channel_context ?? ""),
    } satisfies IdentityAccount)));
    let inserted = 0;
    for (const suggestion of suggestions) {
      const rows = await this.connection.query(
        `INSERT INTO community_identity_link_suggestions(
           community_id,left_platform_account_id,right_platform_account_id,
           confidence,evidence_json,model_version
         ) VALUES ($1,$2,$3,$4,$5,$6)
         ON CONFLICT(
           community_id,left_platform_account_id,right_platform_account_id,
           model_version
         ) DO UPDATE SET confidence=excluded.confidence,
           evidence_json=excluded.evidence_json,updated_at=CURRENT_TIMESTAMP
         WHERE community_identity_link_suggestions.status='pending'
         RETURNING id`,
        [
          communityId,
          suggestion.leftPlatformAccountId,
          suggestion.rightPlatformAccountId,
          suggestion.confidence,
          JSON.stringify({
            identifier_similarity: suggestion.identifierSimilarity,
            manual_approval_required: suggestion.manualApprovalRequired,
            shared_context: suggestion.sharedContext,
            username_similarity: suggestion.usernameSimilarity,
          }),
          suggestion.modelVersion,
        ],
      );
      inserted += Number(rows.length > 0);
    }
    return inserted;
  }

  async refreshCommunityCohorts(
    communityId: number,
    now: Date,
  ): Promise<number> {
    requirePositiveInteger(communityId, "communityId");
    requireDate(now);
    const [signalRows, membershipRows, historyRows] = await Promise.all([
      this.connection.query(
        `SELECT user_id,signal_key,value_real,confidence
           FROM community_derived_signal_windows
          WHERE community_id=$1 AND window_name='24h'`,
        [communityId],
      ),
      this.connection.query(
        `SELECT DISTINCT COALESCE(m.user_id,a.user_id) AS user_id,
                'platform' AS cohort_type,a.platform AS cohort_key
           FROM messages m JOIN platform_accounts a ON a.id=m.platform_account_id
          WHERE m.community_id=$1 AND COALESCE(m.user_id,a.user_id) IS NOT NULL
         UNION
         SELECT DISTINCT a.user_id,'community',COALESCE(o.context_id,o.container_id)
           FROM observations o JOIN platform_accounts a
             ON a.id=o.actor_platform_account_id
          WHERE o.community_id=$1 AND a.user_id IS NOT NULL
            AND COALESCE(o.context_id,o.container_id) IS NOT NULL`,
        [communityId],
      ),
      this.connection.query(
        `SELECT user_id,signal_key,value_real
           FROM community_derived_signal_history
          WHERE community_id=$1 AND window_name='24h'
          ORDER BY calculated_at`,
        [communityId],
      ),
    ]);
    const current = new Map<string, CohortValue>();
    for (const row of signalRows) {
      const value = {
        userId: requirePositiveInteger(row.user_id, "signal.userId"),
        value: Number(row.value_real),
        confidence: Number(row.confidence),
      };
      current.set(`${value.userId}:${String(row.signal_key)}`, value);
    }
    const memberships = new Map<number, Array<readonly [string, string]>>();
    for (const row of membershipRows) {
      const userId = requirePositiveInteger(row.user_id, "membership.userId");
      const values = memberships.get(userId) ?? [];
      values.push([String(row.cohort_type), String(row.cohort_key)]);
      memberships.set(userId, values);
    }
    const cohorts = new Map<string, CohortValue[]>();
    for (const [key, value] of current) {
      const signalKey = key.slice(key.indexOf(":") + 1);
      for (
        const [cohortType, cohortKey] of memberships.get(value.userId) ?? []
      ) {
        const cohortId = JSON.stringify([cohortType, cohortKey, signalKey]);
        const values = cohorts.get(cohortId) ?? [];
        values.push(value);
        cohorts.set(cohortId, values);
      }
    }
    const histories = new Map<string, number[]>();
    for (const row of historyRows) {
      const key = `${requirePositiveInteger(row.user_id, "history.userId")}:${
        String(row.signal_key)
      }`;
      const values = histories.get(key) ?? [];
      values.push(Number(row.value_real));
      histories.set(key, values);
    }
    let baselineCount = 0;
    await this.connection.transaction(async (connection) => {
      await connection.query(
        "DELETE FROM community_cohort_baselines WHERE community_id=$1",
        [communityId],
      );
      await connection.query(
        "DELETE FROM community_cohort_anomalies WHERE community_id=$1",
        [communityId],
      );
      for (const [cohortId, values] of [...cohorts].sort()) {
        const [cohortType, cohortKey, signalKey] = JSON.parse(
          cohortId,
        ) as string[];
        const baseline = calculateCohortBaseline(
          values.map((item) => item.value),
        );
        await insertCohortBaseline(
          connection,
          communityId,
          cohortType,
          cohortKey,
          signalKey,
          baseline,
          now,
        );
        baselineCount += 1;
        if (
          baseline.sampleSize < COHORT_MIN_SAMPLE_SIZE ||
          baseline.stddevValue === 0
        ) continue;
        for (const value of values) {
          const anomaly = calculatePeerAnomaly(values, value.userId);
          if (anomaly) {
            await insertCohortAnomaly(
              connection,
              communityId,
              value.userId,
              cohortType,
              cohortKey,
              signalKey,
              anomaly,
              now,
            );
          }
        }
      }
      for (const [key, values] of [...histories].sort()) {
        const selected = current.get(key);
        if (values.length < COHORT_MIN_SAMPLE_SIZE || !selected) continue;
        const separator = key.indexOf(":");
        const userId = Number(key.slice(0, separator));
        const signalKey = key.slice(separator + 1);
        const baseline = calculateCohortBaseline(values);
        await insertCohortBaseline(
          connection,
          communityId,
          "self",
          String(userId),
          signalKey,
          baseline,
          now,
        );
        baselineCount += 1;
        const anomaly = calculateBaselineAnomaly(
          selected.value,
          selected.confidence,
          baseline,
        );
        if (anomaly) {
          await insertCohortAnomaly(
            connection,
            communityId,
            userId,
            "self",
            String(userId),
            signalKey,
            anomaly,
            now,
          );
        }
      }
    });
    return baselineCount;
  }

  async runModelEvaluation(now: Date): Promise<number> {
    requireDate(now);
    const modelKey = "risk.composite";
    const modelVersion = 2;
    const rows = await this.connection.query(
      `SELECT l.label_value,COALESCE(l.score_value,h.value_real,d.value_real) AS score,
              a.alert_type
         FROM evaluation_labels l
         LEFT JOIN intelligence_alerts a ON a.id=l.alert_id
         LEFT JOIN derived_signal_history h
           ON h.id=a.signal_history_id AND h.signal_key=$1
         LEFT JOIN derived_signals d
           ON d.user_id=COALESCE(l.user_id,a.user_id) AND d.signal_key=$1
        WHERE l.label_value IN ('positive','negative')
          AND COALESCE(l.score_key,h.signal_key,d.signal_key)=$1
          AND COALESCE(l.model_version,h.analyzer_version,d.analyzer_version,$2)=$2`,
      [modelKey, modelVersion],
    );
    const backtests = [25, 50, 75].map((threshold) => {
      let truePositive = 0;
      let falsePositive = 0;
      let trueNegative = 0;
      let falseNegative = 0;
      for (const row of rows) {
        const predicted = Number(row.score ?? 0) >= threshold;
        const positive = row.label_value === "positive";
        truePositive += Number(predicted && positive);
        falsePositive += Number(predicted && !positive);
        trueNegative += Number(!predicted && !positive);
        falseNegative += Number(!predicted && positive);
      }
      return {
        threshold,
        truePositive,
        falsePositive,
        trueNegative,
        falseNegative,
        precision: truePositive / (truePositive + falsePositive || 1),
        recall: truePositive / (truePositive + falseNegative || 1),
        falsePositiveRate: falsePositive / (falsePositive + trueNegative || 1),
      };
    });
    const current = backtests[1];
    const falsePositiveTypes: Record<string, number> = {};
    for (const row of rows) {
      if (row.label_value !== "negative" || Number(row.score ?? 0) < 50) {
        continue;
      }
      const key = String(row.alert_type ?? "untyped");
      falsePositiveTypes[key] = (falsePositiveTypes[key] ?? 0) + 1;
    }
    const distribution = { "0-24": 0, "25-49": 0, "50-74": 0, "75-100": 0 };
    for (const row of rows) {
      const score = Math.max(0, Math.min(100, Number(row.score)));
      const key = score < 25
        ? "0-24"
        : score < 50
        ? "25-49"
        : score < 75
        ? "50-74"
        : "75-100";
      distribution[key] += 1;
    }
    return await this.connection.transaction(async (connection) => {
      const inserted = await connection.query(
        `INSERT INTO model_evaluation_runs(
           model_key,model_version,sample_size,metrics_json,
           score_distribution_json,calculated_at
         ) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id`,
        [
          modelKey,
          modelVersion,
          rows.length,
          JSON.stringify({
            false_positive_alert_types: falsePositiveTypes,
            false_positive_rate: current.falsePositiveRate,
            labeled_negative: rows.filter((row) =>
              row.label_value === "negative"
            ).length,
            labeled_positive: rows.filter((row) =>
              row.label_value === "positive"
            ).length,
            precision: current.precision,
            recall: current.recall,
          }),
          JSON.stringify(distribution),
          now.toISOString(),
        ],
      );
      const runId = requirePositiveInteger(inserted[0]?.id, "evaluationRun.id");
      for (const item of backtests) {
        await connection.query(
          `INSERT INTO threshold_backtests(
             evaluation_run_id,threshold,true_positive,false_positive,
             true_negative,false_negative,precision,recall,false_positive_rate
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
          [
            runId,
            item.threshold,
            item.truePositive,
            item.falsePositive,
            item.trueNegative,
            item.falseNegative,
            item.precision,
            item.recall,
            item.falsePositiveRate,
          ],
        );
      }
      return runId;
    });
  }

  async refreshEmergingTopics(communityId: number, now: Date): Promise<number> {
    requirePositiveInteger(communityId, "communityId");
    requireDate(now);
    const calculatedAt = now.toISOString();
    const baselineStart = new Date(now.getTime() - 8 * 86_400_000)
      .toISOString();
    const rows = await this.connection.query(
      `SELECT id,text_raw,context_id,container_id,occurred_at
         FROM observations
        WHERE community_id=$1 AND occurred_at::timestamptz>=$2::timestamptz
          AND text_raw IS NOT NULL AND BTRIM(text_raw)<>''
        ORDER BY occurred_at,id`,
      [communityId, baselineStart],
    );
    const topics = calculateEmergingTopics(
      rows.map((row) => ({
        observationId: requirePositiveInteger(row.id, "observation.id"),
        text: String(row.text_raw),
        contextId: String(row.context_id ?? ""),
        containerId: String(row.container_id ?? ""),
        occurredAt: String(row.occurred_at),
      } satisfies TopicObservation)),
      communityId,
      calculatedAt,
    );

    await this.connection.transaction(async (connection) => {
      await connection.query(
        "DELETE FROM emerging_topics WHERE community_id=$1",
        [communityId],
      );
      await connection.query(
        "DELETE FROM topic_evidence WHERE community_id=$1",
        [communityId],
      );
      for (const topic of topics) {
        await connection.query(
          `INSERT INTO emerging_topics(
             community_id,topic_key,topic_kind,label,current_count,baseline_rate,
             velocity,context_count,community_count,unusualness,
             first_observed_at,last_observed_at,details_json,calculated_at
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
          [
            communityId,
            topic.topicKey,
            topic.topicKind,
            topic.label,
            topic.currentCount,
            topic.baselineRate,
            topic.velocity,
            topic.contextCount,
            topic.communityCount,
            topic.unusualness,
            topic.firstObservedAt,
            topic.lastObservedAt,
            JSON.stringify({
              cluster_terms: topic.clusterTerms,
              cross_community_diffusion: topic.crossCommunityDiffusion,
            }),
            calculatedAt,
          ],
        );
        await connection.query(
          `INSERT INTO topic_history(
             community_id,topic_key,topic_kind,current_count,baseline_rate,
             velocity,context_count,community_count,unusualness,calculated_at
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
          [
            communityId,
            topic.topicKey,
            topic.topicKind,
            topic.currentCount,
            topic.baselineRate,
            topic.velocity,
            topic.contextCount,
            topic.communityCount,
            topic.unusualness,
            calculatedAt,
          ],
        );
        for (const [observationId, contextKey, occurredAt] of topic.evidence) {
          await connection.query(
            `INSERT INTO topic_evidence(
               community_id,topic_key,observation_id,context_key,occurred_at
             ) VALUES ($1,$2,$3,$4,$5)
             ON CONFLICT(topic_key,observation_id) DO NOTHING`,
            [
              communityId,
              topic.topicKey,
              observationId,
              contextKey,
              occurredAt,
            ],
          );
        }
      }
    });
    return topics.length;
  }
}

function requirePositiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} must be a positive integer`);
  }
  return parsed;
}

function requireDate(value: Date): void {
  if (Number.isNaN(value.getTime())) throw new TypeError("now is invalid");
}

async function insertCohortBaseline(
  connection: DatabaseConnection,
  communityId: number,
  cohortType: string,
  cohortKey: string,
  signalKey: string,
  baseline: ReturnType<typeof calculateCohortBaseline>,
  now: Date,
): Promise<void> {
  await connection.query(
    `INSERT INTO community_cohort_baselines(
       community_id,cohort_type,cohort_key,signal_key,sample_size,
       mean_value,stddev_value,median_value,p90_value,calculated_at
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
    [
      communityId,
      cohortType,
      cohortKey,
      signalKey,
      baseline.sampleSize,
      baseline.meanValue,
      baseline.stddevValue,
      baseline.medianValue,
      baseline.p90Value,
      now.toISOString(),
    ],
  );
}

async function insertCohortAnomaly(
  connection: DatabaseConnection,
  communityId: number,
  userId: number,
  cohortType: string,
  cohortKey: string,
  signalKey: string,
  anomaly: NonNullable<ReturnType<typeof calculateBaselineAnomaly>>,
  now: Date,
): Promise<void> {
  await connection.query(
    `INSERT INTO community_cohort_anomalies(
       community_id,user_id,cohort_type,cohort_key,signal_key,observed_value,
       baseline_mean,z_score,direction,confidence,calculated_at
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
    [
      communityId,
      userId,
      cohortType,
      cohortKey,
      signalKey,
      anomaly.observedValue,
      anomaly.baselineMean,
      anomaly.zScore,
      anomaly.direction,
      anomaly.confidence,
      now.toISOString(),
    ],
  );
}
