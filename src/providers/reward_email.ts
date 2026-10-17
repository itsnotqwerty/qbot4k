import type { RewardDelivery } from "../data/backer_repository.ts";

export class ResendRewardDelivery implements RewardDelivery {
  readonly ready: boolean;
  constructor(
    private readonly key: string | null,
    private readonly from: string | null,
    private readonly origin: string | null,
    private readonly signingKey: string | null,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.ready = Boolean(
      key && from && signingKey && origin &&
        new URL(origin).protocol === "https:",
    );
  }
  async claimCode(nonce: string): Promise<string> {
    if (!this.signingKey) {
      throw new Error("Claim signing key is not configured.");
    }
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(this.signingKey),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const signature = await crypto.subtle.sign(
      "HMAC",
      key,
      encoder.encode(`qbot4k:reward-claim:v1:${nonce}`),
    );
    return Array.from(
      new Uint8Array(signature),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
  }
  async verifySender(): Promise<void> {
    if (!this.ready) throw new Error("Reward sender is not configured.");
    const response = await this.fetcher("https://api.resend.com/domains", {
      headers: { authorization: `Bearer ${this.key}` },
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error("Resend sender verification unavailable.");
    }
    const result: { data?: { name: string; status: string }[] } = await response
      .json();
    const domain = this.from!.split("@")[1].toLowerCase();
    if (
      !Array.isArray(result.data) ||
      !result.data.some((item) =>
        item.name?.toLowerCase() === domain && item.status === "verified"
      )
    ) {
      throw new Error("Resend sender domain is not verified.");
    }
  }
  async send(email: string, code: string, key: string): Promise<void> {
    if (!this.ready) {
      throw new Error("Verified reward sender is not configured.");
    }
    const response = await this.fetcher("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.key}`,
        "content-type": "application/json",
        "idempotency-key": `reward-claim:${key}`,
      },
      body: JSON.stringify({
        from: this.from,
        to: [email],
        subject: "Your QBot4K Kickstarter reward",
        text:
          `Your collected Kickstarter reward is ready to claim.\n\nSign in at ${this.origin}/billing/rewards and enter this one-use code:\n\n${code}\n\nThe code expires in seven days. Choose the organization you own. Claiming stops any existing QBot4K subscription from renewing; fixed-term access begins after confirmed paid time and does not automatically subscribe you at expiry.\n\nIf you did not back this campaign, contact support and do not share this code.`,
      }),
      signal: AbortSignal.timeout(15_000),
    });
    await response.body?.cancel();
    if (!response.ok) throw new Error("Reward email delivery failed.");
  }
}
