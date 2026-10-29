import type { AnnouncementSender } from "../../jobs/announcement_dispatch.ts";
import type { DiscordApi } from "./discord_actions.ts";

export class DiscordAnnouncementSender implements AnnouncementSender {
  readonly platform = "discord";

  constructor(private readonly api: DiscordApi) {}

  async send(
    platform: string,
    externalCommunityId: string,
    targetExternalId: string,
    body: string,
    _source: Readonly<Record<string, unknown>>,
  ): Promise<string> {
    if (platform !== this.platform) {
      throw new TypeError(
        `unsupported Discord announcement platform: ${platform}`,
      );
    }
    const channel = await this.api.getChannel(targetExternalId);
    if (!channel || channel.guild_id !== externalCommunityId) {
      throw new TypeError(
        "Discord announcement channel does not belong to the target server",
      );
    }
    const message = await this.api.sendMessage(targetExternalId, {
      content: body,
      allowed_mentions: { parse: [] },
    });
    if (
      !message || typeof message !== "object" || !("id" in message) ||
      typeof message.id !== "string" || !message.id
    ) {
      throw new TypeError("Discord announcement response has no message ID");
    }
    return message.id;
  }
}
