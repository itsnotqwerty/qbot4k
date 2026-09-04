import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createReadOnlyWebHandler } from "../src/web/web_read_only.ts";

Deno.test("read-only web mode permits observational requests", async () => {
  let calls = 0;
  const handler = createReadOnlyWebHandler(() => {
    calls += 1;
    return new Response("ok");
  });
  assertEquals((await handler(request("GET", "/api/overview"))).status, 200);
  assertEquals((await handler(request("HEAD", "/health/ready"))).status, 200);
  assertEquals(calls, 2);
});

Deno.test("read-only web mode blocks writes before controllers run", async () => {
  let calls = 0;
  const handler = createReadOnlyWebHandler(() => {
    calls += 1;
    return new Response("unexpected");
  });
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    const response = await handler(request(method, "/api/moderation/bulk"));
    assertEquals(response.status, 503);
    assertEquals(response.headers.get("cache-control"), "no-store");
  }
  assertEquals(calls, 0);
});

Deno.test("read-only web mode blocks side-effecting OAuth callbacks", async () => {
  let calls = 0;
  const handler = createReadOnlyWebHandler(() => {
    calls += 1;
    return new Response("unexpected");
  });
  for (
    const path of [
      "/auth/discord/callback",
      "/oauth/discord/callback",
      "/integrations/discord/callback",
      "/integrations/twitch/callback",
    ]
  ) {
    assertEquals((await handler(request("GET", path))).status, 503);
  }
  assertEquals(calls, 0);
});

function request(method: string, path: string): Request {
  return new Request(`https://green.example${path}`, { method });
}
