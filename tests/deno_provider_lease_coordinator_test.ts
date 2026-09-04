import { assertEquals } from "jsr:@std/assert@1.0.14";
import { ProviderLeaseCoordinator } from "../src/providers/provider_lease_coordinator.ts";
import type { ProviderOwnershipLease } from "../src/providers/provider_ownership.ts";

Deno.test("provider coordinator acquires each installation and releases on abort", async () => {
  const calls: unknown[][] = [];
  const controller = new AbortController();
  const leases: ProviderOwnershipLease = {
    installations: (platform) => {
      calls.push(["installations", platform]);
      return Promise.resolve([9, 10]);
    },
    acquire: (installationId, holder, seconds) => {
      calls.push(["acquire", installationId, holder, seconds]);
      if (installationId === 10) controller.abort();
      return Promise.resolve(true);
    },
    renew: () => Promise.resolve(false),
    release: () => Promise.resolve(false),
    owns: () => Promise.resolve(false),
    active: () => Promise.resolve(false),
    releaseAll: (holder) => {
      calls.push(["release-all", holder]);
      return Promise.resolve(2);
    },
  };
  await new ProviderLeaseCoordinator(
    "twitch",
    "twitch-123",
    leases,
  ).run(controller.signal);
  assertEquals(calls, [
    ["installations", "twitch"],
    ["acquire", 9, "twitch-123", 120],
    ["acquire", 10, "twitch-123", 120],
    ["release-all", "twitch-123"],
  ]);
});
