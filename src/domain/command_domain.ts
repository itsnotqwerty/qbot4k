import { caseFold } from "unicode-case-folding";

export const RESERVED_COMMAND_NAMES = new Set([
  "addcom",
  "delcom",
  "editcom",
  "alias",
  "verify",
]);

export interface CommandField {
  readonly name: string;
  readonly value: string;
  readonly inline?: boolean;
}

export interface CommandCard {
  readonly title: string;
  readonly description: string;
  readonly fields?: readonly CommandField[];
  readonly footer?: string | null;
  readonly color?: number | null;
}

export interface CommandReply {
  readonly card: CommandCard;
  readonly textOnly?: boolean;
}

export type ParsedCommand = readonly [
  name: string,
  arguments_: readonly string[],
];

export type CommandTemplateValues = Readonly<Record<string, unknown>>;
export type RandomInteger = (lowerBound: number, upperBound: number) => number;
export type HttpTemplateResponse = (
  method: string,
  url: string,
) => string | null;

const RANDOM_RANGE_PATTERN =
  /\$\{(-?\d+|\$?\{query\}|query)\.\.(-?\d+|\$?\{query\}|query)\}/gu;
const HTTP_TEMPLATE_CALL_PATTERN =
  /\$\{(GET|POST|PUT|DELETE)\}\((https?:\/\/[^\s)]+)\)(?:\[([^\]]+)\])?/giu;
const HTTP_SELECTOR_ALIAS_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/u;
const SANITIZED_RANGE_MIN = -1_000_000;
const SANITIZED_RANGE_MAX = 1_000_000;

export function parseCommand(
  content: string,
  prefix = "!",
): ParsedCommand | null {
  const normalized = content.trim();
  if (!normalized.startsWith(prefix)) return null;
  const body = normalized.slice(prefix.length).trim();
  if (!body) return null;
  const [name, ...arguments_] = body.split(/\s+/u);
  return Object.freeze([caseFold(name), Object.freeze(arguments_)]);
}

export function normalizeCustomCommandName(rawName: string): string {
  const name = caseFold(rawName.trim().replace(/^!+/, ""));
  return RESERVED_COMMAND_NAMES.has(name) || name === "credit" ? "" : name;
}

export function formatCommandTemplate(
  template: string,
  values: CommandTemplateValues = {},
  randomInteger: RandomInteger = secureRandomInteger,
  httpResponse: HttpTemplateResponse = () => null,
): string {
  const mutableValues = { ...values };
  const rangeResolved = template.replace(
    RANDOM_RANGE_PATTERN,
    (_match, rawLower: string, rawUpper: string) => {
      let lowerBound = resolveRangeBound(rawLower, values);
      let upperBound = resolveRangeBound(rawUpper, values);
      if (lowerBound > upperBound) {
        [lowerBound, upperBound] = [upperBound, lowerBound];
      }
      return String(randomInteger(lowerBound, upperBound));
    },
  );
  const resolved = resolveHttpTemplateCalls(
    rangeResolved,
    mutableValues,
    httpResponse,
  );
  try {
    return formatValues(resolved, mutableValues);
  } catch {
    return resolved;
  }
}

export function resolveHttpTemplateCalls(
  template: string,
  values: Record<string, unknown>,
  response: HttpTemplateResponse,
): string {
  const cache = new Map<string, string | null>();
  return template.replace(
    HTTP_TEMPLATE_CALL_PATTERN,
    (_match, rawMethod: string, rawUrl: string, rawSelector?: string) => {
      const method = rawMethod.toUpperCase();
      const url = substituteHttpUrlTemplate(rawUrl, values);
      const selectorSpec = (rawSelector ?? "").trim();
      const selectors = selectorSpec ? parseHttpSelectorSpec(selectorSpec) : [];
      for (const selector of selectors) {
        if (!selector.includes(":")) continue;
        const alias = selector.split(":", 1)[0].trim();
        if (alias && HTTP_SELECTOR_ALIAS_PATTERN.test(alias)) {
          values[alias] ??= `\${${alias}}`;
        }
      }
      const cacheKey = `${method}\u0000${url}`;
      let body = cache.get(cacheKey);
      if (body === undefined) {
        body = response(method, url);
        if (body !== null) cache.set(cacheKey, body);
      }
      if (body === null || body === undefined) return "";
      const decodedBody = body.trim();
      if (!selectorSpec) return escapeFormatBraces(decodedBody);
      if (selectors.length === 0) return "";
      let payload: unknown;
      try {
        payload = JSON.parse(decodedBody);
      } catch {
        return "";
      }
      const inlineValues: string[] = [];
      for (const selector of selectors) {
        const separator = selector.indexOf(":");
        if (separator >= 0) {
          const alias = selector.slice(0, separator).trim();
          const path = selector.slice(separator + 1).trim();
          if (!alias || !path || !HTTP_SELECTOR_ALIAS_PATTERN.test(alias)) {
            continue;
          }
          const extracted = extractJsonPathValue(payload, path);
          if (extracted !== null) {
            values[alias] = renderExtractedJsonValue(extracted);
          }
          continue;
        }
        const extracted = extractJsonPathValue(payload, selector.trim());
        if (extracted !== null) {
          inlineValues.push(renderExtractedJsonValue(extracted));
        }
      }
      return inlineValues.length > 0
        ? escapeFormatBraces(inlineValues.join(" "))
        : "";
    },
  );
}

export function parseHttpSelectorSpec(selectorSpec: string): readonly string[] {
  const trimmed = selectorSpec.trim();
  if (!trimmed) return Object.freeze([]);
  const mappingPattern =
    /\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^,;]+)\s*(?:[,;]|$)/gy;
  const mapped: string[] = [];
  let position = 0;
  while (position < trimmed.length) {
    mappingPattern.lastIndex = position;
    const match = mappingPattern.exec(trimmed);
    if (!match || match.index !== position) break;
    mapped.push(`${match[1].trim()}:${match[2].trim()}`);
    position = mappingPattern.lastIndex;
  }
  if (mapped.length > 0 && position === trimmed.length) {
    return Object.freeze(mapped);
  }
  return Object.freeze(
    trimmed.split(/[,;]/u).map((item) => item.trim()).filter(Boolean),
  );
}

export function substituteHttpUrlTemplate(
  urlTemplate: string,
  values: CommandTemplateValues,
): string {
  const query = String(values.query ?? "").trim();
  return urlTemplate.replaceAll(
    "${query}",
    encodeURIComponent(query).replaceAll("%20", "+"),
  );
}

export function extractJsonPathValue(
  payload: unknown,
  path: string,
): unknown | null {
  const segments = path.split(".").map((segment) => segment.trim()).filter(
    Boolean,
  );
  if (segments.length === 0) return null;
  let current = payload;
  for (const segment of segments) {
    if (Array.isArray(current) && /^\d+$/u.test(segment)) {
      const index = Number.parseInt(segment, 10);
      if (index >= current.length) return null;
      current = current[index];
    } else if (
      current !== null && typeof current === "object" &&
      !Array.isArray(current) && segment in current
    ) {
      current = (current as Record<string, unknown>)[segment];
    } else return null;
  }
  return current ?? null;
}

export function renderExtractedJsonValue(value: unknown): string {
  if (value === null) return "";
  if (typeof value === "string") return value;
  if (["boolean", "number", "object"].includes(typeof value)) {
    return asciiJson(value);
  }
  return String(value);
}

export function renderCommandReply(
  reply: CommandReply,
  platform: string,
): Readonly<Record<string, unknown>> | string {
  const platformName = caseFold(platform).trim();
  if (platformName === "discord") return renderDiscordReply(reply);
  if (platformName === "twitch") return renderPlaintextReply(reply);
  throw new TypeError(`unsupported command platform: ${platform}`);
}

function renderDiscordReply(
  reply: CommandReply,
): Readonly<Record<string, unknown>> {
  if (reply.textOnly) {
    return Object.freeze({
      allowed_mentions: Object.freeze({ parse: Object.freeze([]) }),
      content: reply.card.description,
    });
  }
  const embed: Record<string, unknown> = {
    title: reply.card.title,
    description: reply.card.description,
    color: reply.card.color ?? null,
    fields: (reply.card.fields ?? []).map((field) => ({
      name: field.name,
      value: field.value,
      inline: field.inline ?? false,
    })),
  };
  if (reply.card.footer !== null && reply.card.footer !== undefined) {
    embed.footer = { text: reply.card.footer };
  }
  return Object.freeze({
    allowed_mentions: Object.freeze({ parse: Object.freeze([]) }),
    embeds: Object.freeze([Object.freeze(embed)]),
  });
}

function renderPlaintextReply(reply: CommandReply): string {
  const fields = reply.card.fields ?? [];
  if (reply.textOnly || (fields.length === 0 && reply.card.footer == null)) {
    return reply.card.description;
  }
  return [
    reply.card.title,
    reply.card.description,
    ...fields.map((field) => `${field.name}: ${field.value}`),
    reply.card.footer,
  ].filter((part): part is string => Boolean(part)).join(" | ");
}

function resolveRangeBound(
  rawBound: string,
  values: CommandTemplateValues,
): number {
  const normalized = rawBound.trim().replace(/^\$?\{(.+)\}$/u, "$1");
  if (normalized === "query") {
    const match = String(values.query ?? "").match(/-?\d+/u);
    return clampRange(match ? Number.parseInt(match[0], 10) : 0);
  }
  return clampRange(Number.parseInt(normalized, 10));
}

function clampRange(value: number): number {
  return Math.max(SANITIZED_RANGE_MIN, Math.min(SANITIZED_RANGE_MAX, value));
}

function escapeFormatBraces(value: string): string {
  return value.replaceAll("{", "{{").replaceAll("}", "}}");
}

function asciiJson(value: unknown): string {
  return JSON.stringify(value).replace(
    /[\u007f-\uffff]/gu,
    (character) =>
      [...character].map((part) => {
        const codePoint = part.codePointAt(0)!;
        if (codePoint <= 0xffff) {
          return `\\u${codePoint.toString(16).padStart(4, "0")}`;
        }
        const adjusted = codePoint - 0x10000;
        const high = 0xd800 + (adjusted >> 10);
        const low = 0xdc00 + (adjusted & 0x3ff);
        return `\\u${high.toString(16)}\\u${low.toString(16)}`;
      }).join(""),
  );
}

function formatValues(
  template: string,
  values: CommandTemplateValues,
): string {
  const escapedLeft = "\u0000";
  const escapedRight = "\u0001";
  const protectedTemplate = template.replaceAll("{{", escapedLeft).replaceAll(
    "}}",
    escapedRight,
  );
  const formatted = protectedTemplate.replace(
    /\$\{([^{}]+)\}/gu,
    (_match, key: string) => {
      if (!(key in values)) {
        throw new TypeError(
          `missing template value: ${key}`,
        );
      }
      return String(values[key]);
    },
  );
  if (/\$\{[^}]*$/u.test(formatted)) {
    throw new TypeError("invalid command template");
  }
  return formatted.replaceAll(escapedLeft, "{").replaceAll(escapedRight, "}");
}

function secureRandomInteger(lowerBound: number, upperBound: number): number {
  const width = upperBound - lowerBound + 1;
  if (width <= 0 || width > 2_000_001) {
    throw new TypeError("invalid command template range");
  }
  const limit = Math.floor(0x1_0000_0000 / width) * width;
  const sample = new Uint32Array(1);
  do crypto.getRandomValues(sample); while (sample[0] >= limit);
  return lowerBound + (sample[0] % width);
}
