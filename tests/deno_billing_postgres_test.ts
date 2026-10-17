import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { PostgresBillingRepository } from "../src/data/billing_repository.ts";
import { PostgresBackerRepository } from "../src/data/backer_repository.ts";
import { DEFAULT_IMPORT_COLUMNS } from "../src/domain/backer_rewards.ts";
import { PostgresProcessingJobRepository } from "../src/jobs/jobs.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
} from "../src/data/database.ts";
import { MIGRATION_NAMES, OperationalService } from "../src/data/operations.ts";
import { OperatorAuthRepository } from "../src/data/repository.ts";
import { PostgresCommandExecutionRepository } from "../src/domain/command_execution.ts";
import { PostgresCommandRegistry } from "../src/web/web_commands.ts";
import { PostgresIntegrationRepository } from "../src/web/web_integrations.ts";
import {
  BillingRequestError,
  PostgresStripeBillingRepository,
} from "../src/data/stripe_billing_repository.ts";
import type {
  CheckoutSession,
  PaidSubscription,
  StripeBillingGateway,
} from "../src/providers/stripe_billing.ts";

const databaseUrl = Deno.env.get("QBOT_TEST_POSTGRES_URL");

function queryConnection(
  sql: Pick<ReturnType<typeof postgres>, "unsafe">,
): DatabaseConnection {
  const connection: DatabaseConnection = {
    query: async (
      statement: string,
      parameters: readonly DatabaseParameter[] = [],
    ) =>
      (await sql.unsafe(statement, [...parameters])).map((row) => ({ ...row })),
    transaction: (callback) => callback(connection),
  };
  return connection;
}

Deno.test({
  name:
    "PostgreSQL billing migrations, ownership, grants and signup are isolated",
  ignore: !databaseUrl,
  async fn() {
    const schema = `billing_fixture_${crypto.randomUUID().replaceAll("-", "")}`;
    const parsedUrl = new URL(databaseUrl!);
    const password = parsedUrl.password
      ? decodeURIComponent(parsedUrl.password)
      : () => "";
    const admin = postgres(databaseUrl!, {
      max: 1,
      password,
      onnotice: () => {},
    });
    const sql = postgres(databaseUrl!, {
      max: 2,
      password,
      onnotice: () => {},
      connection: { search_path: schema },
    });
    try {
      await admin`CREATE SCHEMA ${admin(schema)}`;
      const connection: DatabaseConnection = {
        ...queryConnection(sql),
        transaction: <Result>(
          callback: (connection: DatabaseConnection) => Promise<Result>,
        ) =>
          sql.begin((transaction) =>
            callback(queryConnection(transaction))
          ) as Promise<Result>,
      };
      const schemaSql = await Deno.readTextFile(
        new URL("../src/postgres_schema.sql", import.meta.url),
      );
      const operations = new OperationalService(connection, schemaSql);
      await operations.migrate();
      await sql`ALTER TABLE simple_command_definitions DROP COLUMN community_id`;
      await sql`ALTER TABLE simple_command_definitions ADD PRIMARY KEY(command_name)`;
      await sql`DELETE FROM schema_migrations WHERE version=${MIGRATION_NAMES.length}`;
      await sql`INSERT INTO simple_command_definitions(command_name,response_template)
        VALUES ('legacy','Legacy response')`;
      await operations.migrate();
      await operations.migrate();
      assertEquals(
        (await sql`SELECT community_id FROM simple_command_definitions WHERE command_name='legacy'`)[
          0
        ].community_id,
        1,
      );
      const [{ count: migrationCount }] =
        await sql`SELECT count(*) FROM schema_migrations`;
      assertEquals(Number(migrationCount), MIGRATION_NAMES.length);
      await verifyStripeBilling(connection);
      await verifyBackerRewards(connection);

      const auth = new OperatorAuthRepository(connection);
      const login = await auth.completeLogin({
        userId: "billing-fixture",
        username: "Billing fixture",
        guildIds: [],
        permissions: {},
        guildNames: {},
        ownedGuildIds: [],
      }, "admin");
      assertEquals(login.memberships, []);
      const repository = new PostgresBillingRepository(connection);
      assertEquals(
        await repository.organizationAllows(
          login.operatorId,
          1,
          "billing.manage",
        ),
        false,
      );
      assertEquals(
        await repository.platformAllows(login.operatorId, "customers.read"),
        false,
      );
      const organization = await repository.createOrganization(
        login.operatorId,
        "Billing fixture",
        "billing-fixture",
        login.sessionVersion,
      );
      assertEquals(
        await repository.organizationAllows(
          login.operatorId,
          organization.organizationId,
          "billing.manage",
        ),
        true,
      );
      assertEquals(
        await repository.platformAllows(login.operatorId, "staff.manage"),
        false,
      );
      assertEquals(
        (await repository.entitlement(organization.organizationId)).active,
        false,
      );
      assertEquals(
        (await auth.resolveSession(login.operatorId))?.sessionVersion,
        login.sessionVersion + 1,
      );
      assertEquals(
        (await auth.resolveSession(login.operatorId))?.memberships.map((
          membership,
        ) => membership.id),
        [organization.communityId],
      );
      await verifyOwnerCommands(
        connection,
        login.operatorId,
        organization.communityId,
      );

      await assertRejects(() =>
        repository.createOrganization(
          login.operatorId,
          "Stale",
          "stale",
          login.sessionVersion,
        )
      );
      const signup = await auth.completeLogin({
        userId: "signup-fixture",
        username: "Signup fixture",
        guildIds: [],
        permissions: {},
        guildNames: {},
        ownedGuildIds: [],
      }, "viewer");
      const signups = await Promise.allSettled([
        repository.createOrganization(
          signup.operatorId,
          "Signup A",
          "signup-a",
          signup.sessionVersion,
        ),
        repository.createOrganization(
          signup.operatorId,
          "Signup B",
          "signup-b",
          signup.sessionVersion,
        ),
      ]);
      assertEquals(
        signups.filter((result) => result.status === "fulfilled").length,
        1,
      );
      assertEquals(
        (await auth.resolveSession(signup.operatorId))?.memberships.length,
        1,
      );
      assertEquals(
        await repository.platformAllows(signup.operatorId, "staff.manage"),
        false,
      );

      const competing = await Promise.allSettled([
        repository.createOrganization(
          login.operatorId,
          "Collision",
          "collision",
        ),
        repository.createOrganization(
          login.operatorId,
          "Collision",
          "collision",
        ),
      ]);
      assertEquals(
        competing.filter((result) => result.status === "fulfilled").length,
        1,
      );
      const [{ count: organizationCount }] =
        await sql`SELECT count(*) FROM organizations WHERE slug='collision'`;
      assertEquals(Number(organizationCount), 1);

      await sql`ALTER TABLE organization_billing_audit ADD CONSTRAINT fixture_reject_creation CHECK(action <> 'organization.created') NOT VALID`;
      await assertRejects(() =>
        repository.createOrganization(login.operatorId, "Rollback", "occupied")
      );
      const [{ count: rolledBack }] =
        await sql`SELECT count(*) FROM organizations WHERE slug='occupied'`;
      assertEquals(Number(rolledBack), 0);
      await sql`ALTER TABLE organization_billing_audit DROP CONSTRAINT fixture_reject_creation`;

      await sql`
        INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,starts_at,ends_at)
        VALUES (${organization.organizationId},'kickstarter','campaign:backer',1,'fixed_term','2026-10-01T00:00:00Z','2027-01-01T00:00:00Z')
      `;
      const now = new Date("2026-10-17T00:00:00.000Z");
      assertEquals(
        (await repository.entitlement(organization.organizationId, now)).active,
        true,
      );
      assertEquals((await repository.entitlement(1, now)).active, false);
      await assertRejects(() =>
        sql`
        INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,starts_at,ends_at)
        VALUES (1,'kickstarter','campaign:backer',1,'fixed_term','2026-10-01T00:00:00Z','2027-01-01T00:00:00Z')
      `
      );
      await assertRejects(() =>
        sql`
        INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,starts_at)
        VALUES (${organization.organizationId},'stripe','invalid-lifetime',1,'lifetime','2026-10-01T00:00:00Z')
      `
      );
      await assertRejects(() =>
        sql`
        INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,starts_at)
        VALUES (${organization.organizationId},'manual','invalid-term',1,'fixed_term','2026-10-01T00:00:00Z')
      `
      );

      await operations.migrate();
      assertEquals(
        (await repository.entitlement(organization.organizationId, now)).active,
        true,
      );
      const [{ count: roleCount }] =
        await sql`SELECT count(*) FROM organization_operator_roles WHERE organization_id=${organization.organizationId}`;
      assertEquals(Number(roleCount), 1);
      await sql`UPDATE operator_accounts SET status='disabled' WHERE id=${login.operatorId}`;
      assertEquals(
        await repository.organizationAllows(
          login.operatorId,
          organization.organizationId,
          "billing.manage",
        ),
        false,
      );
      await assertRejects(
        () =>
          repository.createOrganization(
            login.operatorId,
            "Disabled",
            "disabled",
          ),
        TypeError,
      );
      await sql`UPDATE organizations SET status='suspended' WHERE id=${organization.organizationId}`;
      assertEquals(
        (await repository.entitlement(organization.organizationId, now)).active,
        false,
      );
      await sql`UPDATE organizations SET status='active' WHERE id=${organization.organizationId}`;
      await sql`UPDATE organization_access_grants SET status='revoked' WHERE organization_id=${organization.organizationId}`;
      assertEquals(
        (await repository.entitlement(organization.organizationId, now)).active,
        false,
      );
      await verifyInstallationPolicy(connection);
    } finally {
      await sql.end();
      try {
        await admin`DROP SCHEMA IF EXISTS ${admin(schema)} CASCADE`;
      } finally {
        await admin.end();
      }
    }
  },
});

async function verifyInstallationPolicy(connection: DatabaseConnection) {
  const login = await new OperatorAuthRepository(connection).completeLogin({
    userId: "connection-owner",
    username: "Connection owner",
    guildIds: [],
    permissions: {},
    guildNames: {},
    ownedGuildIds: [],
  }, "admin");
  const billing = new PostgresBillingRepository(connection);
  const { communityId } = await billing.createOrganization(
    login.operatorId,
    "Connection policy",
    "connection-policy",
  );
  const repository = new PostgresIntegrationRepository(connection);
  const discordState = (guildId: string, targetCommunity = communityId) => ({
    nonce: crypto.randomUUID(),
    operator_id: String(login.operatorId),
    community_id: targetCommunity,
    guild_id: guildId,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  const startDiscord = async (state: ReturnType<typeof discordState>) => {
    await connection.query(
      `INSERT INTO operator_discord_guild_permissions(operator_id,guild_id,permissions)
       VALUES ($1,$2,8) ON CONFLICT(operator_id,guild_id) DO UPDATE SET permissions=8`,
      [login.operatorId, state.guild_id],
    );
    await repository.createDiscordIntent({
      communityId: state.community_id,
      operatorId: login.operatorId,
      guildId: state.guild_id,
      pilotInviteCode: "",
      state,
    });
  };
  const discordStates = ["policy-1", "policy-2", "policy-3", "policy-4"].map(
    (guildId) => discordState(guildId),
  );
  for (const state of discordStates) await startDiscord(state);
  const discordResults = await Promise.allSettled(
    discordStates.map((state) => repository.completeDiscordIntent(state)),
  );
  assertEquals(
    discordResults.filter((result) => result.status === "fulfilled").length,
    3,
  );
  const deniedIndex = discordResults.findIndex((result) =>
    result.status === "rejected"
  );
  const acceptedIndex = discordResults.findIndex((result) =>
    result.status === "fulfilled"
  );
  const denied = discordStates[deniedIndex];
  const accepted = discordStates[acceptedIndex];
  await assertRejects(
    () => startDiscord(discordState("policy-5")),
    TypeError,
    "connection limit",
  );
  const reconnect = discordState(accepted.guild_id);
  await startDiscord(reconnect);
  await repository.completeDiscordIntent(reconnect);
  const reactivate = discordState(accepted.guild_id);
  await startDiscord(reactivate);
  const [installation] = await connection.query(
    "SELECT id FROM community_installations WHERE platform='discord' AND external_community_id=$1",
    [accepted.guild_id],
  );
  await repository.revoke(
    communityId,
    login.operatorId,
    Number(installation.id),
  );
  await repository.completeDiscordIntent(denied);
  await assertRejects(
    () => repository.completeDiscordIntent(reactivate),
    TypeError,
    "connection limit",
  );

  const twitchState = (broadcasterLogin: string) => ({
    nonce: crypto.randomUUID(),
    operator_id: String(login.operatorId),
    community_id: communityId,
    broadcaster_login: broadcasterLogin,
    scopes: ["chat:read"],
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  const startTwitch = (state: ReturnType<typeof twitchState>) =>
    repository.createTwitchIntent({
      communityId,
      operatorId: login.operatorId,
      broadcasterLogin: state.broadcaster_login,
      scopes: state.scopes,
      state,
    });
  const completeTwitch = (state: ReturnType<typeof twitchState>) =>
    repository.completeTwitchIntent(
      state,
      {
        accessToken: "test-access",
        refreshToken: null,
        scopes: state.scopes,
        broadcasterLogin: state.broadcaster_login,
        broadcasterId: `id-${state.broadcaster_login}`,
      },
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
      false,
    );
  const twitchStates = [
    twitchState("policy-first"),
    twitchState("policy-second"),
  ];
  for (const state of twitchStates) await startTwitch(state);
  const twitchResults = await Promise.allSettled(
    twitchStates.map(completeTwitch),
  );
  assertEquals(
    twitchResults.filter((result) => result.status === "fulfilled").length,
    1,
  );
  const twitchAccepted = twitchStates[
    twitchResults.findIndex((result) => result.status === "fulfilled")
  ];
  const twitchDenied = twitchStates[
    twitchResults.findIndex((result) => result.status === "rejected")
  ];
  await assertRejects(
    () => startTwitch(twitchState("policy-third")),
    TypeError,
    "connection limit",
  );
  const twitchReconnect = twitchState(twitchAccepted.broadcaster_login);
  await startTwitch(twitchReconnect);
  await completeTwitch(twitchReconnect);
  const twitchInstallations = await connection.query(
    "SELECT id FROM community_installations WHERE community_id=$1 AND platform='twitch'",
    [communityId],
  );
  assertEquals(twitchInstallations.length, 1);

  for (const staffRole of ["support", "billing", "admin"]) {
    await connection.query(
      `INSERT INTO platform_staff_roles(operator_id,role) VALUES ($1,$2)
       ON CONFLICT(operator_id) DO UPDATE SET role=EXCLUDED.role`,
      [login.operatorId, staffRole],
    );
    const staffDiscord = discordState(`staff-${staffRole}`);
    await startDiscord(staffDiscord);
    await repository.completeDiscordIntent(staffDiscord);
    const staffTwitch = twitchState(`staff-${staffRole}`);
    await startTwitch(staffTwitch);
    await completeTwitch(staffTwitch);
  }
  await completeTwitch(twitchDenied);
  const suspended = twitchState("policy-suspended");
  await startTwitch(suspended);
  await connection.query(
    "UPDATE operator_accounts SET status='disabled' WHERE id=$1",
    [login.operatorId],
  );
  await assertRejects(
    () => completeTwitch(suspended),
    TypeError,
    "not authorized",
  );
  await connection.query(
    "UPDATE operator_accounts SET status='active' WHERE id=$1",
    [login.operatorId],
  );
  await connection.query(
    "DELETE FROM platform_staff_roles WHERE operator_id=$1",
    [login.operatorId],
  );
  await assertRejects(
    () => completeTwitch(suspended),
    TypeError,
    "connection limit",
  );
  await assertRejects(
    () => startDiscord(discordState("policy-over-limit")),
    TypeError,
    "connection limit",
  );

  await connection.query(
    "INSERT INTO platform_staff_roles(operator_id,role) VALUES ($1,'admin')",
    [login.operatorId],
  );
  const permissionRevoked = discordState("policy-permission-revoked");
  await startDiscord(permissionRevoked);
  await connection.query(
    "UPDATE operator_discord_guild_permissions SET permissions=0 WHERE operator_id=$1 AND guild_id=$2",
    [login.operatorId, permissionRevoked.guild_id],
  );
  await assertRejects(
    () => repository.completeDiscordIntent(permissionRevoked),
    TypeError,
    "not authorized",
  );
  await connection.query(
    "UPDATE operator_community_roles SET role='viewer' WHERE operator_id=$1 AND community_id=$2",
    [login.operatorId, communityId],
  );
  await assertRejects(
    () => completeTwitch(suspended),
    TypeError,
    "not authorized",
  );
  await connection.query(
    "UPDATE operator_community_roles SET role='owner' WHERE operator_id=$1 AND community_id=$2",
    [login.operatorId, communityId],
  );
  const second = await billing.createOrganization(
    login.operatorId,
    "Other connections",
    "other-connections",
  );
  const firstTenant = discordState("policy-shared");
  const secondTenant = discordState("policy-shared", second.communityId);
  await startDiscord(firstTenant);
  await startDiscord(secondTenant);
  await repository.completeDiscordIntent(firstTenant);
  await assertRejects(
    () => repository.completeDiscordIntent(secondTenant),
    TypeError,
    "another community",
  );
  const [unchanged] = await connection.query(
    "SELECT community_id FROM community_installations WHERE platform='discord' AND external_community_id='policy-shared'",
  );
  assertEquals(Number(unchanged.community_id), communityId);
}

async function verifyStripeBilling(connection: DatabaseConnection) {
  const login = await new OperatorAuthRepository(connection).completeLogin({
    userId: "stripe-owner",
    username: "Stripe owner",
    guildIds: [],
    permissions: {},
    guildNames: {},
    ownedGuildIds: [],
  }, "viewer");
  const ownership = new PostgresBillingRepository(connection);
  const { organizationId } = await ownership.createOrganization(
    login.operatorId,
    "Stripe fixture",
    "stripe-fixture",
  );
  let snapshots: readonly PaidSubscription[] = [];
  let failRead = false;
  let failCheckout = true;
  let sessionStatus = "open";
  const created = new Map<string, CheckoutSession>();
  const customerKeys = new Set<string>();
  const provider: StripeBillingGateway = {
    environment: "live",
    verify: () => Promise.reject(new Error("unused")),
    customerForCharge: () => Promise.resolve("cus_fixture"),
    validatePrice: () => Promise.resolve(),
    createCustomer: (_organization, key) => {
      customerKeys.add(key);
      return Promise.resolve("cus_fixture");
    },
    createCheckout: (_customer, _cadence, _origin, _organization, key) => {
      const session = created.get(key) ??
        {
          id: `cs_${created.size}`,
          url: `https://checkout.stripe.com/c/pay/${created.size}`,
          status: "open",
          expiresAt: 9999999999,
        };
      created.set(key, session);
      if (failCheckout) {
        failCheckout = false;
        return Promise.reject(
          new Error("Simulated lost response after Stripe created session"),
        );
      }
      return Promise.resolve(session);
    },
    checkout: (id) =>
      Promise.resolve({
        ...[...created.values()].find((session) => session.id === id)!,
        status: sessionStatus,
      }),
    subscriptions: () =>
      failRead
        ? Promise.reject(new Error("Stripe unavailable"))
        : Promise.resolve(snapshots),
  };
  const repository = new PostgresStripeBillingRepository(connection, provider);
  const purchase = () =>
    repository.checkout(
      login.operatorId,
      organizationId,
      "monthly",
      "https://qbot.example",
    );
  await assertRejects(
    () =>
      repository.checkout(
        login.operatorId,
        1,
        "monthly",
        "https://qbot.example",
      ),
    BillingRequestError,
  );
  await assertRejects(purchase);
  const checkoutResults = await Promise.all([purchase(), purchase()]);
  assertEquals(checkoutResults[0], checkoutResults[1]);
  assertEquals(created.size, 1);
  assertEquals(customerKeys.size, 1);
  await assertRejects(
    () =>
      repository.checkout(
        login.operatorId,
        organizationId,
        "annual",
        "https://qbot.example",
      ),
    BillingRequestError,
  );
  sessionStatus = "expired";
  await purchase();
  assertEquals(created.size, 2);
  sessionStatus = "open";
  const startsAt = new Date(Date.now() - 60_000).toISOString();
  const endsAt = new Date(Date.now() + 86_400_000).toISOString();
  snapshots = [{ id: "sub_fixture", status: "active", startsAt, endsAt }];
  const event = {
    id: "evt_paid",
    type: "invoice.paid",
    customerId: "cus_fixture",
    livemode: true,
  };
  await repository.receive(event);
  await repository.receive(event);
  assertEquals(
    Number(
      (await connection.query(
        "SELECT count(*) AS total FROM stripe_event_inbox WHERE event_id='evt_paid'",
      ))[0].total,
    ),
    1,
  );
  assertEquals((await ownership.entitlement(organizationId)).active, false);
  await repository.processPending();
  assertEquals((await ownership.entitlement(organizationId)).active, true);
  assertEquals((await ownership.entitlement(1)).active, false);
  assertEquals(
    (await connection.query(
      "SELECT status FROM stripe_event_inbox WHERE event_id='evt_paid'",
    ))[0].status,
    "processed",
  );
  await assertRejects(purchase, BillingRequestError);
  const renewedEnd = new Date(Date.parse(endsAt) + 31 * 86_400_000)
    .toISOString();
  snapshots = [{
    id: "sub_fixture",
    status: "active",
    startsAt,
    endsAt: renewedEnd,
  }];
  await repository.receive({ ...event, id: "evt_renewal" });
  await repository.processPending();
  await repository.receive({
    ...event,
    id: "evt_old",
    type: "customer.subscription.created",
  });
  await repository.processPending();
  assertEquals(
    (await ownership.entitlement(organizationId)).endsAt,
    renewedEnd,
  );
  snapshots = [{
    id: "sub_fixture",
    status: "canceled",
    startsAt,
    endsAt: renewedEnd,
  }];
  await repository.receive({
    ...event,
    id: "evt_canceled",
    type: "customer.subscription.deleted",
  });
  await repository.processPending();
  assertEquals(
    (await ownership.entitlement(organizationId)).endsAt,
    renewedEnd,
  );
  await assertRejects(purchase, BillingRequestError);
  failRead = true;
  await repository.receive({ ...event, id: "evt_retry" });
  await repository.processPending();
  assertEquals(
    (await connection.query(
      "SELECT status FROM stripe_event_inbox WHERE event_id='evt_retry'",
    ))[0].status,
    "failed",
  );
  assertEquals((await ownership.entitlement(organizationId)).active, true);
  failRead = false;
  await connection.query(
    "UPDATE stripe_event_inbox SET next_attempt_at=CURRENT_TIMESTAMP WHERE event_id='evt_retry'",
  );
  await repository.processPending();
  assertEquals(
    (await connection.query(
      "SELECT status FROM stripe_event_inbox WHERE event_id='evt_retry'",
    ))[0].status,
    "processed",
  );
  await repository.receive({
    ...event,
    id: "evt_unknown",
    customerId: "cus_unknown",
  });
  await repository.processPending();
  assertEquals(
    (await connection.query(
      "SELECT status FROM stripe_event_inbox WHERE event_id='evt_unknown'",
    ))[0].status,
    "quarantined",
  );
  await assertRejects(
    () =>
      repository.receive({ ...event, id: "evt_wrongmode", livemode: false }),
    BillingRequestError,
  );
  await connection.query(
    "INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,starts_at) VALUES ($1,'kickstarter','stripe-fixture-lifetime',1,'lifetime',$2)",
    [organizationId, startsAt],
  );
  snapshots = [{
    id: "sub_fixture",
    status: "canceled",
    startsAt: null,
    endsAt: null,
  }];
  await repository.receive({
    ...event,
    id: "evt_refund",
    customerId: null,
    chargeId: "ch_fixture",
    type: "charge.dispute.created",
  });
  await repository.processPending();
  const entitlement = await ownership.entitlement(organizationId);
  assertEquals(entitlement.grants.length, 1);
  assertEquals(entitlement.grants[0].source, "kickstarter");
  await assertRejects(purchase, BillingRequestError);
  await connection.query(
    "UPDATE organization_access_grants SET status='revoked' WHERE organization_id=$1",
    [organizationId],
  );
  sessionStatus = "complete";
  await purchase();
  assertEquals(created.size, 3);
  await connection.query(
    "INSERT INTO organization_stripe_billing(organization_id,environment,customer_id,customer_key,checkout_key) VALUES ($1,'test','cus_sandbox','sandbox','sandbox')",
    [organizationId],
  );
  snapshots = [{ id: "sub_sandbox", status: "active", startsAt, endsAt }];
  const sandbox = new PostgresStripeBillingRepository(connection, {
    ...provider,
    environment: "test",
  });
  await sandbox.receive({
    id: "evt_sandbox",
    type: "invoice.paid",
    customerId: "cus_sandbox",
    livemode: false,
  });
  await sandbox.processPending();
  assertEquals((await ownership.entitlement(organizationId)).active, false);
  assertEquals(
    (await sandbox.organizations(login.operatorId))[0].subscription_status,
    "active",
  );
  await connection.query(
    "INSERT INTO platform_staff_roles(operator_id,role) VALUES ($1,'admin')",
    [login.operatorId],
  );
  assertEquals(
    (await ownership.entitlement(organizationId)).complimentary,
    true,
  );
  assertEquals(
    (await repository.organizations(login.operatorId))[0].complimentary,
    true,
  );
  await assertRejects(
    purchase,
    BillingRequestError,
    "Complimentary staff access",
  );
  for (const view of ["organizations", "accounts", "audit"] as const) {
    assertEquals(
      Array.isArray(await ownership.adminRows(login.operatorId, view, 1)),
      true,
    );
  }
  await connection.query(
    "UPDATE operator_accounts SET status='disabled' WHERE id=$1",
    [login.operatorId],
  );
  assertEquals((await ownership.entitlement(organizationId)).active, false);
  assertEquals(
    await ownership.adminRows(login.operatorId, "accounts", 1),
    null,
  );
  await connection.query(
    "UPDATE operator_accounts SET status='active' WHERE id=$1",
    [login.operatorId],
  );
  await connection.query(
    "UPDATE organization_operator_roles SET role='member' WHERE operator_id=$1",
    [login.operatorId],
  );
  assertEquals((await ownership.entitlement(organizationId)).active, false);
  await connection.query(
    "UPDATE organization_operator_roles SET role='owner' WHERE operator_id=$1",
    [login.operatorId],
  );
  await connection.query(
    "DELETE FROM platform_staff_roles WHERE operator_id=$1",
    [login.operatorId],
  );
  assertEquals((await ownership.entitlement(organizationId)).active, false);
}

async function verifyBackerRewards(connection: DatabaseConnection) {
  const auth = new OperatorAuthRepository(connection);
  const staff = await auth.completeLogin({
    userId: "reward-staff",
    username: "Reward staff",
    guildIds: [],
    permissions: {},
    guildNames: {},
    ownedGuildIds: [],
  }, "admin");
  const owner = await auth.completeLogin({
    userId: "reward-owner",
    username: "Reward owner",
    guildIds: [],
    permissions: {},
    guildNames: {},
    ownedGuildIds: [],
  }, "admin");
  const ownership = new PostgresBillingRepository(connection);
  const organization = await ownership.createOrganization(
    owner.operatorId,
    "Reward organization",
    "reward-organization",
  );
  await connection.query(
    "INSERT INTO platform_staff_roles(operator_id,role) VALUES ($1,'admin')",
    [staff.operatorId],
  );
  const emails: { email: string; code: string }[] = [];
  let failProvider = true;
  let cancellations = 0;
  let failMail = true;
  let senderVerified = false;
  let failDiscountRemoval = true;
  const repository = new PostgresBackerRepository(connection, {
    ready: true,
    verifySender: () =>
      senderVerified
        ? Promise.resolve()
        : Promise.reject(new Error("Sender unverified")),
    claimCode: (nonce) => Promise.resolve(nonce.replaceAll("-", "").repeat(2)),
    send: (email, code) => {
      emails.push({ email, code });
      return failMail
        ? Promise.reject(new Error("Lost mail acknowledgement"))
        : Promise.resolve();
    },
  }, {
    environment: "live",
    prepareReward: () => {
      cancellations++;
      return failProvider
        ? Promise.reject(new Error("provider outage"))
        : Promise.resolve("2026-12-31T00:00:00.000Z");
    },
    removeBackerDiscount: () =>
      failDiscountRemoval
        ? Promise.reject(new Error("Stripe unavailable"))
        : Promise.resolve(),
  });
  const mapping = {
    campaignId: "test-campaign",
    rewards: { reward1: "sixMonths" },
  } as const;
  const header =
    "backer_id,email,reward_id,currency,collected_cents,refunded_cents,collection_status\n";
  const csv = header +
    "backer1,backer@example.test,reward1,USD,5000,0,collected";
  await assertRejects(
    () =>
      repository.campaign(
        owner.operatorId,
        mapping,
        DEFAULT_IMPORT_COLUMNS,
        true,
      ),
    BillingRequestError,
  );
  await assertRejects(
    () =>
      repository.campaign(
        staff.operatorId,
        mapping,
        DEFAULT_IMPORT_COLUMNS,
        true,
      ),
    Error,
    "Sender unverified",
  );
  senderVerified = true;
  await repository.campaign(
    staff.operatorId,
    mapping,
    DEFAULT_IMPORT_COLUMNS,
    true,
  );
  const preview = await repository.preview(
    staff.operatorId,
    mapping.campaignId,
    csv,
  );
  assertEquals(
    await repository.preview(staff.operatorId, mapping.campaignId, csv),
    preview,
  );
  await repository.process();
  assertEquals(emails.length, 0);
  await repository.confirm(staff.operatorId, preview);
  await repository.confirm(staff.operatorId, preview);
  await repository.process();
  assertEquals(emails.length, 1);
  assertEquals(emails[0].email, "backer@example.test");
  failMail = false;
  await connection.query(
    "UPDATE backer_records SET next_attempt_at=CURRENT_TIMESTAMP WHERE campaign_id=$1",
    [mapping.campaignId],
  );
  await repository.process();
  assertEquals(emails[1].code, emails[0].code);
  const [stored] = await connection.query(
    "SELECT claim_hash FROM backer_records WHERE campaign_id=$1",
    [mapping.campaignId],
  );
  assertEquals(stored.claim_hash === emails[0].code, false);
  await connection.query(
    "INSERT INTO organization_stripe_billing(organization_id,environment,customer_id,customer_key,checkout_key) VALUES ($1,'live','cus_rewards','reward-key','reward-checkout')",
    [organization.organizationId],
  );
  const [session] = await connection.query(
    "SELECT session_version FROM operator_accounts WHERE id=$1",
    [owner.operatorId],
  );
  await assertRejects(
    () =>
      repository.claim(
        owner.operatorId,
        1,
        organization.organizationId,
        emails[0].code,
      ),
    BillingRequestError,
  );
  await repository.claim(
    owner.operatorId,
    Number(session.session_version),
    organization.organizationId,
    emails[0].code,
  );
  await assertRejects(
    () =>
      repository.claim(
        owner.operatorId,
        Number(session.session_version),
        organization.organizationId,
        emails[0].code,
      ),
    BillingRequestError,
  );
  await repository.process();
  assertEquals(
    (await repository.benefits(owner.operatorId))[0].fulfillment_status,
    "failed",
  );
  assertEquals(
    (await ownership.entitlement(organization.organizationId)).active,
    false,
  );
  failProvider = false;
  const benefits = await repository.benefits(owner.operatorId);
  await repository.retry(staff.operatorId, Number(benefits[0].id));
  await repository.process();
  const fulfilled = (await repository.benefits(owner.operatorId))[0];
  assertEquals(fulfilled.fulfillment_status, "fulfilled");
  assertEquals(cancellations, 2);
  assertEquals(fulfilled.discount_percent, 20);
  await repository.confirm(staff.operatorId, preview);
  await repository.process();
  assertEquals(
    String((await repository.benefits(owner.operatorId))[0].access_ends_at),
    String(fulfilled.access_ends_at),
  );
  const invalid = await repository.preview(
    staff.operatorId,
    mapping.campaignId,
    header + "backer1,changed@example.test,reward1,USD,5000,0,collected",
  );
  await assertRejects(
    () => repository.confirm(staff.operatorId, invalid),
    BillingRequestError,
  );
  const refund = await repository.preview(
    staff.operatorId,
    mapping.campaignId,
    header + "backer1,backer@example.test,reward1,USD,5000,5000,collected",
  );
  await repository.confirm(staff.operatorId, refund);
  await repository.process();
  assertEquals(
    (await repository.benefits(owner.operatorId))[0].fulfillment_status,
    "failed",
  );
  assertEquals(
    (await connection.query(
      "SELECT status FROM organization_access_grants WHERE source='kickstarter' AND source_id=$1",
      [`backer:${fulfilled.id}`],
    ))[0].status,
    "revoked",
  );
  failDiscountRemoval = false;
  await repository.retry(staff.operatorId, Number(fulfilled.id));
  await repository.process();
  assertEquals(
    (await repository.benefits(owner.operatorId))[0].fulfillment_status,
    "revoked",
  );
  assertEquals(
    (await repository.benefits(owner.operatorId))[0].discount_percent,
    0,
  );
  assertEquals(
    (await ownership.entitlement(organization.organizationId)).active,
    false,
  );
  assertEquals(
    (await repository.overview(staff.operatorId, mapping.campaignId, 1)).backers
      .length,
    1,
  );
  await connection.query(
    "UPDATE platform_billing_policy SET enforce_paid_access=TRUE WHERE id=1",
  );
  assertEquals(
    (await connection.query("SELECT qbot_community_has_access($1) AS allowed", [
      organization.communityId,
    ]))[0].allowed,
    false,
  );
  const [leased] = await connection.query(
    "INSERT INTO processing_jobs(community_id,stage,job_type,idempotency_key,status,claimed_by,attempts) VALUES ($1,'action','fixture.paid-access','fixture.paid-access','running','paid-worker',1) RETURNING id",
    [organization.communityId],
  );
  const jobs = new PostgresProcessingJobRepository(connection);
  assertEquals(
    await jobs.deferUnpaid(Number(leased.id), "other-worker"),
    false,
  );
  assertEquals(await jobs.deferUnpaid(Number(leased.id), "paid-worker"), true);
  const [deferred] = await connection.query(
    "SELECT status,attempts,claimed_by FROM processing_jobs WHERE id=$1",
    [Number(leased.id)],
  );
  assertEquals([deferred.status, deferred.attempts, deferred.claimed_by], [
    "pending",
    0,
    null,
  ]);
  await connection.query(
    "UPDATE organizations SET beta_access_until=CURRENT_TIMESTAMP+INTERVAL '1 day' WHERE id=$1",
    [organization.organizationId],
  );
  assertEquals(
    (await connection.query("SELECT qbot_community_has_access($1) AS allowed", [
      organization.communityId,
    ]))[0].allowed,
    true,
  );
  await connection.query(
    "UPDATE processing_jobs SET status='running',claimed_by='paid-worker',attempts=1 WHERE id=$1",
    [Number(leased.id)],
  );
  assertEquals(await jobs.deferUnpaid(Number(leased.id), "paid-worker"), false);
  await connection.query(
    "UPDATE platform_billing_policy SET enforce_paid_access=FALSE WHERE id=1",
  );
  await assertRejects(
    () => repository.overview(owner.operatorId, mapping.campaignId, 1),
    BillingRequestError,
  );
  await repository.campaign(
    staff.operatorId,
    { ...mapping, campaignId: "disabled-campaign" },
    DEFAULT_IMPORT_COLUMNS,
    false,
  );
  const disabled = await repository.preview(
    staff.operatorId,
    "disabled-campaign",
    csv,
  );
  await repository.confirm(staff.operatorId, disabled);
  await repository.process();
  assertEquals(emails.length, 2);
  const newOwner = await auth.completeLogin({
    userId: "reward-only-owner",
    username: "Reward-only owner",
    guildIds: [],
    permissions: {},
    guildNames: {},
    ownedGuildIds: [],
  }, "viewer");
  const rewardOnly = await ownership.createOrganization(
    newOwner.operatorId,
    "Reward only",
    "reward-only",
  );
  await repository.activate(staff.operatorId, "disabled-campaign");
  await repository.process();
  const [newSession] = await connection.query(
    "SELECT session_version FROM operator_accounts WHERE id=$1",
    [newOwner.operatorId],
  );
  await repository.claim(
    newOwner.operatorId,
    Number(newSession.session_version),
    rewardOnly.organizationId,
    emails[2].code,
  );
  await repository.process();
  assertEquals(
    (await repository.benefits(newOwner.operatorId))[0].fulfillment_status,
    "fulfilled",
  );
  assertEquals(
    (await connection.query(
      "SELECT organization_id FROM organization_stripe_billing WHERE organization_id=$1",
      [rewardOnly.organizationId],
    )).length,
    0,
  );
  await connection.query(
    "UPDATE backer_records SET access_starts_at='2025-01-01',access_ends_at='2025-07-01' WHERE organization_id=$1",
    [rewardOnly.organizationId],
  );
  await connection.query(
    "UPDATE organization_access_grants SET starts_at='2025-01-01',ends_at='2025-07-01' WHERE organization_id=$1 AND source='kickstarter'",
    [rewardOnly.organizationId],
  );
  let discounted = false;
  const conversion = new PostgresStripeBillingRepository(connection, {
    environment: "live",
    verify: () => Promise.reject(new Error("unused")),
    customerForCharge: () => Promise.resolve(null),
    validatePrice: () => Promise.resolve(),
    createCustomer: () => Promise.resolve("cus_reward_only"),
    subscriptions: () => Promise.resolve([]),
    checkout: () => Promise.reject(new Error("unused")),
    createCheckout: (
      _customer,
      _cadence,
      _origin,
      _organization,
      _key,
      backerId,
    ) => {
      discounted = Boolean(backerId);
      return Promise.resolve({
        id: "cs_reward_only",
        url: "https://checkout.stripe.com/c/pay/reward-only",
        status: "open",
        expiresAt: 9999999999,
      });
    },
  });
  assertEquals(
    await conversion.checkout(
      newOwner.operatorId,
      rewardOnly.organizationId,
      "monthly",
      "https://example.test",
    ),
    "https://checkout.stripe.com/c/pay/reward-only",
  );
  assertEquals(discounted, true);
}

async function verifyOwnerCommands(
  connection: DatabaseConnection,
  operatorId: number,
  communityId: number,
) {
  const registry = new PostgresCommandRegistry(connection);
  const commands = new PostgresCommandExecutionRepository(connection);
  const [user] = await connection.query(
    "INSERT INTO users(primary_display_name) VALUES ('Owner') RETURNING id",
  );
  const [actor] = await connection.query(
    "INSERT INTO platform_accounts(user_id,platform,platform_user_id,username) VALUES ($1,'discord','billing-fixture','renamed-owner') RETURNING id",
    [Number(user.id)],
  );
  const run = async (
    contentRaw: string,
    targetCommunityId = communityId,
    platform = "discord",
  ) => {
    const [observation] = await connection.query(
      `INSERT INTO observations(platform,community_id,event_type,actor_platform_account_id,occurred_at)
       VALUES ($1,$2,'message.created',$3,CURRENT_TIMESTAMP::text) RETURNING id`,
      [platform, targetCommunityId, Number(actor.id)],
    );
    await commands.execute({
      observationId: Number(observation.id),
      communityId: targetCommunityId,
      platform,
      channelId: "owner-channel",
      username: "not-the-discord-id",
      contentRaw,
      isModerator: false,
      roleNames: [],
    });
    const [job] = await connection.query(
      "SELECT payload_json FROM processing_jobs WHERE observation_id=$1",
      [Number(observation.id)],
    );
    return job ? JSON.parse(String(job.payload_json)) : null;
  };
  await connection.query(
    "DELETE FROM operator_community_roles WHERE operator_id=$1",
    [operatorId],
  );
  const added = await run("!addcom docs Owner response");
  assertEquals(added.rendered_reply.embeds[0].description, "Created !docs.");
  await run("!editcom docs Updated response");
  assertEquals(
    (await registry.list(communityId)).simple.map((row) =>
      row.response_template
    ),
    ["Updated response"],
  );
  assertEquals((await registry.list(1)).simple.map((row) => row.command_name), [
    "legacy",
  ]);
  assertEquals(
    (await registry.list(communityId)).simple.some((row) =>
      row.command_name === "legacy"
    ),
    false,
  );
  assertEquals((await run("!docs")).rendered_reply.content, "Updated response");
  assertEquals(await run("!docs", 1), null);

  const denied = await run("!addcom docs Cross-tenant write", 1);
  assertEquals(
    denied.rendered_reply.embeds[0].description.includes(
      "requires owner or moderator",
    ),
    true,
  );
  assertEquals((await registry.list(1)).simple.length, 1);
  await registry.update(1, {
    record_type: "simple",
    command_name: "docs",
    response_template: "Default response",
    enabled: true,
  });
  assertEquals(
    (await run("!docs", 1)).rendered_reply.content,
    "Default response",
  );
  assertEquals((await run("!docs")).rendered_reply.content, "Updated response");
  await run("!alias shortcut docs");
  assertEquals(
    (await run("!shortcut")).rendered_reply.content,
    "Updated response",
  );
  assertEquals(await run("!shortcut", 1), null);
  await run("!delcom docs");
  assertEquals(
    (await run("!docs", 1)).rendered_reply.content,
    "Default response",
  );
  await registry.update(communityId, {
    record_type: "simple",
    command_name: "docs",
    response_template: "Dashboard response",
    enabled: true,
  });
  await registry.update(1, {
    record_type: "simple",
    action: "delete",
    command_name: "docs",
  });
  assertEquals(
    (await run("!docs")).rendered_reply.content,
    "Dashboard response",
  );

  await connection.query(
    "UPDATE operator_accounts SET status='disabled' WHERE id=$1",
    [operatorId],
  );
  assertEquals(
    (await run("!editcom docs Disabled")).rendered_reply.embeds[0].description
      .includes("requires owner or moderator"),
    true,
  );
  await connection.query(
    "UPDATE operator_accounts SET status='active' WHERE id=$1",
    [operatorId],
  );
  await connection.query(
    "INSERT INTO operator_permission_overrides(operator_id,community_id,permission,decision) VALUES ($1,$2,'commands.manage','deny')",
    [operatorId, communityId],
  );
  assertEquals(
    (await run("!editcom docs Overridden")).rendered_reply.embeds[0].description
      .includes("requires owner or moderator"),
    true,
  );
  await connection.query(
    "DELETE FROM operator_permission_overrides WHERE operator_id=$1",
    [operatorId],
  );
  await connection.query(
    "UPDATE organization_operator_roles SET role='billing_manager' WHERE operator_id=$1",
    [operatorId],
  );
  assertEquals(
    (await run("!editcom docs Billing manager")).rendered_reply.embeds[0]
      .description.includes("requires owner or moderator"),
    true,
  );
  await connection.query(
    "INSERT INTO operator_community_roles(operator_id,community_id,role) VALUES ($1,$2,'owner')",
    [operatorId, communityId],
  );
  assertEquals(
    (await run("!editcom docs Community owner")).rendered_reply.embeds[0]
      .description,
    "Updated !docs.",
  );
  await connection.query(
    "UPDATE organization_operator_roles SET role='owner' WHERE operator_id=$1",
    [operatorId],
  );
  const twitchDenied = await run(
    "!editcom docs Wrong provider",
    communityId,
    "twitch",
  );
  assertEquals(
    twitchDenied.message.includes("requires owner or moderator"),
    true,
  );
  assertEquals((await run("!docs")).rendered_reply.content, "Community owner");
}
