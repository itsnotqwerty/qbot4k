export const SIGNAL_ANALYZER_VERSION = 2;

export interface SignalEvidence {
  userId: number;
  messageCount: number;
  channelCount: number;
  platformCount: number;
  accountCount: number;
  eligibleMessageCount: number;
  positiveCount: number;
  negativeCount: number;
  negativePoints: number;
  replyCount: number;
  welcomePositiveCount: number;
  welcomeCount: number;
  welcomeDuplicateCount: number;
  findingCount: number;
  severityPoints: number;
  moderationPenaltyPoints: number;
  windowStart?: string | null;
  windowEnd?: string | null;
}

export interface DerivedSignal {
  userId: number;
  signalKey: string;
  value: number;
  confidence: number;
  evidenceCount: number;
  windowStart: string | null;
  windowEnd: string | null;
  details: Readonly<Record<string, unknown>>;
  analyzerVersion: number;
  calculatedAt: string;
}

export function calculateDerivedSignals(
  evidence: SignalEvidence,
  calculatedAt: string,
): readonly DerivedSignal[] {
  const count = evidence.messageCount;
  const positiveRatio = count ? evidence.positiveCount / count : 0;
  const negativeRatio = count ? evidence.negativeCount / count : 0;
  const moderationRate = count ? Math.min(1, evidence.findingCount / count) : 0;
  const severityRate = count ? Math.min(1, evidence.severityPoints / count) : 0;
  const riskScore = roundHalfToEven(
    Math.min(
      100,
      negativeRatio * 45 + moderationRate * 35 + severityRate * 20,
    ),
    2,
  );
  const confidence = roundHalfToEven(Math.min(1, count / 20), 4);
  const windowStart = evidence.windowStart ?? null;
  const windowEnd = evidence.windowEnd ?? null;
  return Object.freeze([
    signal(evidence, "activity.message_count", count, count, {
      unit: "messages",
    }, calculatedAt),
    signal(
      evidence,
      "activity.eligible_message_count",
      evidence.eligibleMessageCount,
      count,
      { unit: "messages", excludes: ["commands", "empty_messages"] },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "activity.active_channel_count",
      evidence.channelCount,
      count,
      { unit: "channels" },
      calculatedAt,
    ),
    signal(evidence, "activity.platform_count", evidence.platformCount, count, {
      unit: "platforms",
    }, calculatedAt),
    signal(
      evidence,
      "identity.linked_account_count",
      evidence.accountCount,
      evidence.accountCount,
      { unit: "accounts" },
      calculatedAt,
      1,
      null,
      null,
    ),
    signal(
      evidence,
      "behavior.positive_message_ratio",
      positiveRatio,
      count,
      {
        positive_messages: evidence.positiveCount,
        message_count: count,
        unit: "ratio",
      },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "behavior.negative_message_ratio",
      negativeRatio,
      count,
      {
        negative_messages: evidence.negativeCount,
        message_count: count,
        unit: "ratio",
      },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "behavior.negative_severity_points",
      evidence.negativePoints,
      evidence.negativeCount,
      { unit: "legacy_penalty_points", source: "classified_message_evidence" },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "behavior.reply_to_human_count",
      evidence.replyCount,
      evidence.replyCount,
      { unit: "replies" },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "behavior.welcome_count",
      evidence.welcomePositiveCount,
      evidence.welcomeCount,
      { unit: "welcome_events", all_welcome_events: evidence.welcomeCount },
      calculatedAt,
    ),
    signal(
      evidence,
      "behavior.welcome_duplicate_count",
      evidence.welcomeDuplicateCount,
      evidence.welcomeDuplicateCount,
      { unit: "duplicate_welcome_events" },
      calculatedAt,
      confidence,
    ),
    signal(evidence, "moderation.finding_count", evidence.findingCount, count, {
      unit: "findings",
    }, calculatedAt),
    signal(
      evidence,
      "moderation.penalty_points",
      evidence.moderationPenaltyPoints,
      evidence.findingCount,
      { unit: "legacy_penalty_points", source: "moderation_evidence" },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "moderation.severity_index",
      severityRate,
      evidence.findingCount,
      {
        weighted_severity: roundHalfToEven(evidence.severityPoints, 4),
        message_count: count,
        unit: "ratio",
      },
      calculatedAt,
      confidence,
    ),
    signal(
      evidence,
      "risk.composite",
      riskScore,
      count,
      {
        unit: "score_0_100",
        negative_ratio: roundHalfToEven(negativeRatio, 4),
        moderation_rate: roundHalfToEven(moderationRate, 4),
        severity_rate: roundHalfToEven(severityRate, 4),
        formula: "negative_ratio*45 + moderation_rate*35 + severity_rate*20",
        independent_of_social_score: true,
      },
      calculatedAt,
      confidence,
    ),
  ]);

  function signal(
    source: SignalEvidence,
    signalKey: string,
    value: number,
    evidenceCount: number,
    details: Readonly<Record<string, unknown>>,
    timestamp: string,
    resolvedConfidence = Math.min(1, evidenceCount / 20),
    resolvedWindowStart: string | null = windowStart,
    resolvedWindowEnd: string | null = windowEnd,
  ): DerivedSignal {
    return Object.freeze({
      userId: source.userId,
      signalKey,
      value,
      confidence: roundHalfToEven(
        Math.max(0, Math.min(1, resolvedConfidence)),
        4,
      ),
      evidenceCount,
      windowStart: resolvedWindowStart,
      windowEnd: resolvedWindowEnd,
      details: Object.freeze(details),
      analyzerVersion: SIGNAL_ANALYZER_VERSION,
      calculatedAt: timestamp,
    });
  }
}

function roundHalfToEven(value: number, digits: number): number {
  const factor = 10 ** digits;
  const scaled = value * factor;
  const floor = Math.floor(scaled);
  const fraction = scaled - floor;
  if (Math.abs(fraction - 0.5) > Number.EPSILON * Math.abs(scaled) * 2) {
    return Math.round(scaled) / factor;
  }
  return (floor % 2 === 0 ? floor : floor + 1) / factor;
}
