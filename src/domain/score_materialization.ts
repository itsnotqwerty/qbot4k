import type { DatabaseConnection } from "../data/database.ts";
import {
  calculateTemporalRisk,
  clampSocialScore,
  isPoweruserScore,
  SOCIAL_SCORE_DEFAULT,
} from "./scoring.ts";

export const SOCIAL_SCORE_MODEL_VERSION = 2;
export const SOCIAL_SCORE_JOB_TYPE = "analysis.social_score.calculate";

interface SignalRow {
  signal_key: string;
  value_real: number;
  confidence: number;
  evidence_count: number;
}

interface ScoreComponent {
  readonly key: string;
  readonly label: string;
  readonly rawValue: number;
  readonly normalizedValue: number;
  readonly weight: number;
  readonly confidence: number;
  readonly evidenceCount: number;
  readonly source: Readonly<Record<string, unknown>>;
}

export class PostgresSocialScoreRepository {
  constructor(private readonly connection: DatabaseConnection) {}

  async enqueue(userId: number, trigger = "evidence"): Promise<void> {
    await this.connection.query(
      `INSERT INTO processing_jobs(
         community_id,stage,job_type,payload_json,priority,idempotency_key
       ) VALUES (
         (SELECT COALESCE(MAX(community_id),1) FROM messages WHERE user_id=$1),
         'analysis',$2,$3,20,$4
       ) ON CONFLICT(idempotency_key) DO NOTHING`,
      [
        userId,
        SOCIAL_SCORE_JOB_TYPE,
        JSON.stringify({ user_id: userId, trigger }),
        `score:user:${userId}:${SOCIAL_SCORE_MODEL_VERSION}:${trigger}:${
          new Date().toISOString().slice(0, 10)
        }`,
      ],
    );
  }

  async calculate(userId: number): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const user = (await connection.query(
        "SELECT id FROM users WHERE id=$1 FOR UPDATE",
        [userId],
      ))[0];
      if (!user) return;
      const signalRows = await connection.query(
        `SELECT signal_key,value_real,confidence,evidence_count
           FROM derived_signals
          WHERE user_id=$1 AND analyzer_version=2`,
        [userId],
      );
      const signals: readonly SignalRow[] = signalRows.map((row) => ({
        signal_key: String(row.signal_key),
        value_real: Number(row.value_real),
        confidence: Number(row.confidence),
        evidence_count: Number(row.evidence_count),
      }));
      const windowRows = await connection.query(
        `SELECT window_name,value_real AS value,confidence,evidence_count
           FROM derived_signal_windows
          WHERE user_id=$1 AND signal_key='risk.composite'`,
        [userId],
      );
      const windows = windowRows.map((row) => ({
        window_name: String(row.window_name),
        value: Number(row.value),
        confidence: Number(row.confidence),
        evidence_count: Number(row.evidence_count),
      }));
      const temporalRisk = calculateTemporalRisk(windows.map((window) => ({
        windowName: window.window_name,
        value: Number(window.value),
        confidence: Number(window.confidence),
        evidenceCount: Number(window.evidence_count),
      })));
      const byKey = new Map(
        signals.map((signal) => [signal.signal_key, signal]),
      );
      const eligible = metric(byKey, "activity.eligible_message_count");
      const positiveRatio = metric(byKey, "behavior.positive_message_ratio");
      const replyCount = metric(byKey, "behavior.reply_to_human_count");
      const welcomeCount = metric(byKey, "behavior.welcome_count");
      const accountCount = metric(byKey, "identity.linked_account_count");
      const risk = metric(byKey, "risk.composite");
      const severity = metric(byKey, "moderation.severity_index");
      const moderationPenalty = metric(byKey, "moderation.penalty_points");
      const negativePoints = metric(byKey, "behavior.negative_severity_points");
      const components: ScoreComponent[] = [
        component(
          "activity",
          "Verified participation",
          eligible.value,
          Math.min(25, eligible.value) / 25,
          0.24,
          eligible,
          { cap: 25, commands_excluded: true },
        ),
        component(
          "contribution",
          "Positive contribution",
          positiveRatio.value,
          positiveRatio.value,
          0.14,
          positiveRatio,
          { cap: 1 },
        ),
        component(
          "engagement",
          "Human interaction",
          replyCount.value + welcomeCount.value,
          Math.min(20, replyCount.value + welcomeCount.value) / 20,
          0.12,
          replyCount,
          { cap: 20 },
        ),
        component(
          "identity",
          "Linked identity",
          accountCount.value,
          Math.min(2, accountCount.value) / 2,
          0.1,
          accountCount,
          { cap: 2 },
        ),
        component(
          "risk",
          "Temporal risk",
          temporalRisk?.value ?? risk.value,
          1 - Math.min(100, temporalRisk?.value ?? risk.value) / 100,
          0.22,
          temporalRisk
            ? {
              value: temporalRisk.value,
              confidence: temporalRisk.confidence,
              evidence_count: temporalRisk.evidenceCount,
            }
            : risk,
          {
            windows: temporalRisk?.windowValues ?? {},
            prior_score_input: false,
          },
        ),
        component(
          "moderation",
          "Moderation history",
          moderationPenalty.value + negativePoints.value + severity.value,
          1 - Math.min(1, moderationPenalty.value / 100 + severity.value),
          0.18,
          moderationPenalty,
          { penalty_points_capped_at: 100 },
        ),
      ];
      const evidenceCount = components.reduce(
        (total, item) => total + item.evidenceCount,
        0,
      );
      const confidence = round(
        Math.min(1, evidenceCount / 50),
        4,
      );
      const contribution = components.reduce(
        (total, item) => total + item.normalizedValue * item.weight,
        0,
      );
      const score = clampSocialScore(
        Math.round(SOCIAL_SCORE_DEFAULT - 75 + contribution * 200),
      );
      const calculatedAt = new Date().toISOString();
      const run = (await connection.query(
        `INSERT INTO social_score_runs(
           user_id,model_version,score,confidence,evidence_count,band,
           explanation_json,calculated_at
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id`,
        [
          userId,
          SOCIAL_SCORE_MODEL_VERSION,
          score,
          confidence,
          evidenceCount,
          isPoweruserScore(score) && confidence >= 0.5
            ? "poweruser"
            : score >= 650
            ? "trusted"
            : score >= 500
            ? "standard"
            : "elevated_risk",
          JSON.stringify({
            model: "social-score-v2",
            prior_score_input: false,
            poweruser_requires_confidence: true,
          }),
          calculatedAt,
        ],
      ))[0];
      const runId = Number(run.id);
      for (const item of components) {
        await connection.query(
          `INSERT INTO social_score_components(
             score_run_id,component_key,label,raw_value,normalized_value,
             weight,contribution,confidence,evidence_count,source_json
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
          [
            runId,
            item.key,
            item.label,
            item.rawValue,
            item.normalizedValue,
            item.weight,
            item.normalizedValue * item.weight,
            item.confidence,
            item.evidenceCount,
            JSON.stringify(item.source),
          ],
        );
      }
      await connection.query(
        `UPDATE users SET
           current_reputation_score=$2,
           candidate_flag=$3,
           score_confidence=$4,
           score_model_version=$5,
           score_calculated_at=$6,
           updated_at=CURRENT_TIMESTAMP
         WHERE id=$1`,
        [
          userId,
          score,
          isPoweruserScore(score) && confidence >= 0.5 ? 1 : 0,
          confidence,
          SOCIAL_SCORE_MODEL_VERSION,
          calculatedAt,
        ],
      );
    });
  }
}

function metric(
  signals: Map<string, SignalRow>,
  key: string,
): { value: number; confidence: number; evidence_count: number } {
  const signal = signals.get(key);
  return {
    value: Number(signal?.value_real ?? 0),
    confidence: Number(signal?.confidence ?? 0),
    evidence_count: Number(signal?.evidence_count ?? 0),
  };
}

function component(
  key: string,
  label: string,
  rawValue: number,
  normalizedValue: number,
  weight: number,
  source: { confidence: number; evidence_count: number },
  metadata: Readonly<Record<string, unknown>>,
): ScoreComponent {
  return {
    key,
    label,
    rawValue,
    normalizedValue: Math.max(0, Math.min(1, normalizedValue)),
    weight,
    confidence: Math.max(0, Math.min(1, source.confidence)),
    evidenceCount: Math.max(0, Math.trunc(source.evidence_count)),
    source: metadata,
  };
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}
