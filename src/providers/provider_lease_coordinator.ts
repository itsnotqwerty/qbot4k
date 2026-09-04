import type { ProviderOwnershipLease } from "./provider_ownership.ts";

export class ProviderLeaseCoordinator {
  constructor(
    private readonly platform: "discord" | "twitch",
    private readonly holder: string,
    private readonly leases: ProviderOwnershipLease,
    private readonly leaseSeconds = 120,
    private readonly renewMilliseconds = 40_000,
    private readonly onError: (error: unknown) => void = () => undefined,
  ) {}

  async run(signal: AbortSignal): Promise<void> {
    try {
      while (!signal.aborted) {
        try {
          const installationIds = await this.leases.installations(this.platform);
          for (const installationId of installationIds) {
            await this.leases.acquire(
              installationId,
              this.holder,
              this.leaseSeconds,
            );
          }
        } catch (error) {
          this.onError(error);
        }
        await abortableDelay(this.renewMilliseconds, signal);
      }
    } finally {
      await this.leases.releaseAll(this.holder);
    }
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", finish, { once: true });
    function finish() {
      clearTimeout(timeout);
      signal.removeEventListener("abort", finish);
      resolve();
    }
  });
}
