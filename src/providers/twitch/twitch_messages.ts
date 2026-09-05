import type { DatabaseConnection } from "../../data/database.ts";
import { PermanentJobError, type ProcessingJob } from "../../jobs/jobs.ts";

export const TWITCH_MESSAGE_JOB_TYPE = "twitch.message.send";

export interface TwitchMessageSender {
  sendMessage(channel: string, message: string): Promise<unknown>;
}

export class PostgresTwitchMessageRepository {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly sender: TwitchMessageSender,
  ) {}

  async sendMessage(job: ProcessingJob): Promise<void> {
    if (job.jobType !== TWITCH_MESSAGE_JOB_TYPE) {
      throw new PermanentJobError(
        `unsupported Twitch message job: ${job.jobType}`,
      );
    }
    const channelId = String(job.payload.channel_id ?? "").trim()
      .toLocaleLowerCase();
    const message = String(job.payload.message ?? "").trim();
    if (!channelId || !message) {
      throw new PermanentJobError("Twitch message requires channel and body");
    }
    if (!job.observationId) {
      throw new PermanentJobError(
        "Twitch message action has no originating observation",
      );
    }
    const installation = (await this.connection.query(
      `SELECT installation.display_name,installation.capabilities_json
         FROM observations AS observation
         JOIN community_installations AS installation
           ON installation.id=observation.installation_id
          AND installation.community_id=observation.community_id
          AND installation.platform='twitch' AND installation.status='active'
          AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
            WHERE lease.installation_id=installation.id AND lease.owner_runtime='deno'
              AND lease.lease_holder IS NOT NULL
              AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
        WHERE observation.id=$1 AND observation.community_id=$2`,
      [job.observationId, job.communityId],
    ))[0];
    if (!installation) {
      throw new PermanentJobError(
        "Twitch message installation is not active for the job tenant",
      );
    }
    if (
      !String(installation.capabilities_json ?? "").includes("announcements")
    ) {
      throw new PermanentJobError("Twitch message capability is disabled");
    }
    await this.sender.sendMessage(
      String(installation.display_name || channelId),
      message,
    );
  }
}
