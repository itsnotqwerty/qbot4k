import { assertEquals } from "jsr:@std/assert@1.0.14";
import { createSecurityHeadersHandler } from "../src/web/web_security_headers.ts";

const handler = createSecurityHeadersHandler(
  () => new Response("ok", { status: 200 }),
);

Deno.test("Fresh responses include browser security headers", async () => {
  const response = await handler(new Request("http://127.0.0.1/dashboard"));
  assertEquals(response.headers.get("x-content-type-options"), "nosniff");
  assertEquals(response.headers.get("x-frame-options"), "DENY");
  assertEquals(response.headers.get("referrer-policy"), "no-referrer");
  assertEquals(
    response.headers.get("permissions-policy"),
    "camera=(), geolocation=(), microphone=()",
  );
  assertEquals(
    response.headers.get("content-security-policy")?.includes(
      "frame-ancestors 'none'",
    ),
    true,
  );
  assertEquals(response.headers.has("strict-transport-security"), false);
});

Deno.test("Fresh responses enable HSTS behind HTTPS ingress", async () => {
  const response = await handler(
    new Request("http://127.0.0.1/dashboard", {
      headers: { "x-forwarded-proto": "https" },
    }),
  );
  assertEquals(
    response.headers.get("strict-transport-security"),
    "max-age=31536000; includeSubDomains",
  );
});
