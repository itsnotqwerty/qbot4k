import type { LogLevel } from "./config.ts";

const LOG_PRIORITY: Readonly<Record<LogLevel, number>> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

export interface LogRecord {
  readonly timestamp: string;
  readonly level: LogLevel;
  readonly logger: string;
  readonly message: string;
  readonly context?: Readonly<Record<string, unknown>>;
  readonly error?: Readonly<Record<string, unknown>>;
}

export type LogSink = (record: LogRecord) => void;

const SENSITIVE_KEY =
  /(?:authorization|cookie|credential|password|passwd|secret|token|api[_-]?key)/iu;

function redactText(value: string): string {
  return value
    .replace(/\b(Bearer|Bot)\s+[^\s,;]+/giu, "$1 [REDACTED]")
    .replace(
      /\b(postgres(?:ql)?:\/\/)[^\s/@]+(?::[^\s/@]*)?@/giu,
      "$1[REDACTED]@",
    )
    .replace(
      /\b(password|passwd|secret|token|api[_-]?key)=([^\s&,;]+)/giu,
      "$1=[REDACTED]",
    );
}

function redactedValue(value: unknown, key?: string): unknown {
  if (key && SENSITIVE_KEY.test(key)) return "[REDACTED]";
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) return value.map((item) => redactedValue(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([nestedKey, nestedValue]) => [
        nestedKey,
        redactedValue(nestedValue, nestedKey),
      ]),
    );
  }
  return value;
}

function serializedError(error: unknown): Readonly<Record<string, unknown>> {
  if (error instanceof Error) {
    return Object.freeze({
      name: error.name,
      message: redactText(error.message),
      stack: error.stack ? redactText(error.stack) : undefined,
    });
  }
  return Object.freeze({ name: "Error", message: redactText(String(error)) });
}

export class StructuredLogger {
  constructor(
    readonly name: string,
    readonly level: LogLevel = "INFO",
    private readonly sink: LogSink = (record) =>
      console.log(JSON.stringify(record)),
  ) {}

  log(
    level: LogLevel,
    message: string,
    context?: Readonly<Record<string, unknown>>,
    error?: unknown,
  ): void {
    if (LOG_PRIORITY[level] < LOG_PRIORITY[this.level]) return;
    this.sink(Object.freeze({
      timestamp: new Date().toISOString(),
      level,
      logger: this.name,
      message: redactText(message),
      ...(context
        ? {
          context: Object.freeze(
            redactedValue(context) as Record<string, unknown>,
          ),
        }
        : {}),
      ...(error === undefined ? {} : { error: serializedError(error) }),
    }));
  }

  debug(message: string, context?: Readonly<Record<string, unknown>>): void {
    this.log("DEBUG", message, context);
  }

  info(message: string, context?: Readonly<Record<string, unknown>>): void {
    this.log("INFO", message, context);
  }

  warning(message: string, context?: Readonly<Record<string, unknown>>): void {
    this.log("WARNING", message, context);
  }

  error(
    message: string,
    error?: unknown,
    context?: Readonly<Record<string, unknown>>,
  ): void {
    this.log("ERROR", message, context, error);
  }
}
