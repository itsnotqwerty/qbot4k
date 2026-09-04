import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import {
  constantTimeEqual,
  createDiscordInstallState,
  createOauthState,
  createSessionCookie,
  createTwitchInstallState,
  parseDiscordInstallState,
  parseSessionCookie,
  parseTwitchInstallState,
  verifyEventsubSignature,
  verifyOauthState,
  verifyRequestOrigin,
} from "../src/security/security.ts";

Deno.test("OAuth state matches the Python nonce and HMAC contract", async () => {
  const state = await createOauthState("shared-secret", "fixed-nonce");
  assertEquals(
    state,
    "fixed-nonce.5111a68bd716f587b7bf183940fbae0bb8d2046cd36b0fce3bf79f9fd42d5509",
  );
  assertEquals(await verifyOauthState("shared-secret", state), true);
  assertEquals(await verifyOauthState("shared-secret", `${state}0`), false);
});

const now = new Date("2026-09-02T12:00:00Z");

Deno.test("session cookies round-trip and reject tampering and expiry", async () => {
  const cookie = await createSessionCookie("session-secret", {
    userId: "fixture-user",
    username: "Fixture Operator",
    role: "moderator",
    expiresAt: "2030-01-01T00:00:00+00:00",
    communityId: 1,
    sessionVersion: 1,
  });
  const session = await parseSessionCookie("session-secret", cookie, now);
  assertEquals(session?.communityId, 1);
  assertEquals(
    await parseSessionCookie("session-secret", cookie + "x", now),
    null,
  );
  assertEquals(
    await parseSessionCookie(
      "session-secret",
      cookie,
      new Date("2031-01-01T00:00:00Z"),
    ),
    null,
  );
});

Deno.test("Deno reads a Python-created session cookie", async () => {
  const pythonCookie =
    "eyJjb21tdW5pdHlfaWQiOjEsImV4cGlyZXNfYXQiOiIyMDMwLTAxLTAxVDAwOjAwOjAwKzAwOjAwIiwicm9sZSI6Im1vZGVyYXRvciIsInNlc3Npb25fdmVyc2lvbiI6MSwidXNlcl9pZCI6ImZpeHR1cmUtdXNlciIsInVzZXJuYW1lIjoiRml4dHVyZSBPcGVyYXRvciJ9.fe404dcefbbccd1bcf602d332ba8f2e19e5a8c7a4c64d76d0104290c6df2dacb";
  const session = await parseSessionCookie("session-secret", pythonCookie, now);
  assertEquals(session, {
    userId: "fixture-user",
    username: "Fixture Operator",
    role: "moderator",
    expiresAt: "2030-01-01T00:00:00+00:00",
    communityId: 1,
    sessionVersion: 1,
  });
});

Deno.test("installation states are tenant-bound, scoped, and expiring", async () => {
  const discord = await createDiscordInstallState("session-secret", {
    operatorId: "operator-1",
    communityId: 42,
    guildId: "guild-9",
    now,
    nonce: "fixed-nonce",
  });
  assertEquals(
    (await parseDiscordInstallState("session-secret", discord, now))?.guildId,
    "guild-9",
  );
  assertEquals(
    await parseDiscordInstallState(
      "session-secret",
      discord,
      new Date("2026-09-02T12:16:00Z"),
    ),
    null,
  );

  const twitch = await createTwitchInstallState("session-secret", {
    operatorId: "operator-1",
    communityId: 42,
    broadcasterLogin: "Streamer_A",
    now,
    nonce: "fixed-nonce",
    scopes: ["moderator:read:followers", "channel:read:subscriptions"],
  });
  const parsed = await parseTwitchInstallState("session-secret", twitch, now);
  assertEquals(parsed?.broadcasterLogin, "streamer_a");
  assertEquals(parsed?.scopes, [
    "channel:read:subscriptions",
    "moderator:read:followers",
  ]);
  await assertRejects(
    () =>
      createTwitchInstallState("secret", {
        operatorId: "1",
        communityId: 1,
        broadcasterLogin: "channel",
        scopes: ["user:edit"],
      }),
    TypeError,
    "unsupported scopes",
  );
});

Deno.test("EventSub verifies raw bytes and rejects stale or modified messages", async () => {
  const body = new TextEncoder().encode('{"event":{"id":"fixture"}}');
  const signature =
    "sha256=0b3f9c7b04c3be00c1047c4e2b44d18c1e2db4bef19d12b4cfb03b6e15412757";
  const input = {
    messageId: "message-1",
    timestamp: "2026-09-02T12:00:00Z",
    body,
    signature,
    now,
  };
  assertEquals(await verifyEventsubSignature("eventsub-secret", input), true);
  assertEquals(
    await verifyEventsubSignature("eventsub-secret", {
      ...input,
      body: new TextEncoder().encode("{}"),
    }),
    false,
  );
  assertEquals(
    await verifyEventsubSignature("eventsub-secret", {
      ...input,
      now: new Date("2026-09-02T12:11:00Z"),
    }),
    false,
  );
});

Deno.test("constant-time and origin guards preserve request policy", () => {
  assertEquals(constantTimeEqual("same", "same"), true);
  assertEquals(constantTimeEqual("same", "different"), false);
  assertEquals(
    verifyRequestOrigin("GET", new Headers(), "https://example.test"),
    true,
  );
  assertEquals(
    verifyRequestOrigin(
      "POST",
      new Headers({ origin: "https://example.test", "x-csrf-token": "token" }),
      "https://example.test",
      "token",
    ),
    true,
  );
  assertEquals(
    verifyRequestOrigin(
      "POST",
      new Headers({ origin: "https://evil.test" }),
      "https://example.test",
    ),
    false,
  );
});
