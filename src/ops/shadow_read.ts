import type { DatabaseConnection } from "../data/database.ts";

export interface ShadowComparison {
  readonly method: string;
  readonly path: string;
  readonly matched: boolean;
  readonly primaryStatus: number;
  readonly upstreamStatus: number;
  readonly primaryContentType: string;
  readonly upstreamContentType: string;
  readonly primaryLatencyMs: number;
  readonly upstreamLatencyMs: number;
  readonly comparedAt: string;
}

export interface ShadowComparisonStore {
  record(comparison: ShadowComparison): Promise<void>;
}

export class PostgresShadowComparisonStore implements ShadowComparisonStore {
  constructor(private readonly connection: DatabaseConnection) {}

  async record(comparison: ShadowComparison): Promise<void> {
    const dimension = JSON.stringify({
      method: comparison.method,
      path: comparison.path,
      primary_status: comparison.primaryStatus,
      upstream_status: comparison.upstreamStatus,
      primary_content_type: comparison.primaryContentType,
      upstream_content_type: comparison.upstreamContentType,
    });
    for (
      const [metric, value] of [
        ["shadow_read.matched", comparison.matched ? 1 : 0],
        ["shadow_read.primary_latency_ms", comparison.primaryLatencyMs],
        ["shadow_read.upstream_latency_ms", comparison.upstreamLatencyMs],
      ] as const
    ) {
      await this.connection.query(
        `INSERT INTO operational_metrics(metric_name,dimension_key,value,observed_at)
         VALUES ($1,$2,$3,$4)`,
        [metric, dimension, value, comparison.comparedAt],
      );
    }
  }
}

export function createShadowReadHandler(
  primary: (request: Request) => Response | Promise<Response>,
  upstreamUrl: string,
  store: ShadowComparisonStore,
  fetcher: typeof fetch = fetch,
  now: () => number = performance.now.bind(performance),
): (request: Request) => Promise<Response> {
  const upstream = new URL(upstreamUrl);
  return async (request) => {
    if (!new Set(["GET", "HEAD"]).has(request.method)) {
      return await primary(request);
    }
    const upstreamRequest = mirrorRequest(request, upstream);
    const primaryStarted = now();
    const primaryPromise = Promise.resolve(primary(request));
    const upstreamStarted = now();
    const upstreamPromise = fetcher(upstreamRequest);
    const primaryResponse = await primaryPromise;
    const primaryLatencyMs = now() - primaryStarted;
    try {
      const upstreamResponse = await upstreamPromise;
      const upstreamLatencyMs = now() - upstreamStarted;
      const [primaryBody, upstreamBody] = await Promise.all([
        normalizedBody(primaryResponse.clone()),
        normalizedBody(upstreamResponse.clone()),
      ]);
      const primaryContentType = mediaType(primaryResponse);
      const upstreamContentType = mediaType(upstreamResponse);
      await store.record(Object.freeze({
        method: request.method,
        path: new URL(request.url).pathname,
        matched: primaryResponse.status === upstreamResponse.status &&
          primaryContentType === upstreamContentType &&
          primaryBody === upstreamBody,
        primaryStatus: primaryResponse.status,
        upstreamStatus: upstreamResponse.status,
        primaryContentType,
        upstreamContentType,
        primaryLatencyMs,
        upstreamLatencyMs,
        comparedAt: new Date().toISOString(),
      }));
    } catch {
      // Shadow failures never alter the primary response.
    }
    return primaryResponse;
  };
}

function mirrorRequest(request: Request, upstream: URL): Request {
  const source = new URL(request.url);
  const target = new URL(source.pathname + source.search, upstream);
  const headers = new Headers();
  for (const name of ["accept", "accept-language", "cookie", "user-agent"]) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  headers.set("x-qbot-shadow-read", "1");
  return new Request(target, { method: request.method, headers });
}

async function normalizedBody(response: Response): Promise<string> {
  if (response.status === 204 || response.status === 304) return "";
  const body = await response.text();
  if (mediaType(response) === "application/json") {
    try {
      return JSON.stringify(sortJson(JSON.parse(body)));
    } catch {
      return body.trim();
    }
  }
  if (mediaType(response) === "text/html") {
    return body.replace(/\s+/gu, " ").trim();
  }
  return body;
}

function mediaType(response: Response): string {
  return (response.headers.get("content-type") ?? "")
    .split(";", 1)[0].trim().toLocaleLowerCase();
}

function sortJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJson);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, entry]) => [key, sortJson(entry)]),
    );
  }
  return value;
}
