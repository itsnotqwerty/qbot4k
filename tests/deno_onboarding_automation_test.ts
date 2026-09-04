import { assertEquals } from "jsr:@std/assert@1.0.14";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";
import {
  type OnboardingRoleGateway,
  PostgresOnboardingAutomation,
} from "../src/domain/onboarding_automation.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: Array<{
    sql: string;
    parameters: readonly DatabaseParameter[];
  }> = [];
  constructor(private readonly responses: Array<readonly DatabaseRow[]>) {}
  query(
    sql: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.responses.shift() ?? []);
  }
  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    return callback(this);
  }
}

class FakeRoles implements OnboardingRoleGateway {
  readonly assignments: string[][] = [];
  constructor(private readonly fail = false) {}
  assignRole(guildId: string, userId: string, roleId: string): Promise<void> {
    this.assignments.push([guildId, userId, roleId]);
    return this.fail
      ? Promise.reject(new Error("provider unavailable"))
      : Promise.resolve();
  }
}

Deno.test("newcomer roles use tenant installations and persist outcomes", async () => {
  const connection = new FakeConnection([
    [
      {
        community_id: 2,
        platform_user_id: "member-1",
        newcomer_role_id: "role-1",
        role_assignment_attempts: 0,
        external_community_id: "guild-2",
        capabilities_json: '["member_lifecycle"]',
      },
    ],
    [],
    [],
  ]);
  const roles = new FakeRoles();
  assertEquals(
    await new PostgresOnboardingAutomation(connection, roles)
      .dispatchNewcomerRoles(),
    1,
  );
  assertEquals(roles.assignments, [["guild-2", "member-1", "role-1"]]);
  assertEquals(
    connection.calls[0].sql.includes("FOR UPDATE OF m SKIP LOCKED"),
    true,
  );
  assertEquals(connection.calls[1].parameters, [1, 2, "member-1"]);
  assertEquals(
    connection.calls[2].parameters[0],
    "onboarding.newcomer_role_assigned",
  );
});

Deno.test("role assignment failures exhaust into failed state", async () => {
  const connection = new FakeConnection([[
    {
      community_id: 2,
      platform_user_id: "member-1",
      newcomer_role_id: "role-1",
      role_assignment_attempts: 2,
      external_community_id: "guild-2",
      capabilities_json: '["member_lifecycle"]',
    },
  ], []]);
  assertEquals(
    await new PostgresOnboardingAutomation(connection, new FakeRoles(true))
      .dispatchNewcomerRoles(),
    0,
  );
  assertEquals(connection.calls[1].parameters, [
    "failed",
    3,
    "provider unavailable",
    2,
    "member-1",
  ]);
});

Deno.test("newcomer roles refuse installations without capability", async () => {
  const connection = new FakeConnection([[
    {
      community_id: 2,
      platform_user_id: "member-1",
      newcomer_role_id: "role-1",
      role_assignment_attempts: 0,
      external_community_id: "guild-2",
      capabilities_json: "[]",
    },
  ], []]);
  const roles = new FakeRoles();
  assertEquals(
    await new PostgresOnboardingAutomation(connection, roles)
      .dispatchNewcomerRoles(),
    0,
  );
  assertEquals(roles.assignments, []);
  assertEquals(connection.calls[1].parameters, [1, 2, "member-1"]);
});

Deno.test("checkpoint reminders enqueue once and mark the member", async () => {
  const connection = new FakeConnection([
    [
      {
        community_id: 2,
        discord_installation_id: 4,
        platform_user_id: "member-1",
        username: "Member",
        welcome_channel_id: "welcome",
        checkpoint_reminder_template: "Reminder {mention}, hello {username}",
      },
    ],
    [{ id: 12 }],
    [],
    [],
    [],
  ]);
  assertEquals(
    await new PostgresOnboardingAutomation(connection, new FakeRoles())
      .queueCheckpointReminders(new Date("2026-09-04T12:00:00Z")),
    1,
  );
  assertEquals(connection.calls[0].parameters, [
    "2026-09-04T12:00:00.000Z",
    50,
  ]);
  assertEquals(
    connection.calls[0].sql.includes("FOR UPDATE OF m SKIP LOCKED"),
    true,
  );
  assertEquals(connection.calls[1].parameters?.slice(0, 5), [
    2,
    4,
    "welcome",
    "Reminder <@member-1>, hello Member",
    "onboarding-checkpoint-reminder:member-1",
  ]);
  assertEquals(connection.calls[2].parameters, [
    "2026-09-04T12:00:00.000Z",
    2,
    "member-1",
  ]);
});
