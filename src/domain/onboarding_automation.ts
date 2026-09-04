import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";

const ROLE_ASSIGNMENT_MAX_ATTEMPTS = 3;

export interface OnboardingRoleGateway {
  assignRole(guildId: string, userId: string, roleId: string): Promise<void>;
}

export interface OnboardingAutomationService {
  dispatchNewcomerRoles(limit?: number): Promise<number>;
  queueCheckpointReminders(now?: Date, limit?: number): Promise<number>;
}

export type CheckpointReminderService = Pick<
  OnboardingAutomationService,
  "queueCheckpointReminders"
>;

export class PostgresOnboardingAutomation
  implements OnboardingAutomationService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly roles?: OnboardingRoleGateway,
  ) {}

  async dispatchNewcomerRoles(limit = 20): Promise<number> {
    const roles = this.roles;
    if (!roles) {
      throw new TypeError("onboarding role gateway is required");
    }
    const boundedLimit = Math.max(1, Math.min(Math.trunc(limit), 200));
    return await this.connection.transaction(async (connection) => {
      const rows = await connection.query(
        `SELECT m.community_id,m.platform_user_id,m.newcomer_role_id,
                m.role_assignment_attempts,i.external_community_id,
                i.capabilities_json
           FROM community_onboarding_members m
           JOIN community_installations i
             ON i.id=m.discord_installation_id AND i.community_id=m.community_id
            AND i.platform='discord' AND i.status='active'
            AND EXISTS (SELECT 1 FROM installation_runtime_leases lease
              WHERE lease.installation_id=i.id AND lease.owner_runtime='deno'
                AND lease.lease_holder IS NOT NULL
                AND lease.lease_expires_at::timestamptz>CURRENT_TIMESTAMP)
          WHERE m.status='newcomer' AND m.role_assignment_status='pending'
            AND m.role_assignment_attempts<$1
          ORDER BY m.joined_at,m.community_id
          FOR UPDATE OF m SKIP LOCKED LIMIT $2`,
        [ROLE_ASSIGNMENT_MAX_ATTEMPTS, boundedLimit],
      );
      let assigned = 0;
      for (const row of rows) {
        const communityId = positiveInteger(row.community_id, "community_id");
        const userId = String(row.platform_user_id);
        const roleId = String(row.newcomer_role_id);
        const attempts = Number(row.role_assignment_attempts) + 1;
        if (!jsonArray(row.capabilities_json).includes("member_lifecycle")) {
          await connection.query(
            `UPDATE community_onboarding_members
                SET role_assignment_status='failed',role_assignment_attempts=$1,
                    role_assignment_error='member lifecycle capability is disabled',
                    updated_at=CURRENT_TIMESTAMP
              WHERE community_id=$2 AND platform_user_id=$3`,
            [attempts, communityId, userId],
          );
          continue;
        }
        try {
          await roles.assignRole(
            String(row.external_community_id),
            userId,
            roleId,
          );
        } catch (error) {
          await connection.query(
            `UPDATE community_onboarding_members
                SET role_assignment_status=$1,role_assignment_attempts=$2,
                    role_assignment_error=$3,updated_at=CURRENT_TIMESTAMP
              WHERE community_id=$4 AND platform_user_id=$5`,
            [
              attempts >= ROLE_ASSIGNMENT_MAX_ATTEMPTS ? "failed" : "pending",
              attempts,
              boundedError(error),
              communityId,
              userId,
            ],
          );
          continue;
        }
        await connection.query(
          `UPDATE community_onboarding_members
              SET role_assignment_status='assigned',role_assignment_attempts=$1,
                  role_assignment_error=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE community_id=$2 AND platform_user_id=$3`,
          [attempts, communityId, userId],
        );
        await audit(
          connection,
          "onboarding.newcomer_role_assigned",
          communityId,
          { platform_user_id: userId, role_id: roleId },
        );
        assigned += 1;
      }
      return assigned;
    });
  }

  async queueCheckpointReminders(
    now = new Date(),
    limit = 50,
  ): Promise<number> {
    if (Number.isNaN(now.getTime())) throw new TypeError("now is invalid");
    const dueAt = now.toISOString();
    const boundedLimit = Math.max(1, Math.min(Math.trunc(limit), 500));
    return await this.connection.transaction(async (connection) => {
      const rows = await connection.query(
        `SELECT m.community_id,m.discord_installation_id,m.platform_user_id,
                m.username,s.welcome_channel_id,s.checkpoint_reminder_template
           FROM community_onboarding_members m
           JOIN community_onboarding_settings s ON s.community_id=m.community_id
           JOIN community_installations i
             ON i.id=m.discord_installation_id AND i.community_id=m.community_id
            AND i.platform='discord' AND i.status='active'
          WHERE m.status='newcomer'
            AND m.checkpoint_due_at::timestamptz<=$1::timestamptz
            AND m.reminder_sent_at IS NULL
            AND s.checkpoint_reminder_enabled=1
          ORDER BY m.checkpoint_due_at,m.community_id
          FOR UPDATE OF m SKIP LOCKED LIMIT $2`,
        [dueAt, boundedLimit],
      );
      let queued = 0;
      for (const row of rows) {
        if (await this.queueReminder(connection, row, dueAt)) queued += 1;
      }
      return queued;
    });
  }

  private async queueReminder(
    connection: DatabaseConnection,
    row: DatabaseRow,
    dueAt: string,
  ): Promise<boolean> {
    const communityId = positiveInteger(row.community_id, "community_id");
    const installationId = positiveInteger(
      row.discord_installation_id,
      "installation_id",
    );
    const userId = String(row.platform_user_id);
    const body = String(row.checkpoint_reminder_template)
      .replaceAll("{mention}", `<@${userId}>`)
      .replaceAll("{username}", String(row.username));
    const inserted = (await connection.query(
      `INSERT INTO community_announcements(
         community_id,target_installation_id,platform,target_external_id,body,
         dedupe_key,source_json,status,scheduled_at,timezone
       ) VALUES ($1,$2,'discord',$3,$4,$5,$6,'scheduled',$7,'UTC')
       ON CONFLICT(community_id,dedupe_key) WHERE dedupe_key IS NOT NULL
       DO NOTHING RETURNING id`,
      [
        communityId,
        installationId,
        String(row.welcome_channel_id),
        body,
        `onboarding-checkpoint-reminder:${userId}`,
        JSON.stringify({
          type: "onboarding_checkpoint_reminder",
          user_id: userId,
        }),
        dueAt,
      ],
    ))[0];
    if (!inserted) return false;
    await connection.query(
      `UPDATE community_onboarding_members
          SET reminder_sent_at=$1,updated_at=CURRENT_TIMESTAMP
        WHERE community_id=$2 AND platform_user_id=$3
          AND reminder_sent_at IS NULL`,
      [dueAt, communityId, userId],
    );
    await audit(connection, "announcement.created", Number(inserted.id), {
      community_id: communityId,
      source: {
        type: "onboarding_checkpoint_reminder",
        user_id: userId,
      },
    }, "community_announcement");
    await audit(
      connection,
      "onboarding.checkpoint_reminder_queued",
      communityId,
      { platform_user_id: userId },
    );
    return true;
  }
}

function jsonArray(value: unknown): readonly string[] {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

async function audit(
  connection: DatabaseConnection,
  action: string,
  entityId: number,
  payload: Readonly<Record<string, unknown>>,
  entityType = "community",
): Promise<void> {
  await connection.query(
    `INSERT INTO audit_log(
       actor_type,action_type,entity_type,entity_id,payload_json
     ) VALUES ('system',$1,$2,$3,$4)`,
    [action, entityType, entityId, JSON.stringify(payload)],
  );
}

function positiveInteger(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new TypeError(`${name} is invalid`);
  }
  return parsed;
}

function boundedError(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(0, 500);
}
