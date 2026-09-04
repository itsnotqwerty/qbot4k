import { assertEquals } from "jsr:@std/assert@1.0.14";
import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { app, createApp } from "../main.ts";
import Home from "../routes/index.tsx";
import type { DatabaseHealthSource } from "../src/data/database.ts";
import { RoleHealthMonitor } from "../src/core/health.ts";

Deno.test("Fresh foundation exposes runtime health", async () => {
  const response = await app.handler()(
    new Request("http://localhost/api/fresh-health"),
  );
  assertEquals(response.status, 200);
  assertEquals(await response.json(), { status: "ready" });
});

Deno.test("Fresh shell renders core navigation without JavaScript", async () => {
  const html = render(h(Home, {}));
  assertEquals((await Deno.stat("static/styles.css")).isFile, true);
  assertEquals(html.includes('href="/health/ready"'), true);
  assertEquals(html.includes(">View status</a>"), true);
  assertEquals(html.includes(">Online</output>"), true);
  assertEquals(html.includes(">Link Discord</a>"), true);
  assertEquals(html.includes("Discord and Twitch"), true);
});

Deno.test("Fresh legal routes publish configured-independent public pages", async () => {
  for (
    const [path, heading] of [["/privacy", "Privacy policy"], [
      "/terms",
      "Terms of service",
    ]]
  ) {
    const response = await app.handler()(
      new Request(`http://localhost${path}`),
    );
    assertEquals(response.status, 200);
    assertEquals(
      response.headers.get("content-type"),
      "text/html; charset=utf-8",
    );
    assertEquals((await response.text()).includes(heading), true);
  }
});

Deno.test("Fresh readiness route uses PostgreSQL and role health", async () => {
  const database: DatabaseHealthSource = {
    health: () =>
      Promise.resolve({
        status: "degraded",
        backend: "postgresql",
        path: "postgresql://database/qbot4k",
        tableCount: 41,
        integrity: "migration_pending",
        schemaVersion: 26,
      }),
  };
  const monitor = new RoleHealthMonitor(database, "web");
  monitor.setStatus("ready");
  const response = await createApp(monitor).handler()(
    new Request("http://localhost/health/ready"),
  );
  assertEquals(response.status, 503);
  assertEquals((await response.json()).database.integrity, "migration_pending");
});
