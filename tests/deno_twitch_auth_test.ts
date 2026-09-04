import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import {
  TwitchReauthorizationRequired,
  TwitchTemporaryAuthError,
  TwitchTokenManager,
} from "../src/providers/twitch/twitch_auth.ts";

Deno.test("Twitch token manager validates normalized access tokens", async () => {
  const requests: Request[] = [];
  const manager = new TwitchTokenManager({
    initialAccessToken: "oauth:access-1",
    fetcher: (input, init) => {
      requests.push(new Request(input, init));
      return Promise.resolve(Response.json({
        login: "broadcaster",
        client_id: "client-1",
        user_id: "user-1",
      }));
    },
  });
  assertEquals(await manager.validateToken(), {
    accessToken: "access-1",
    login: "broadcaster",
    clientId: "client-1",
    userId: "user-1",
  });
  assertEquals(requests[0].headers.get("Authorization"), "OAuth access-1");
});

Deno.test("Twitch token manager refreshes once and persists rotation", async () => {
  const persisted: unknown[][] = [];
  let refreshes = 0;
  let validations = 0;
  const manager = new TwitchTokenManager({
    initialAccessToken: "expired",
    refreshToken: "refresh-1",
    clientId: "client-1",
    clientSecret: "secret-1",
    fetcher: (input) => {
      const url = String(input);
      if (url.endsWith("/validate")) {
        validations += 1;
        return Promise.resolve(
          validations === 1
            ? new Response("", { status: 401 })
            : Response.json({
              login: "broadcaster",
              client_id: "client-1",
              user_id: "user-1",
            }),
        );
      }
      refreshes += 1;
      return Promise.resolve(Response.json({
        access_token: "access-2",
        refresh_token: "refresh-2",
      }));
    },
    onTokenRefresh: (accessToken, refreshToken) => {
      persisted.push([accessToken, refreshToken]);
    },
  });
  assertEquals(await manager.validateToken(), {
    accessToken: "access-2",
    login: "broadcaster",
    clientId: "client-1",
    userId: "user-1",
  });
  assertEquals(refreshes, 1);
  assertEquals(persisted, [["access-2", "refresh-2"]]);
});

Deno.test("Twitch token manager serializes concurrent refreshes", async () => {
  let refreshes = 0;
  const manager = new TwitchTokenManager({
    initialAccessToken: "expired",
    refreshToken: "refresh-1",
    clientId: "client-1",
    clientSecret: "secret-1",
    fetcher: () => {
      refreshes += 1;
      return Promise.resolve(Response.json({ access_token: "access-2" }));
    },
  });
  assertEquals(
    await Promise.all([
      manager.refreshAccessToken(),
      manager.refreshAccessToken(),
    ]),
    ["access-2", "access-2"],
  );
  assertEquals(refreshes, 1);
});

Deno.test("Twitch token manager distinguishes rejected and unavailable auth", async () => {
  await assertRejects(
    () =>
      new TwitchTokenManager({
        initialAccessToken: "expired",
        fetcher: () => Promise.resolve(new Response("", { status: 401 })),
      }).validateToken(),
    TwitchReauthorizationRequired,
  );
  await assertRejects(
    () =>
      new TwitchTokenManager({
        initialAccessToken: "token",
        fetcher: () => Promise.reject(new Error("offline")),
      }).validateToken(),
    TwitchTemporaryAuthError,
    "offline",
  );
});
