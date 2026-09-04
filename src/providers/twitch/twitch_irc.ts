import type { TwitchTokenManager } from "./twitch_auth.ts";
import type { TwitchIngestionService } from "./twitch_ingestion.ts";
import { parseTwitchIrcMessage } from "./twitch_message.ts";

export interface TwitchIrcConnection {
  writeLine(line: string): Promise<void>;
  readLine(): Promise<string | null>;
  close(): void;
}

export interface TwitchIrcTransport {
  connect(): Promise<TwitchIrcConnection>;
}

export interface TwitchIrcHealth {
  readonly status: "idle" | "connecting" | "ready" | "reconnecting" | "down";
  readonly channels: readonly string[];
  readonly lastError: string | null;
}

export interface TwitchInstallationHealthSink {
  ready(channels: readonly string[]): Promise<void>;
  failed(error: string): Promise<void>;
}

const NOOP_HEALTH: TwitchInstallationHealthSink = {
  ready: () => Promise.resolve(),
  failed: () => Promise.resolve(),
};

export class TwitchIrcClient {
  private status: TwitchIrcHealth["status"] = "idle";
  private joinedChannels: readonly string[] = [];
  private lastError: string | null = null;
  private connection: TwitchIrcConnection | null = null;
  constructor(
    private readonly tokens: TwitchTokenManager,
    private readonly transport: TwitchIrcTransport,
    private readonly ingestion: TwitchIngestionService,
    private readonly reconnectMilliseconds = 1_000,
    private readonly installationHealth: TwitchInstallationHealthSink =
      NOOP_HEALTH,
  ) {}

  async run(signal: AbortSignal): Promise<void> {
    let delay = this.reconnectMilliseconds;
    while (!signal.aborted) {
      try {
        await this.connectOnce(signal);
        delay = this.reconnectMilliseconds;
      } catch (error) {
        if (signal.aborted) break;
        this.status = "reconnecting";
        this.lastError = errorMessage(error);
        await this.installationHealth.failed(this.lastError);
        await abortableDelay(delay, signal);
        delay = Math.min(60_000, delay * 2);
      }
    }
    this.status = "down";
  }

  async connectOnce(signal: AbortSignal): Promise<void> {
    const validation = await this.tokens.validateToken();
    const channels = await this.ingestion.channels();
    if (!channels.length) throw new TypeError("No Twitch channels configured");
    const connection = await this.transport.connect();
    this.connection = connection;
    this.status = "connecting";
    try {
      await connection.writeLine(`PASS oauth:${validation.accessToken}`);
      await connection.writeLine(`NICK ${validation.login}`);
      await connection.writeLine(
        "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership",
      );
      const normalizedChannels = [
        ...new Set(channels.map(channelName).filter(Boolean)),
      ];
      for (const channel of normalizedChannels) {
        await connection.writeLine(`JOIN #${channel}`);
      }
      this.joinedChannels = Object.freeze(normalizedChannels);
      this.status = "ready";
      this.lastError = null;
      await this.installationHealth.ready(normalizedChannels);
      while (!signal.aborted) {
        const line = await connection.readLine();
        if (line === null) throw new TypeError("Twitch IRC connection closed");
        if (line.startsWith("PING ")) {
          await connection.writeLine(line.replace(/^PING/u, "PONG"));
          continue;
        }
        const payload = parseTwitchIrcMessage(line);
        if (payload) await this.ingestion.ingest(payload);
      }
    } finally {
      this.connection = null;
      connection.close();
    }
  }

  async sendMessage(channel: string, message: string): Promise<string> {
    const normalizedChannel = channelName(channel);
    const normalizedMessage = message.trim();
    if (
      !normalizedChannel || !this.joinedChannels.includes(normalizedChannel)
    ) {
      throw new TypeError("Twitch channel is not joined");
    }
    if (!normalizedMessage || /[\r\n]/u.test(normalizedMessage)) {
      throw new TypeError("Twitch message must be one non-empty IRC line");
    }
    if (!this.connection || this.status !== "ready") {
      throw new TypeError("Twitch IRC connection is not ready");
    }
    await this.connection.writeLine(
      `PRIVMSG #${normalizedChannel} :${normalizedMessage.slice(0, 500)}`,
    );
    return crypto.randomUUID();
  }

  health(): TwitchIrcHealth {
    return Object.freeze({
      status: this.status,
      channels: this.joinedChannels,
      lastError: this.lastError,
    });
  }
}

export class NativeTwitchIrcTransport implements TwitchIrcTransport {
  async connect(): Promise<TwitchIrcConnection> {
    const connection = await Deno.connectTls({
      hostname: "irc.chat.twitch.tv",
      port: 6697,
    });
    return new NativeTwitchIrcConnection(connection);
  }
}

class NativeTwitchIrcConnection implements TwitchIrcConnection {
  private readonly reader: ReadableStreamDefaultReader<string>;
  private readonly writer: WritableStreamDefaultWriter<Uint8Array>;

  constructor(private readonly connection: Deno.TlsConn) {
    this.reader = connection.readable
      .pipeThrough(new TextDecoderStream())
      .pipeThrough(lineStream())
      .getReader();
    this.writer = connection.writable.getWriter();
  }

  async writeLine(line: string): Promise<void> {
    await this.writer.write(new TextEncoder().encode(`${line}\r\n`));
  }

  async readLine(): Promise<string | null> {
    const result = await this.reader.read();
    return result.done ? null : result.value;
  }

  close(): void {
    this.reader.cancel().catch(() => undefined);
    this.writer.releaseLock();
    this.connection.close();
  }
}

function lineStream(): TransformStream<string, string> {
  let buffer = "";
  return new TransformStream({
    transform(chunk, controller) {
      buffer += chunk;
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        controller.enqueue(buffer.slice(0, newline).replace(/\r$/u, ""));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
      }
    },
    flush(controller) {
      if (buffer) controller.enqueue(buffer);
    },
  });
}

function channelName(value: string): string {
  return value.trim().replace(/^#/u, "").toLocaleLowerCase();
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
