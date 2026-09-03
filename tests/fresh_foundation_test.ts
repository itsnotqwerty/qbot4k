import { assertEquals } from "jsr:@std/assert@1.0.14";
import { app } from "../main.ts";

Deno.test("Fresh foundation exposes runtime health", async () => {
  const response = await app.handler()(
    new Request("http://localhost/api/fresh-health"),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "ready" });
});
