import type { AnnouncementSender } from "../../jobs/announcement_dispatch.ts";
import type { TwitchIrcClient } from "./twitch_irc.ts";

export class TwitchAnnouncementSender implements AnnouncementSender {
  constructor(private readonly client: TwitchIrcClient) {}

  send(
    platform: string,
    _externalCommunityId: string,
    targetExternalId: string,
    body: string,
    _source: Readonly<Record<string, unknown>>,
  ): Promise<string> {
    if (platform.trim().toLocaleLowerCase() !== "twitch") {
      throw new TypeError(
        `unsupported Twitch announcement platform: ${platform}`,
      );
    }
    return this.client.sendMessage(targetExternalId, body);
  }
}
