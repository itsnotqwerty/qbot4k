import type { DiscordIngestionService } from "./discord_ingestion.ts";
import { decodeDiscordGatewayFrame } from "../provider_protocol.ts";

export const DISCORD_GATEWAY_INTENTS = (1 << 0) | (1 << 1) | (1 << 2) |
  (1 << 9) | (1 << 10) | (1 << 15);

export interface DiscordGatewaySocket {
  send(payload: Readonly<Record<string, unknown>>): void;
  receive(signal: AbortSignal): Promise<unknown>;
  close(): void;
}

export interface DiscordGatewayTransport {
  gatewayUrl(botToken: string): Promise<string>;
  connect(url: string): Promise<DiscordGatewaySocket>;
}

export interface DiscordGatewayHealth {
  readonly status: "idle" | "connecting" | "ready" | "reconnecting" | "down";
  readonly sessionId: string | null;
  readonly sequence: number | null;
  readonly lastError: string | null;
}

export interface DiscordInstallationHealthSink {
  ready(guildIds: readonly string[]): Promise<void>;
  failed(error: string): Promise<void>;
}

export class DiscordGatewayClient {
  private sessionId: string | null = null;
  private resumeGatewayUrl: string | null = null;
  private sequence: number | null = null;
  private status: DiscordGatewayHealth["status"] = "idle";
  private lastError: string | null = null;

  constructor(
    private readonly botToken: string,
    private readonly transport: DiscordGatewayTransport,
    private readonly ingestion: DiscordIngestionService,
    private readonly reconnectMilliseconds = 5_000,
    private readonly guildIds: readonly string[] = [],
    private readonly installationHealth?: DiscordInstallationHealthSink,
  ) {
    if (!botToken.trim()) throw new TypeError("Discord bot token is required");
  }

  async run(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      try {
        await this.connectOnce(signal);
      } catch (error) {
        if (signal.aborted) break;
        this.status = "reconnecting";
        this.lastError = errorMessage(error);
        await this.installationHealth?.failed(this.lastError);
        await abortableDelay(this.reconnectMilliseconds, signal);
      }
    }
    this.status = "down";
  }

  async connectOnce(signal: AbortSignal): Promise<void> {
    const resumable = this.sessionId !== null && this.sequence !== null &&
      this.resumeGatewayUrl !== null;
    const baseUrl = resumable
      ? this.resumeGatewayUrl!
      : await this.transport.gatewayUrl(this.token());
    const socket = await this.transport.connect(gatewayUrl(baseUrl));
    this.status = "connecting";
    try {
      const hello = decodeDiscordGatewayFrame(await socket.receive(signal));
      if (hello.kind !== "hello") {
        throw new TypeError("Discord gateway did not send hello");
      }
      socket.send(
        resumable
          ? {
            op: 6,
            d: {
              token: this.token(),
              session_id: this.sessionId,
              seq: this.sequence,
            },
          }
          : identifyPayload(this.token()),
      );
      let heartbeatDue = Date.now() + hello.heartbeatIntervalMs;
      let awaitingHeartbeat = false;

      while (!signal.aborted) {
        const received = await receiveUntil(
          socket,
          signal,
          Math.max(0, heartbeatDue - Date.now()),
        );
        if (received.timedOut) {
          if (awaitingHeartbeat) {
            throw new TypeError("Discord heartbeat was not acknowledged");
          }
          socket.send({ op: 1, d: this.sequence });
          awaitingHeartbeat = true;
          heartbeatDue = Date.now() + hello.heartbeatIntervalMs;
          continue;
        }
        const frame = decodeDiscordGatewayFrame(received.value);
        if (frame.kind === "heartbeat_ack") {
          awaitingHeartbeat = false;
          continue;
        }
        if (frame.kind === "reconnect") {
          throw new TypeError("Discord gateway requested reconnect");
        }
        if (frame.kind === "invalid_session") {
          if (!frame.resumable) this.clearSession();
          throw new TypeError("Discord gateway invalidated the session");
        }
        if (frame.kind !== "dispatch") continue;
        this.sequence = frame.sequence;
        const data = record(frame.data);
        if (!data) continue;
        if (frame.eventName === "READY") {
          this.sessionId = text(data.session_id) || null;
          this.resumeGatewayUrl = text(data.resume_gateway_url) || null;
          this.status = "ready";
          this.lastError = null;
          await this.installationHealth?.ready(
            Array.isArray(data.guilds)
              ? data.guilds.map((guild) => text(record(guild)?.id)).filter(
                Boolean,
              )
              : [],
          );
          continue;
        }
        if (frame.eventName === "RESUMED") {
          this.status = "ready";
          this.lastError = null;
          continue;
        }
        const guildId = text(data.guild_id);
        if (this.guildIds.length && !this.guildIds.includes(guildId)) continue;
        await this.ingestion.ingest(frame.eventName, data);
      }
    } finally {
      socket.close();
    }
  }

  health(): DiscordGatewayHealth {
    return Object.freeze({
      status: this.status,
      sessionId: this.sessionId,
      sequence: this.sequence,
      lastError: this.lastError,
    });
  }

  private token(): string {
    return this.botToken.replace(/^Bot\s+/iu, "").trim();
  }

  private clearSession(): void {
    this.sessionId = null;
    this.resumeGatewayUrl = null;
    this.sequence = null;
  }
}

export class NativeDiscordGatewayTransport implements DiscordGatewayTransport {
  async gatewayUrl(botToken: string): Promise<string> {
    const response = await fetch("https://discord.com/api/v10/gateway/bot", {
      headers: {
        Authorization: `Bot ${botToken}`,
        Accept: "application/json",
        "User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
      },
    });
    if (!response.ok) {
      throw new TypeError(
        `Discord gateway request failed: HTTP ${response.status}`,
      );
    }
    const payload = record(await response.json());
    const url = text(payload?.url);
    if (!url) {
      throw new TypeError("Discord gateway response did not include a url");
    }
    return url;
  }

  connect(url: string): Promise<DiscordGatewaySocket> {
    return NativeDiscordGatewaySocket.connect(url);
  }
}

class NativeDiscordGatewaySocket implements DiscordGatewaySocket {
  private constructor(private readonly socket: WebSocket) {}

  static connect(url: string): Promise<NativeDiscordGatewaySocket> {
    const socket = new WebSocket(url);
    return new Promise((resolve, reject) => {
      socket.addEventListener(
        "open",
        () => resolve(new NativeDiscordGatewaySocket(socket)),
        { once: true },
      );
      socket.addEventListener(
        "error",
        () => reject(new TypeError("Discord gateway connection failed")),
        { once: true },
      );
    });
  }

  send(payload: Readonly<Record<string, unknown>>): void {
    this.socket.send(JSON.stringify(payload));
  }

  receive(signal: AbortSignal): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const clean = () => {
        signal.removeEventListener("abort", aborted);
        this.socket.removeEventListener("message", message);
        this.socket.removeEventListener("close", closed);
        this.socket.removeEventListener("error", failed);
      };
      const aborted = () => {
        clean();
        reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      };
      const message = (event: MessageEvent) => {
        clean();
        try {
          resolve(JSON.parse(String(event.data)));
        } catch {
          reject(new TypeError("Discord gateway sent invalid JSON"));
        }
      };
      const closed = () => {
        clean();
        reject(new TypeError("Discord gateway connection closed"));
      };
      const failed = () => {
        clean();
        reject(new TypeError("Discord gateway connection failed"));
      };
      signal.addEventListener("abort", aborted, { once: true });
      this.socket.addEventListener("message", message, { once: true });
      this.socket.addEventListener("close", closed, { once: true });
      this.socket.addEventListener("error", failed, { once: true });
      if (signal.aborted) aborted();
    });
  }

  close(): void {
    this.socket.close(1000, "shutdown");
  }
}

function identifyPayload(token: string): Readonly<Record<string, unknown>> {
  return Object.freeze({
    op: 2,
    d: Object.freeze({
      token,
      intents: DISCORD_GATEWAY_INTENTS,
      properties: Object.freeze({
        os: "linux",
        browser: "qbot4k",
        device: "qbot4k",
      }),
    }),
  });
}

async function receiveUntil(
  socket: DiscordGatewaySocket,
  signal: AbortSignal,
  milliseconds: number,
): Promise<
  | { readonly timedOut: true }
  | { readonly timedOut: false; readonly value: unknown }
> {
  const completed = new AbortController();
  const combined = AbortSignal.any([signal, completed.signal]);
  try {
    const timeout = abortableDelay(milliseconds, combined).then(() => ({
      timedOut: true as const,
    }));
    const received = socket.receive(combined).then((value) => ({
      timedOut: false as const,
      value,
    }));
    return await Promise.race([timeout, received]);
  } finally {
    completed.abort();
  }
}

function gatewayUrl(url: string): string {
  const parsed = new URL(url);
  parsed.searchParams.set("v", "10");
  parsed.searchParams.set("encoding", "json");
  return parsed.toString();
}

function abortableDelay(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(done, Math.max(0, milliseconds));
    signal.addEventListener("abort", done, { once: true });
    function done() {
      clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
