export const CONTENT_ANALYZER_VERSION = 3;

export type ContentEntity = readonly [
  type: string,
  value: string,
  normalizedValue: string,
  confidence: number,
  startOffset: number,
  endOffset: number,
];

export interface ContentUnderstanding {
  languageCode: string;
  languageConfidence: number;
  sentimentLabel: "positive" | "negative" | "neutral";
  sentimentScore: number;
  intentLabel:
    | "threat"
    | "question"
    | "request"
    | "coordination"
    | "promotion"
    | "complaint"
    | "statement";
  intentConfidence: number;
  threatLevel: "critical" | "high" | "medium" | "none";
  threatScore: number;
  indicators: readonly string[];
  entities: readonly ContentEntity[];
  conversation: Readonly<Record<string, unknown>>;
}

const WORD_PATTERN = /[\p{L}\p{N}_'-]+/gu;
const URL_PATTERN = /https?:\/\/[^\s<>]+/giu;
const EMAIL_PATTERN = /\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b/gu;
const IP_PATTERN = /\b(?:\d{1,3}\.){3}\d{1,3}\b/gu;
const MENTION_PATTERN = /<@!?(\d+)>|(?<![\p{L}\p{N}_])@([\p{L}\p{N}_.-]+)/gu;
const HASHTAG_PATTERN = /(?<![\p{L}\p{N}_])#([\p{L}\p{N}_-]+)/gu;
const PROPER_NAME_PATTERN = /\b(?:[A-Z][a-z]{2,})(?:\s+[A-Z][a-z]{2,}){0,2}\b/g;

const LANGUAGE_WORDS = new Map<string, ReadonlySet<string>>([
  [
    "en",
    new Set([
      "the",
      "and",
      "you",
      "this",
      "that",
      "with",
      "for",
      "are",
      "not",
      "have",
    ]),
  ],
  [
    "es",
    new Set([
      "el",
      "la",
      "los",
      "las",
      "que",
      "para",
      "con",
      "una",
      "por",
      "como",
    ]),
  ],
  [
    "fr",
    new Set([
      "le",
      "la",
      "les",
      "des",
      "que",
      "pour",
      "avec",
      "une",
      "pas",
      "est",
    ]),
  ],
  [
    "de",
    new Set([
      "der",
      "die",
      "das",
      "und",
      "ist",
      "mit",
      "für",
      "nicht",
      "ein",
      "eine",
    ]),
  ],
  [
    "pt",
    new Set(["o", "a", "os", "as", "que", "para", "com", "uma", "não", "por"]),
  ],
]);
const POSITIVE = new Set([
  "good",
  "great",
  "love",
  "helpful",
  "excellent",
  "thanks",
  "safe",
  "happy",
  "win",
]);
const NEGATIVE = new Set([
  "bad",
  "hate",
  "awful",
  "angry",
  "fraud",
  "scam",
  "danger",
  "hurt",
  "kill",
  "threat",
]);
const NEGATIONS = new Set([
  "not",
  "never",
  "no",
  "isn't",
  "wasn't",
  "don't",
  "didn't",
]);

export function understandContent(
  text: string,
  attributes: Readonly<Record<string, unknown>> = {},
): ContentUnderstanding {
  const words = [...text.matchAll(WORD_PATTERN)].map((match) =>
    match[0].toLocaleLowerCase()
  );
  const [languageCode, languageConfidence] = detectLanguage(words, text);
  let sentimentTotal = 0;
  for (const [index, word] of words.entries()) {
    let polarity = POSITIVE.has(word) ? 1 : NEGATIVE.has(word) ? -1 : 0;
    if (
      polarity !== 0 &&
      words.slice(Math.max(0, index - 3), index).some((item) =>
        NEGATIONS.has(item)
      )
    ) {
      polarity *= -1;
    }
    sentimentTotal += polarity;
  }
  const rawSentiment = Math.max(
    -1,
    Math.min(1, sentimentTotal / Math.max(2, Math.sqrt(words.length))),
  );
  const sentimentScore = roundHalfToEven(rawSentiment, 4);
  const sentimentLabel = sentimentScore >= 0.2
    ? "positive"
    : sentimentScore <= -0.2
    ? "negative"
    : "neutral";

  const lowered = text.toLocaleLowerCase();
  const indicators: string[] = [];
  let threatScore = 0;
  if (
    /\b(?:i|we)\s+(?:will|gonna|am going to)\s+(?:kill|hurt|attack|shoot|bomb|doxx)\b/u
      .test(lowered)
  ) {
    threatScore = 0.95;
    indicators.push("direct_future_threat");
  } else if (/\b(?:kill|shoot|bomb|attack|doxx|swat)\b/u.test(lowered)) {
    threatScore = 0.55;
    indicators.push("threat_term_in_context");
  }
  if (
    (IP_PATTERN.test(text) || EMAIL_PATTERN.test(text)) &&
    ["address", "home", "leak", "dox"].some((term) => lowered.includes(term))
  ) {
    threatScore = Math.max(threatScore, 0.75);
    indicators.push("possible_personal_data_exposure");
  }
  IP_PATTERN.lastIndex = 0;
  EMAIL_PATTERN.lastIndex = 0;
  const threatLevel = threatScore >= 0.9
    ? "critical"
    : threatScore >= 0.7
    ? "high"
    : threatScore >= 0.4
    ? "medium"
    : "none";

  let intentLabel: ContentUnderstanding["intentLabel"];
  let intentConfidence: number;
  if (threatScore >= 0.7) {
    [intentLabel, intentConfidence] = ["threat", threatScore];
  } else if (text.trimEnd().endsWith("?")) {
    [intentLabel, intentConfidence] = ["question", 0.85];
  } else if (/\b(?:please|could you|can you|need you to)\b/u.test(lowered)) {
    [intentLabel, intentConfidence] = ["request", 0.78];
  } else if (
    /\b(?:join|meet|coordinate|everyone|at \d{1,2}(?::\d{2})?)\b/u.test(lowered)
  ) [intentLabel, intentConfidence] = ["coordination", 0.66];
  else if (/\b(?:buy|sale|discount|promo|subscribe)\b/u.test(lowered)) {
    [intentLabel, intentConfidence] = ["promotion", 0.68];
  } else if (sentimentLabel === "negative") {
    [intentLabel, intentConfidence] = ["complaint", 0.58];
  } else [intentLabel, intentConfidence] = ["statement", 0.52];

  const entities: ContentEntity[] = [];
  for (const match of text.matchAll(URL_PATTERN)) {
    const raw = match[0];
    const value = raw.replace(/[.,;!?)]+$/u, "");
    entities.push([
      "url",
      value,
      value.toLocaleLowerCase(),
      0.99,
      match.index,
      match.index + value.length,
    ]);
    const domain = new URL(value).hostname.toLocaleLowerCase().replace(
      /^www\./u,
      "",
    );
    if (domain) {
      entities.push([
        "domain",
        domain,
        domain,
        0.99,
        match.index,
        match.index + raw.length,
      ]);
    }
  }
  appendEntities("email", EMAIL_PATTERN, 0.92);
  appendEntities("ip_address", IP_PATTERN, 0.92);
  appendEntities("hashtag", HASHTAG_PATTERN, 0.92);
  appendEntities("mention", MENTION_PATTERN, 0.92);
  for (const match of text.matchAll(PROPER_NAME_PATTERN)) {
    if (!["http", "https"].includes(match[0].toLocaleLowerCase())) {
      entities.push([
        "named_entity",
        match[0],
        match[0].toLocaleLowerCase(),
        0.62,
        match.index,
        match.index + match[0].length,
      ]);
    }
  }
  const replyId = attributes.referenced_message_id ||
    attributes.message_reference_id;
  return Object.freeze({
    languageCode,
    languageConfidence,
    sentimentLabel,
    sentimentScore,
    intentLabel,
    intentConfidence,
    threatLevel,
    threatScore,
    indicators: Object.freeze(indicators),
    entities: Object.freeze(entities),
    conversation: Object.freeze({
      is_question: text.trimEnd().endsWith("?"),
      reply_to: replyId ? String(replyId) : null,
      thread_id: attributes.thread_id ?? null,
      mentioned_accounts: entities.filter((item) => item[0] === "mention").map((
        item,
      ) => item[1]),
    }),
  });

  function appendEntities(
    type: string,
    pattern: RegExp,
    confidence: number,
  ): void {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      const value = match.slice(1).find((group) => group !== undefined) ??
        match[0];
      entities.push([
        type,
        value,
        value.toLocaleLowerCase(),
        confidence,
        match.index,
        match.index + match[0].length,
      ]);
    }
  }
}

function detectLanguage(
  words: readonly string[],
  text: string,
): readonly [string, number] {
  if (words.length === 0) return ["und", 0];
  if (/[\u0400-\u04ff]/u.test(text)) return ["ru", 0.9];
  if (/[\u4e00-\u9fff]/u.test(text)) return ["zh", 0.9];
  let best = "en";
  let hits = -1;
  for (const [code, vocabulary] of LANGUAGE_WORDS) {
    const score = words.filter((word) => vocabulary.has(word)).length;
    if (score > hits) [best, hits] = [code, score];
  }
  if (hits === 0) {
    return [...text].every((character) => character.codePointAt(0)! < 128)
      ? ["en", 0.35]
      : ["und", 0.2];
  }
  return [best, Math.min(0.98, 0.45 + hits / Math.max(2, words.length))];
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
