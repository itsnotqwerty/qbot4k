import { caseFold } from "unicode-case-folding";

export interface CampaignObservation {
  observationId: number;
  text: string;
  userId: number | null;
}

export interface CampaignMember {
  observationId: number;
  userId: number | null;
  similarity: number;
}

export interface CoordinationCampaign {
  campaignKey: string;
  campaignType: "near_duplicate";
  severity: "medium" | "high";
  messageCount: number;
  actorCount: number;
  confidence: number;
  domains: readonly string[];
  tokens: readonly string[];
  members: readonly CampaignMember[];
}

const TOKEN_PATTERN = /[a-z0-9]{3,}/gu;
const URL_PATTERN = /https?:\/\/[^\s]+/giu;

export function campaignFeatures(text: string): {
  tokens: ReadonlySet<string>;
  domains: ReadonlySet<string>;
} {
  const normalized = caseFold(text);
  const tokens = new Set(
    [...normalized.matchAll(TOKEN_PATTERN)].map((match) => match[0]),
  );
  const domains = new Set<string>();
  for (const match of text.matchAll(URL_PATTERN)) {
    try {
      const hostname = caseFold(new URL(match[0]).hostname).replace(
        /^www\./u,
        "",
      );
      if (hostname) domains.add(hostname);
    } catch {
      // Python urlparse likewise contributes no hostname for malformed URLs.
    }
  }
  return Object.freeze({ tokens, domains });
}

export async function calculateCoordinationCampaign(
  current: CampaignObservation,
  candidates: readonly CampaignObservation[],
): Promise<CoordinationCampaign | null> {
  const { tokens, domains } = campaignFeatures(current.text);
  if (tokens.size < 4 && domains.size === 0) return null;
  const members: CampaignMember[] = [{
    observationId: current.observationId,
    userId: current.userId,
    similarity: 1,
  }];
  for (const candidate of candidates) {
    const other = campaignFeatures(candidate.text);
    const union = new Set([...tokens, ...other.tokens]);
    let similarity = union.size === 0
      ? 0
      : [...tokens].filter((token) => other.tokens.has(token)).length /
        union.size;
    if ([...domains].some((domain) => other.domains.has(domain))) {
      similarity = Math.max(similarity, 0.75);
    }
    if (similarity >= 0.72) {
      members.push({
        observationId: candidate.observationId,
        userId: candidate.userId,
        similarity,
      });
    }
  }
  const actors = new Set(
    members.flatMap((member) => member.userId === null ? [] : [member.userId]),
  );
  if (members.length < 3 || actors.size < 2) return null;
  const sortedDomains = [...domains].sort();
  const sortedTokens = [...tokens].sort();
  const keyMaterial =
    (sortedDomains.length > 0 ? sortedDomains : sortedTokens.slice(0, 12))
      .join("|");
  return Object.freeze({
    campaignKey: (await sha256(keyMaterial)).slice(0, 24),
    campaignType: "near_duplicate",
    severity: actors.size >= 5 || members.length >= 10 ? "high" : "medium",
    messageCount: members.length,
    actorCount: actors.size,
    confidence: Math.min(
      0.99,
      0.55 + members.length * 0.06 + actors.size * 0.04,
    ),
    domains: Object.freeze(sortedDomains),
    tokens: Object.freeze(sortedTokens.slice(0, 20)),
    members: Object.freeze(members.map((member) => Object.freeze(member))),
  });
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
