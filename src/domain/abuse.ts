import { caseFold } from "unicode-case-folding";

export type AntiAbuseEnforcementMode = "shadow" | "enforce";

export interface AntiAbusePolicy {
  enabled: boolean;
  enforcementMode: AntiAbuseEnforcementMode;
  messageBurstLimit: number;
  messageBurstWindowSeconds: number;
  mentionLimit: number;
  joinRaidLimit: number;
  joinRaidWindowSeconds: number;
}

export interface AbuseFinding {
  reasonCode: "message_flood" | "mention_spam" | "join_raid";
  title: string;
  severity: "high" | "critical";
  windowSeconds: number;
  dedupeKey: string;
  enforcementRequired: boolean;
}

export function validateAntiAbusePolicy(input: {
  enabled: boolean;
  enforcementMode: string;
  messageBurstLimit: number;
  messageBurstWindowSeconds: number;
  mentionLimit: number;
  joinRaidLimit: number;
  joinRaidWindowSeconds: number;
}): AntiAbusePolicy {
  const enforcementMode = caseFold(input.enforcementMode.trim());
  if (enforcementMode !== "shadow" && enforcementMode !== "enforce") {
    throw new TypeError(
      "anti-abuse enforcement mode must be shadow or enforce",
    );
  }
  const bounds = {
    messageBurstLimit: [Math.trunc(input.messageBurstLimit), 2, 100],
    messageBurstWindowSeconds: [
      Math.trunc(input.messageBurstWindowSeconds),
      1,
      300,
    ],
    mentionLimit: [Math.trunc(input.mentionLimit), 1, 100],
    joinRaidLimit: [Math.trunc(input.joinRaidLimit), 2, 1000],
    joinRaidWindowSeconds: [Math.trunc(input.joinRaidWindowSeconds), 1, 3600],
  } as const;
  for (const [name, [value, minimum, maximum]] of Object.entries(bounds)) {
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw new TypeError(
        `${snakeCase(name)} must be between ${minimum} and ${maximum}`,
      );
    }
  }
  return Object.freeze({
    enabled: Boolean(input.enabled),
    enforcementMode,
    messageBurstLimit: bounds.messageBurstLimit[0],
    messageBurstWindowSeconds: bounds.messageBurstWindowSeconds[0],
    mentionLimit: bounds.mentionLimit[0],
    joinRaidLimit: bounds.joinRaidLimit[0],
    joinRaidWindowSeconds: bounds.joinRaidWindowSeconds[0],
  });
}

export function calculateMessageAbuseFindings(
  policy: AntiAbusePolicy,
  input: {
    communityId: number;
    platformAccountId: number;
    occurredAt: string;
    recentMessageCount: number;
    mentionCount: number;
    isModerator: boolean;
  },
): readonly AbuseFinding[] {
  if (!policy.enabled || input.isModerator) return Object.freeze([]);
  const candidates: Array<readonly ["message_flood" | "mention_spam", string]> =
    [];
  if (input.recentMessageCount >= policy.messageBurstLimit) {
    candidates.push(["message_flood", "Message flood detected"]);
  }
  if (input.mentionCount >= policy.mentionLimit) {
    candidates.push(["mention_spam", "Mention spam detected"]);
  }
  return Object.freeze(candidates.map(([reasonCode, title]) =>
    Object.freeze({
      reasonCode,
      title,
      severity: "high" as const,
      windowSeconds: policy.messageBurstWindowSeconds,
      dedupeKey: antiAbuseWindowKey(
        reasonCode,
        input.communityId,
        input.platformAccountId,
        input.occurredAt,
        policy.messageBurstWindowSeconds,
      ),
      enforcementRequired: policy.enforcementMode === "enforce",
    })
  ));
}

export function calculateJoinRaidFinding(
  policy: AntiAbusePolicy,
  input: { communityId: number; occurredAt: string; joinCount: number },
): AbuseFinding | null {
  if (!policy.enabled || input.joinCount < policy.joinRaidLimit) return null;
  return Object.freeze({
    reasonCode: "join_raid",
    title: "Join raid detected",
    severity: "critical",
    windowSeconds: policy.joinRaidWindowSeconds,
    dedupeKey: antiAbuseWindowKey(
      "join_raid",
      input.communityId,
      0,
      input.occurredAt,
      policy.joinRaidWindowSeconds,
    ),
    enforcementRequired: false,
  });
}

export function antiAbuseWindowKey(
  reasonCode: string,
  communityId: number,
  subjectId: number,
  occurredAt: string,
  windowSeconds: number,
): string {
  const timestamp = parseTimestampSeconds(occurredAt);
  const bucket = Math.floor(
    Math.trunc(timestamp) / Math.max(1, Math.trunc(windowSeconds)),
  );
  return `anti-abuse:${communityId}:${reasonCode}:${subjectId}:${bucket}`;
}

function parseTimestampSeconds(value: string): number {
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/u.test(value) ? value : `${value}Z`;
  const timestamp = Date.parse(normalized);
  if (!Number.isFinite(timestamp)) {
    throw new TypeError(`invalid timestamp: ${value}`);
  }
  return timestamp / 1000;
}

function snakeCase(value: string): string {
  return value.replace(/[A-Z]/gu, (character) => `_${character.toLowerCase()}`);
}
