export type DiscordGatewayFrame =
  | Readonly<{ kind: "hello"; heartbeatIntervalMs: number }>
  | Readonly<
    { kind: "dispatch"; eventName: string; sequence: number; data: unknown }
  >
  | Readonly<{ kind: "heartbeat_ack" }>
  | Readonly<{ kind: "reconnect" }>
  | Readonly<{ kind: "invalid_session"; resumable: boolean }>;

export type TwitchEventsubFrame =
  | Readonly<
    { kind: "welcome"; sessionId: string; keepaliveTimeoutSeconds: number }
  >
  | Readonly<{ kind: "keepalive" }>
  | Readonly<{ kind: "reconnect"; reconnectUrl: string }>
  | Readonly<
    { kind: "notification"; subscriptionType: string; payload: unknown }
  >
  | Readonly<{ kind: "revocation"; subscriptionType: string; status: string }>;

export interface ProviderSocketFactory {
  connect(url: string): WebSocket;
}

export class NativeProviderSocketFactory implements ProviderSocketFactory {
  connect(url: string): WebSocket {
    return new WebSocket(url);
  }
}

export function decodeDiscordGatewayFrame(value: unknown): DiscordGatewayFrame {
  const frame = object(value, "Discord Gateway frame");
  const opcode = integer(frame.op, "Discord Gateway opcode");
  if (opcode === 10) {
    const data = object(frame.d, "Discord hello data");
    return Object.freeze({
      kind: "hello",
      heartbeatIntervalMs: integer(
        data.heartbeat_interval,
        "heartbeat interval",
      ),
    });
  }
  if (opcode === 0) {
    return Object.freeze({
      kind: "dispatch",
      eventName: text(frame.t, "Discord event name"),
      sequence: integer(frame.s, "Discord sequence"),
      data: frame.d,
    });
  }
  if (opcode === 11) return Object.freeze({ kind: "heartbeat_ack" });
  if (opcode === 7) return Object.freeze({ kind: "reconnect" });
  if (opcode === 9) {
    return Object.freeze({
      kind: "invalid_session",
      resumable: frame.d === true,
    });
  }
  throw new TypeError(`unsupported Discord Gateway opcode: ${opcode}`);
}

export function decodeTwitchEventsubFrame(value: unknown): TwitchEventsubFrame {
  const frame = object(value, "Twitch EventSub frame");
  const metadata = object(frame.metadata, "Twitch EventSub metadata");
  const payload = object(frame.payload, "Twitch EventSub payload");
  const messageType = text(metadata.message_type, "Twitch message type");
  if (messageType === "session_welcome") {
    const session = object(payload.session, "Twitch session");
    return Object.freeze({
      kind: "welcome",
      sessionId: text(session.id, "Twitch session id"),
      keepaliveTimeoutSeconds: integer(
        session.keepalive_timeout_seconds,
        "Twitch keepalive timeout",
      ),
    });
  }
  if (messageType === "session_keepalive") {
    return Object.freeze({ kind: "keepalive" });
  }
  if (messageType === "session_reconnect") {
    const session = object(payload.session, "Twitch session");
    return Object.freeze({
      kind: "reconnect",
      reconnectUrl: text(session.reconnect_url, "Twitch reconnect URL"),
    });
  }
  const subscription = object(payload.subscription, "Twitch subscription");
  const subscriptionType = text(subscription.type, "Twitch subscription type");
  if (messageType === "notification") {
    return Object.freeze({
      kind: "notification",
      subscriptionType,
      payload: payload.event,
    });
  }
  if (messageType === "revocation") {
    return Object.freeze({
      kind: "revocation",
      subscriptionType,
      status: text(subscription.status, "Twitch subscription status"),
    });
  }
  throw new TypeError(
    `unsupported Twitch EventSub message type: ${messageType}`,
  );
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new TypeError(`${name} must be a non-negative integer`);
  }
  return parsed;
}

function text(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${name} must be a non-empty string`);
  }
  return value;
}
