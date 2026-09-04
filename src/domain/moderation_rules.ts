import { caseFold } from "unicode-case-folding";
import type { NormalizedMessage } from "../core/models.ts";

export interface ModerationRule {
  readonly id: number;
  readonly name: string;
  readonly ruleType: string;
  readonly pattern: string;
  readonly severity: string;
  readonly autoEnforceAction: string | null;
  readonly enabled: boolean;
  readonly enforcementMode: string;
  readonly actionDurationSeconds: number;
}

export interface ModerationFinding {
  readonly ruleId: number;
  readonly ruleName: string;
  readonly ruleType: string;
  readonly severity: string;
  readonly reasonCode: string;
  readonly autoEnforceAction: string | null;
  readonly enforcementMode: string;
  readonly actionDurationSeconds: number;
}

const EGREGIOUS_TERMS = new Set([
  "alligatorbait",
  "gatorbait",
  "beaner",
  "bohunk",
  "boong",
  "boonga",
  "boonie",
  "bountybar",
  "cameljockey",
  "chink",
  "chinky",
  "coon",
  "coondog",
  "dago",
  "darkie",
  "darky",
  "datnigga",
  "faggot",
  "fagot",
  "fag",
  "gook",
  "greaseball",
  "hebe",
  "heeb",
  "honkey",
  "honky",
  "hymie",
  "ikey",
  "jap",
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
  "kyke",
  "kkk",
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

const WORD_CHARACTER = "\\p{L}\\p{N}_";
const LINK_PATTERN = /https?:\/\/|www\./iu;
const STREAMBOO_BRAND_PATTERN =
  /s[^\p{L}\p{N}]*t[^\p{L}\p{N}]*r[^\p{L}\p{N}]*e[^\p{L}\p{N}]*a[^\p{L}\p{N}]*m[^\p{L}\p{N}]*b[^\p{L}\p{N}]*(?:o|0)[^\p{L}\p{N}]*(?:o|0)/iu;
const STREAMBOO_SOLICITATION_PATTERN =
  /(?<![\p{L}\p{N}_])(?:viewers?|followers?|promotion|promot(?:e|ion)|boost(?:ing)?|audience|engagement)(?![\p{L}\p{N}_])/iu;

export function evaluateMessageModeration(
  message: NormalizedMessage,
  rules: Iterable<ModerationRule>,
): readonly ModerationFinding[] {
  const findings: ModerationFinding[] = [];
  for (const rule of rules) {
    if (!rule.enabled || rule.enforcementMode === "disabled") continue;
    let matched = false;
    switch (rule.ruleType) {
      case "exact_term":
        matched = matchesExactTerm(message.contentRaw, rule.pattern);
        break;
      case "banned_phrase":
        matched = matchesPhrase(message.contentRaw, rule.pattern);
        break;
      case "streamboo_viewer_spam":
        matched = !message.isModerator &&
          containsStreambooViewerSpam(message.contentRaw);
        break;
      case "link_restriction":
        matched = !message.isModerator && LINK_PATTERN.test(message.contentRaw);
        break;
      case "duplicate_message":
        matched = matchesDuplicate(message, rule.pattern);
        break;
    }
    if (matched) findings.push(buildFinding(rule));
  }
  return Object.freeze(findings);
}

export function evaluateEgregiousContent(
  message: NormalizedMessage,
  rule: ModerationRule,
): readonly ModerationFinding[] {
  if (
    message.isModerator || !rule.enabled ||
    rule.enforcementMode === "disabled" ||
    !isEgregiousContent(message.contentRaw)
  ) return [];
  return Object.freeze([buildFinding(rule)]);
}

export function containsStreambooViewerSpam(content: string): boolean {
  const normalized = caseFold(content.normalize("NFKC")).replace(
    /\p{Cf}/gu,
    "",
  );
  return STREAMBOO_BRAND_PATTERN.test(normalized) &&
    STREAMBOO_SOLICITATION_PATTERN.test(normalized);
}

export function isEgregiousContent(content: string): boolean {
  const normalized = caseFold(content).trim();
  return [...EGREGIOUS_TERMS].some((term) =>
    new RegExp(
      `(?<![${WORD_CHARACTER}])${escapeRegExp(term)}(?![${WORD_CHARACTER}])`,
      "u",
    ).test(normalized)
  );
}

function buildFinding(rule: ModerationRule): ModerationFinding {
  return Object.freeze({
    ruleId: rule.id,
    ruleName: rule.name,
    ruleType: rule.ruleType,
    severity: rule.severity,
    reasonCode: rule.ruleType,
    autoEnforceAction: rule.autoEnforceAction,
    enforcementMode: rule.enforcementMode,
    actionDurationSeconds: rule.actionDurationSeconds,
  });
}

function matchesExactTerm(content: string, pattern: string): boolean {
  const normalizedPattern = caseFold(pattern.trim());
  if (!normalizedPattern) return false;
  return new RegExp(
    `(?<![${WORD_CHARACTER}])${
      escapeRegExp(normalizedPattern)
    }(?![${WORD_CHARACTER}])`,
    "u",
  ).test(caseFold(content));
}

function matchesPhrase(content: string, pattern: string): boolean {
  const expression = pattern.trim();
  if (!expression) return false;
  try {
    return new RegExp(expression, "iu").test(content);
  } catch {
    return false;
  }
}

function matchesDuplicate(
  message: NormalizedMessage,
  pattern: string,
): boolean {
  if (caseFold(pattern.trim()) !== "same_user_same_content") return false;
  const previous = message.metadata.previous_normalized_content;
  return typeof previous === "string" &&
    caseFold(previous) === caseFold(message.contentNormalized);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
