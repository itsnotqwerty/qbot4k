import { assertEquals, assertStringIncludes } from "jsr:@std/assert@1.0.14";
import {
  recordReliabilityBuckets,
  type ReliabilityBucket,
  RoleHealthMonitor,
  type StatusStore,
} from "../src/core/health.ts";
import { statusPage } from "../src/web/web_pages.ts";
import type { DatabaseHealth } from "../src/data/database.ts";

class FakeStore implements StatusStore {
  readonly heartbeats: Record<string, { status: string; updatedAt: string }> =
    {};
  readonly written: ReliabilityBucket[] = [];

  writeRoleHeartbeat(role: string, status: string): Promise<void> {
    this.heartbeats[role] = {
      status,
      updatedAt: new Date().toISOString(),
    };
    return Promise.resolve();
  }

  roleHeartbeats(): Promise<
    Readonly<Record<string, { status: string; updatedAt: string }>>
  > {
    return Promise.resolve(this.heartbeats);
  }

  recordBucket(
    service: string,
    bucketStart: string,
    observer: string,
    isUp: boolean,
    status: string,
  ): Promise<void> {
    this.written.push({ bucketStart, observer, isUp, status });
    void service;
    return Promise.resolve();
  }

  buckets(
    service: string,
    _sinceIso: string,
  ): Promise<readonly ReliabilityBucket[]> {
    return Promise.resolve(
      this.written.filter((b) => b.bucketStart && service === service),
    );
  }
}

const fakeHealth = (): DatabaseHealth => ({
  status: "ready",
  path: "memory",
  backend: "postgresql",
  tableCount: 1,
  integrity: "ok",
  schemaVersion: "1",
});

Deno.test("reliability recorder writes one bucket per service with observer", async () => {
  const store = new FakeStore();
  const monitor = new RoleHealthMonitor(
    { health: () => Promise.resolve(fakeHealth()) },
    "web",
    new Date("2026-09-05T10:00:00Z"),
    store,
  );
  monitor.setStatus("ready");
  store.heartbeats.discord = {
    status: "ready",
    updatedAt: new Date("2026-09-05T10:00:00Z").toISOString(),
  };
  await recordReliabilityBuckets(
    monitor,
    store,
    new Date("2026-09-05T10:00:30Z"),
  );
  const services = store.written.map((b) => b.bucketStart.slice(0, 16));
  assertEquals(new Set(services).size, 1, "all buckets share the minute");
  assertEquals(store.written.length, 5);
  const web = store.written.find((b) => b.observer === "web");
  assertEquals(web?.isUp, true);
  const missing = store.written.filter((b) => b.status === "unknown");
  // jobs/twitch/analysis have no heartbeat -> recorded as not up
  assertEquals(missing.length >= 3, true);
  for (const b of store.written) assertEquals(b.observer, "web");
});

Deno.test("status page renders recorded uptime and downtime events", async () => {
  const store = new FakeStore();
  const now = new Date();
  const minute = 60_000;
  const base = Math.floor(now.valueOf() / minute) * minute;
  // web: 3 minutes, one down
  for (let i = 0; i < 3; i++) {
    store.written.push({
      bucketStart: new Date(base - i * minute).toISOString(),
      observer: "web",
      isUp: i !== 1,
      status: i === 1 ? "down" : "ready",
    });
  }
  store.heartbeats.web = {
    status: "ready",
    updatedAt: now.toISOString(),
  };
  const html = await (await statusPage(store)).text();
  assertStringIncludes(html, "66.667% uptime", "2 of 3 minutes up");
  assertStringIncludes(html, "3 observations");
  assertStringIncludes(html, "Downtime events");
  assertStringIncludes(html, "Recorded status: down");
  assertStringIncludes(html, 'chip chip-ok">ready');
});

Deno.test("status page degrades gracefully without a store", async () => {
  const html = await (await statusPage()).text();
  assertStringIncludes(html, "no observations yet");
  assertStringIncludes(html, 'chip chip-warn">unknown');
});
