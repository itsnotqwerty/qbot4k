const encoder = new TextEncoder();
const decoder = new TextDecoder();

export interface DashboardSession {
  readonly userId: string;
  readonly username: string;
  readonly role: string;
  readonly expiresAt: string;
  readonly communityId: number | null;
  readonly sessionVersion: number;
}

export interface DiscordInstallState {
  readonly operatorId: string;
  readonly communityId: number;
  readonly guildId: string;
  readonly nonce: string;
  readonly expiresAt: string;
}

export interface TwitchInstallState {
  readonly operatorId: string;
  readonly communityId: number;
  readonly broadcasterLogin: string;
  readonly scopes: readonly string[];
  readonly nonce: string;
  readonly expiresAt: string;
}

export const TWITCH_INSTALL_SCOPES = new Set([
  "channel:read:subscriptions",
  "moderator:manage:banned_users",
  "moderator:manage:chat_settings",
  "moderator:manage:shield_mode",
  "moderator:read:followers",
]);

function pythonJson(value: unknown): string {
  const sortValue = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(sortValue);
    if (item !== null && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, child]) => [key, sortValue(child)]),
      );
    }
    return item;
  };
  return JSON.stringify(sortValue(value)).replace(
    /[\u007f-\uffff]/g,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_");
}

function base64UrlDecode(value: string): Uint8Array {
  const binary = atob(value.replaceAll("-", "+").replaceAll("_", "/"));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function hexadecimal(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

async function hmacHex(secret: string, message: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const bytes = new Uint8Array(message.byteLength);
  bytes.set(message);
  return hexadecimal(await crypto.subtle.sign("HMAC", key, bytes));
}

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = encoder.encode(left);
  const rightBytes = encoder.encode(right);
  let mismatch = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index++) {
    mismatch |= (leftBytes[index] ?? 0) ^ (rightBytes[index] ?? 0);
  }
  return mismatch === 0;
}

async function createSignedEnvelope(
  secret: string,
  payload: unknown,
): Promise<string> {
  const encodedPayload = base64UrlEncode(encoder.encode(pythonJson(payload)));
  return `${encodedPayload}.${await hmacHex(
    secret,
    encoder.encode(encodedPayload),
  )}`;
}

async function parseSignedEnvelope(
  secret: string,
  value?: string | null,
): Promise<Record<string, unknown> | null> {
  if (!value?.includes(".")) return null;
  const separator = value.lastIndexOf(".");
  const encodedPayload = value.slice(0, separator);
  const signature = value.slice(separator + 1);
  const expected = await hmacHex(secret, encoder.encode(encodedPayload));
  if (!constantTimeEqual(signature, expected)) return null;
  try {
    const payload = JSON.parse(decoder.decode(base64UrlDecode(encodedPayload)));
    return payload !== null && typeof payload === "object" &&
        !Array.isArray(payload)
      ? payload as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function validFutureTimestamp(value: unknown, now: Date): string | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) || parsed < now ? null : value;
}

export async function createSessionCookie(
  secret: string,
  session: DashboardSession,
): Promise<string> {
  return await createSignedEnvelope(secret, {
    community_id: session.communityId,
    expires_at: session.expiresAt,
    role: session.role,
    session_version: session.sessionVersion,
    user_id: session.userId,
    username: session.username,
  });
}

export async function parseSessionCookie(
  secret: string,
  value?: string | null,
  now = new Date(),
): Promise<DashboardSession | null> {
  const payload = await parseSignedEnvelope(secret, value);
  if (!payload) return null;
  const expiresAt = validFutureTimestamp(payload.expires_at, now);
  if (!expiresAt) return null;
  const communityId =
    payload.community_id === null || payload.community_id === undefined
      ? null
      : Number(payload.community_id);
  const sessionVersion = Number(payload.session_version || 1);
  if (
    (communityId !== null && !Number.isInteger(communityId)) ||
    !Number.isInteger(sessionVersion)
  ) return null;
  return Object.freeze({
    userId: String(payload.user_id || ""),
    username: String(payload.username || ""),
    role: String(payload.role || ""),
    expiresAt,
    communityId,
    sessionVersion,
  });
}

function pythonIso(date: Date): string {
  return date.toISOString().replace(/\.000Z$/u, "+00:00").replace(
    /Z$/u,
    "+00:00",
  );
}

function nonce(): string {
  return base64UrlEncode(crypto.getRandomValues(new Uint8Array(24))).replace(
    /=+$/u,
    "",
  );
}

export async function createOauthState(
  secret: string,
  stateNonce = nonce(),
): Promise<string> {
  if (!secret || !stateNonce) {
    throw new TypeError("OAuth state requires a secret and nonce");
  }
  return `${stateNonce}.${await hmacHex(secret, encoder.encode(stateNonce))}`;
}

export async function verifyOauthState(
  secret: string,
  state: string | null,
): Promise<boolean> {
  if (!secret || !state?.includes(".")) return false;
  const separator = state.lastIndexOf(".");
  const stateNonce = state.slice(0, separator);
  const signature = state.slice(separator + 1);
  if (!stateNonce || !signature) return false;
  const expected = await hmacHex(secret, encoder.encode(stateNonce));
  return constantTimeEqual(signature, expected);
}

export async function createDiscordInstallState(
  secret: string,
  input: {
    operatorId: string;
    communityId: number;
    guildId: string;
    now?: Date;
    nonce?: string;
  },
): Promise<string> {
  const expiresAt = new Date((input.now ?? new Date()).valueOf() + 15 * 60_000);
  return await createSignedEnvelope(secret, {
    community_id: input.communityId,
    expires_at: pythonIso(expiresAt),
    guild_id: input.guildId.trim(),
    nonce: input.nonce ?? nonce(),
    operator_id: input.operatorId.trim(),
  });
}

export async function parseDiscordInstallState(
  secret: string,
  value: string | null,
  now = new Date(),
): Promise<DiscordInstallState | null> {
  const payload = await parseSignedEnvelope(secret, value);
  if (!payload) return null;
  const expiresAt = validFutureTimestamp(payload.expires_at, now);
  const operatorId = String(payload.operator_id || "").trim();
  const guildId = String(payload.guild_id || "").trim();
  const stateNonce = String(payload.nonce || "").trim();
  const communityId = Number(payload.community_id);
  if (
    !expiresAt || !operatorId || !guildId || !stateNonce ||
    !Number.isInteger(communityId)
  ) return null;
  return Object.freeze({
    operatorId,
    communityId,
    guildId,
    nonce: stateNonce,
    expiresAt,
  });
}

export async function createTwitchInstallState(
  secret: string,
  input: {
    operatorId: string;
    communityId: number;
    broadcasterLogin: string;
    scopes: readonly string[];
    now?: Date;
    nonce?: string;
  },
): Promise<string> {
  const scopes = [
    ...new Set(input.scopes.map((scope) => scope.trim()).filter(Boolean)),
  ].sort();
  if (
    !scopes.length || scopes.some((scope) => !TWITCH_INSTALL_SCOPES.has(scope))
  ) {
    throw new TypeError("Twitch installation requested unsupported scopes");
  }
  const expiresAt = new Date((input.now ?? new Date()).valueOf() + 20 * 60_000);
  return await createSignedEnvelope(secret, {
    broadcaster_login: input.broadcasterLogin.trim().toLocaleLowerCase(),
    community_id: input.communityId,
    expires_at: pythonIso(expiresAt),
    nonce: input.nonce ?? nonce(),
    operator_id: input.operatorId.trim(),
    scopes,
  });
}

export async function parseTwitchInstallState(
  secret: string,
  value: string | null,
  now = new Date(),
): Promise<TwitchInstallState | null> {
  const payload = await parseSignedEnvelope(secret, value);
  if (!payload) return null;
  const expiresAt = validFutureTimestamp(payload.expires_at, now);
  const operatorId = String(payload.operator_id || "").trim();
  const broadcasterLogin = String(payload.broadcaster_login || "").trim()
    .toLocaleLowerCase();
  const stateNonce = String(payload.nonce || "").trim();
  const communityId = Number(payload.community_id);
  const scopes = Array.isArray(payload.scopes)
    ? [
      ...new Set(
        payload.scopes.map((scope) => String(scope).trim()).filter(Boolean),
      ),
    ].sort()
    : [];
  if (
    !expiresAt || !operatorId || !broadcasterLogin || !stateNonce ||
    !Number.isInteger(communityId) || !scopes.length ||
    scopes.some((scope) => !TWITCH_INSTALL_SCOPES.has(scope))
  ) return null;
  return Object.freeze({
    operatorId,
    communityId,
    broadcasterLogin,
    scopes: Object.freeze(scopes),
    nonce: stateNonce,
    expiresAt,
  });
}

export async function verifyEventsubSignature(
  secret: string,
  input: {
    messageId: string;
    timestamp: string;
    body: Uint8Array;
    signature: string;
    now?: Date;
    maxAgeSeconds?: number;
  },
): Promise<boolean> {
  if (!secret || !input.messageId || !input.timestamp || !input.signature) {
    return false;
  }
  const received = new Date(input.timestamp);
  if (Number.isNaN(received.valueOf())) return false;
  if (
    Math.abs((input.now ?? new Date()).valueOf() - received.valueOf()) >
      (input.maxAgeSeconds ?? 600) * 1000
  ) return false;
  const prefix = encoder.encode(input.messageId + input.timestamp);
  const message = new Uint8Array(prefix.length + input.body.length);
  message.set(prefix);
  message.set(input.body, prefix.length);
  return constantTimeEqual(
    `sha256=${await hmacHex(secret, message)}`,
    input.signature,
  );
}

export function verifyRequestOrigin(
  method: string,
  headers: Headers,
  expectedOrigin: string,
  expectedCsrfToken?: string,
): boolean {
  if (!["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    return true;
  }
  const origin = headers.get("origin");
  if (!origin || !constantTimeEqual(origin, expectedOrigin)) return false;
  return expectedCsrfToken === undefined ||
    constantTimeEqual(headers.get("x-csrf-token") ?? "", expectedCsrfToken);
}
