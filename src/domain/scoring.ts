import { caseFold } from "unicode-case-folding";

export const SOCIAL_SCORE_MIN = 350;
export const SOCIAL_SCORE_MAX = 900;
export const SOCIAL_SCORE_DEFAULT = 500;
export const POWERUSER_THRESHOLD = 700;

const POSITIVE_TERMS = new Set([
  "thanks",
  "thank you",
  "great",
  "awesome",
  "nice",
  "love",
  "good job",
  "well done",
]);

const VERY_NEGATIVE_TERMS = new Set([
  "abuse",
  "assassin",
  "assassinate",
  "assassination",
  "assault",
  "asshole",
  "assholes",
  "asswipe",
  "bastard",
  "beaner",
  "bitch",
  "bitches",
  "bohunk",
  "boong",
  "boonga",
  "boonie",
  "bullshit",
  "cameljockey",
  "chink",
  "chinky",
  "clogwog",
  "coon",
  "coondog",
  "coolie",
  "cooly",
  "cunt",
  "dago",
  "darkie",
  "darky",
  "datnigga",
  "fag",
  "faggot",
  "fagot",
  "fuck",
  "fucked",
  "fucker",
  "fuckers",
  "fucking",
  "gaymuthafuckinwhore",
  "gook",
  "greaseball",
  "gyp",
  "gypo",
  "gypp",
  "gyppie",
  "gyppo",
  "gyppy",
  "hebe",
  "heeb",
  "honkey",
  "honky",
  "hymie",
  "ikey",
  "jap",
  "japcrap",
  "jiga",
  "jigaboo",
  "jigg",
  "jigga",
  "jiggabo",
  "jigger",
  "jijjiboo",
  "junglebunny",
  "kaffer",
  "kaffir",
  "kaffre",
  "kafir",
  "kanake",
  "kigger",
  "kike",
  "kkk",
  "koon",
  "kraut",
  "kyke",
  "lynch",
  "macaca",
  "mgger",
  "mggor",
  "mooncricket",
  "mulatto",
  "munt",
  "nazi",
  "negro",
  "negroes",
  "negroid",
  "negro's",
  "nig",
  "nigg",
  "nigga",
  "niggah",
  "niggaracci",
  "niggaz",
  "nigger",
  "niggerhead",
  "niggerhole",
  "niggers",
  "nigger's",
  "niggor",
  "niggur",
  "niglet",
  "nignog",
  "nigr",
  "nigra",
  "nigre",
  "nlgger",
  "nlggor",
  "nip",
  "paki",
  "palesimian",
  "pickaninny",
  "picaninny",
  "piccaninny",
  "piker",
  "pikey",
  "piky",
  "polack",
  "porchmonkey",
  "raghead",
  "rape",
  "raped",
  "raper",
  "rapist",
  "retard",
  "retarded",
  "roundeye",
  "sandnigger",
  "slant",
  "slanteye",
  "snownigger",
  "spaghettibender",
  "spaghettinigger",
  "spic",
  "spick",
  "spig",
  "spigotty",
  "spik",
  "swastika",
  "tarbaby",
  "timbernigger",
  "towelhead",
  "wetback",
  "whigger",
  "wigger",
  "wog",
  "wop",
  "yellowman",
  "zigabo",
  "zipperhead",
]);

const TOKEN_PATTERN = /[a-z0-9']+/g;
const MIN_FUZZY_TERM_LENGTH = 5;
const MAX_FUZZY_LENGTH_DELTA = 1;
const FUZZY_MIN_SIMILARITY = 0.92;
const WORD_CHARACTER = "\\p{L}\\p{N}_";

const negativeSingleTerms = [...VERY_NEGATIVE_TERMS].filter((term) =>
  !term.includes(" ")
);
const negativePhraseTerms = [...VERY_NEGATIVE_TERMS].filter((term) =>
  term.includes(" ")
).sort();
const positiveSingleTerms = new Set(
  [...POSITIVE_TERMS].filter((term) => !term.includes(" ")),
);
const positivePhraseTerms = [...POSITIVE_TERMS].filter((term) =>
  term.includes(" ")
).sort();
const fuzzyNegativeByInitial = new Map<string, string[]>();

for (const term of [...negativeSingleTerms].sort()) {
  if (term.length < MIN_FUZZY_TERM_LENGTH || !/^\p{L}+$/u.test(term)) continue;
  const candidates = fuzzyNegativeByInitial.get(term[0]) ?? [];
  candidates.push(term);
  fuzzyNegativeByInitial.set(term[0], candidates);
}

export interface TemporalSignalWindow {
  windowName: string;
  value: number;
  confidence: number;
  evidenceCount: number;
}

export interface TemporalRisk {
  value: number;
  confidence: number;
  evidenceCount: number;
  windowValues: Readonly<Record<string, number>>;
}

const TEMPORAL_RISK_WEIGHTS: Readonly<Record<string, number>> = Object.freeze({
  "24h": 0.45,
  "7d": 0.30,
  "30d": 0.15,
  lifetime: 0.10,
});

export function calculateTemporalRisk(
  windows: readonly TemporalSignalWindow[],
): TemporalRisk | null {
  const available = windows.filter((window) =>
    window.windowName in TEMPORAL_RISK_WEIGHTS
  );
  if (available.length === 0) return null;
  const weightTotal = available.reduce(
    (total, window) => total + TEMPORAL_RISK_WEIGHTS[window.windowName],
    0,
  );
  const value = available.reduce(
    (total, window) =>
      total + window.value * TEMPORAL_RISK_WEIGHTS[window.windowName],
    0,
  ) / weightTotal;
  const confidence = available.reduce(
    (total, window) =>
      total + window.confidence * TEMPORAL_RISK_WEIGHTS[window.windowName],
    0,
  ) / weightTotal;
  return Object.freeze({
    value: roundDecimal(value, 4),
    confidence: roundDecimal(confidence, 4),
    evidenceCount: Math.max(...available.map((window) => window.evidenceCount)),
    windowValues: Object.freeze(Object.fromEntries(available.map((window) => [
      window.windowName,
      roundDecimal(window.value, 4),
    ]))),
  });
}

export type ScoreDelta = readonly [delta: number, reasonCode: string];

export function clampSocialScore(score: number): number {
  return Math.max(SOCIAL_SCORE_MIN, Math.min(SOCIAL_SCORE_MAX, score));
}

export function isPoweruserScore(score: number): boolean {
  return clampSocialScore(score) >= POWERUSER_THRESHOLD;
}

export function averageSocialScores(
  firstScore: number,
  secondScore: number,
): number {
  return clampSocialScore(roundHalfToEven((firstScore + secondScore) / 2));
}

export function defaultSocialScoreForName(_displayName: string): number {
  return SOCIAL_SCORE_DEFAULT;
}

export function enforcedSocialScoreForName(
  _displayName: string,
  proposedScore: number,
): number {
  return clampSocialScore(proposedScore);
}

export function scoreDeltaForMessage(contentRaw: string): ScoreDelta | null {
  const normalized = caseFold(contentRaw).trim();
  if (!normalized || normalized.startsWith("!") || normalized.startsWith("/")) {
    return null;
  }
  if (containsVeryNegativeContent(normalized)) {
    return [-10, "very_negative_content"];
  }
  if (containsPositiveSignal(normalized)) return [1, "positive_message"];
  return [1, "message_sent"];
}

export function scoreDeltaForModeration(input: {
  severity: string;
  actionType?: string | null;
  reasonCode?: string | null;
}): ScoreDelta {
  if (caseFold(input.reasonCode ?? "").trim() === "egregious_term") {
    return [-20, "moderation_penalty"];
  }
  const severity = caseFold(input.severity).trim();
  let delta = new Map([
    ["low", -20],
    ["medium", -35],
    ["high", -55],
  ]).get(severity) ?? -25;
  if (input.actionType) delta -= 15;
  return [delta, "moderation_penalty"];
}

function containsVeryNegativeContent(normalized: string): boolean {
  const tokens = normalized.match(TOKEN_PATTERN) ?? [];
  const tokenSet = new Set(tokens);
  if (negativeSingleTerms.some((term) => tokenSet.has(term))) return true;
  if (
    negativePhraseTerms.some((term) => containsBoundedPhrase(normalized, term))
  ) {
    return true;
  }
  return tokens.some(isFuzzyNegativeTokenMatch);
}

function containsPositiveSignal(normalized: string): boolean {
  const tokens = normalized.match(TOKEN_PATTERN) ?? [];
  if (tokens.some((token) => positiveSingleTerms.has(token))) return true;
  return positivePhraseTerms.some((term) =>
    containsBoundedPhrase(normalized, term)
  );
}

function containsBoundedPhrase(text: string, phrase: string): boolean {
  return new RegExp(
    `(?<![${WORD_CHARACTER}])${escapeRegExp(phrase)}(?![${WORD_CHARACTER}])`,
    "u",
  ).test(text);
}

function isFuzzyNegativeTokenMatch(token: string): boolean {
  if (token.length < MIN_FUZZY_TERM_LENGTH || !/^\p{L}+$/u.test(token)) {
    return false;
  }
  for (const candidate of fuzzyNegativeByInitial.get(token[0]) ?? []) {
    if (Math.abs(candidate.length - token.length) > MAX_FUZZY_LENGTH_DELTA) {
      continue;
    }
    if (token.at(-1) !== candidate.at(-1)) continue;
    if (isSingleEditApartOrEqual(token, candidate)) return true;
    if (sequenceSimilarity(token, candidate) >= FUZZY_MIN_SIMILARITY) {
      return true;
    }
  }
  return false;
}

function isSingleEditApartOrEqual(first: string, second: string): boolean {
  if (first === second) return true;
  if (Math.abs(first.length - second.length) > 1) return false;
  if (first.length > second.length) [first, second] = [second, first];
  let firstIndex = 0;
  let secondIndex = 0;
  let edits = 0;
  while (firstIndex < first.length && secondIndex < second.length) {
    if (first[firstIndex] === second[secondIndex]) {
      firstIndex += 1;
      secondIndex += 1;
      continue;
    }
    edits += 1;
    if (edits > 1) return false;
    if (first.length === second.length) firstIndex += 1;
    secondIndex += 1;
  }
  if (firstIndex < first.length || secondIndex < second.length) edits += 1;
  return edits <= 1;
}

export function sequenceSimilarity(first: string, second: string): number {
  if (!first && !second) return 1;
  if (!first || !second) return 0;
  const matching = matchingBlockSize(first, second);
  return (2 * matching) / (first.length + second.length);
}

function matchingBlockSize(first: string, second: string): number {
  if (!first || !second) return 0;
  let bestFirst = 0;
  let bestSecond = 0;
  let bestSize = 0;
  const lengths = new Map<number, number>();
  for (let firstIndex = 0; firstIndex < first.length; firstIndex += 1) {
    const next = new Map<number, number>();
    for (let secondIndex = 0; secondIndex < second.length; secondIndex += 1) {
      if (first[firstIndex] !== second[secondIndex]) continue;
      const size = (lengths.get(secondIndex - 1) ?? 0) + 1;
      next.set(secondIndex, size);
      if (size > bestSize) {
        bestFirst = firstIndex - size + 1;
        bestSecond = secondIndex - size + 1;
        bestSize = size;
      }
    }
    lengths.clear();
    for (const [index, length] of next) lengths.set(index, length);
  }
  if (bestSize === 0) return 0;
  return matchingBlockSize(
    first.slice(0, bestFirst),
    second.slice(0, bestSecond),
  ) +
    bestSize +
    matchingBlockSize(
      first.slice(bestFirst + bestSize),
      second.slice(bestSecond + bestSize),
    );
}

export function roundHalfToEven(value: number): number {
  const floor = Math.floor(value);
  if (value - floor !== 0.5) return Math.round(value);
  return floor % 2 === 0 ? floor : floor + 1;
}

export function roundDecimal(value: number, digits: number): number {
  return Number(value.toFixed(digits));
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
