import { assertEquals } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { PostgresDatabase } from "../src/data/database.ts";
import { withOperationalService } from "../src/data/operations.ts";

const databaseUrl = Deno.env.get("QBOT_TEST_POSTGRES_URL");

Deno.test({
  name: "PostgreSQL provider leases are exclusive and recoverable",
  ignore: !databaseUrl,
  async fn() {
    const fixtureId = crypto.randomUUID();
    const sql = postgres(databaseUrl!, { max: 2 });
    const database = new PostgresDatabase(databaseUrl!, { maxConnections: 4 });
    let organizationId: number | null = null;
    try {
      const [organization] = await sql`
        INSERT INTO organizations(name,slug)
        VALUES (${`Provider lease ${fixtureId}`},${`provider-${fixtureId}`})
        RETURNING id
      `;
      organizationId = Number(organization.id);
      const [workspace] = await sql`
        INSERT INTO workspaces(organization_id,name,slug)
        VALUES (${organizationId},${"Provider lease"},${`provider-${fixtureId}`})
        RETURNING id
      `;
      const [community] = await sql`
        INSERT INTO communities(workspace_id,name,slug)
        VALUES (${
        Number(workspace.id)
      },${"Provider lease"},${`provider-${fixtureId}`})
        RETURNING id
      `;
      const installations = await sql`
        INSERT INTO community_installations(
          community_id,platform,external_community_id,display_name,status
        ) VALUES
          (${
        Number(community.id)
      },'discord',${`discord-${fixtureId}`},'Discord fixture','active'),
          (${
        Number(community.id)
      },'twitch',${`twitch-${fixtureId}`},'Twitch fixture','active')
        RETURNING id,platform
      `;
      const discordId = Number(
        installations.find((row) => row.platform === "discord")!.id,
      );
      const twitchId = Number(
        installations.find((row) => row.platform === "twitch")!.id,
      );
      await sql`
        INSERT INTO installation_runtime_leases(installation_id,owner_runtime)
        VALUES (${discordId},'deno'),(${twitchId},'deno')
      `;

      const leases = database.providerOwnershipLease();
      assertEquals(await leases.installations("discord"), [discordId]);
      assertEquals(await leases.installations("twitch"), [twitchId]);
      const contenders = await Promise.all([
        leases.acquire(discordId, `discord-a-${fixtureId}`, 60),
        leases.acquire(discordId, `discord-b-${fixtureId}`, 60),
      ]);
      assertEquals(contenders.filter(Boolean).length, 1);
      const winner = contenders[0]
        ? `discord-a-${fixtureId}`
        : `discord-b-${fixtureId}`;
      const loser = contenders[0]
        ? `discord-b-${fixtureId}`
        : `discord-a-${fixtureId}`;
      assertEquals(await leases.owns(discordId, winner), true);
      assertEquals(await leases.owns(discordId, loser), false);
      assertEquals(await leases.acquire(twitchId, loser, 60), true);
      assertEquals(await leases.release(discordId, loser), false);
      assertEquals(await leases.release(discordId, winner), true);
      assertEquals(await leases.acquire(discordId, loser, 60), true);

      await sql`
        UPDATE installation_runtime_leases
           SET lease_expires_at='2000-01-01T00:00:00+00:00'
         WHERE installation_id=${discordId}
      `;
      assertEquals(await leases.owns(discordId, loser), false);
      assertEquals(await leases.acquire(discordId, winner, 60), true);
      assertEquals(await leases.release(discordId, winner), true);

      await withOperationalService(
        databaseUrl!,
        (service) => service.transferInstallationOwnership(discordId, "python"),
      );
      assertEquals(await leases.installations("discord"), []);
      assertEquals(await leases.acquire(discordId, winner, 60), false);
      const [handoffAudit] = await sql`
        SELECT payload_json::jsonb AS payload FROM audit_log
         WHERE action_type='provider_installation.ownership_transferred'
           AND entity_id=${discordId}
         ORDER BY id DESC LIMIT 1
      `;
      assertEquals(handoffAudit.payload.previous_owner, "deno");
      assertEquals(handoffAudit.payload.owner_runtime, "python");
    } finally {
      if (organizationId !== null) {
        await sql`DELETE FROM organizations WHERE id=${organizationId}`;
      }
      await database.close();
      await sql.end();
    }
  },
});
