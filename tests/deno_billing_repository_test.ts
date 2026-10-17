import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import {
  OrganizationSignupConflict,
  PostgresBillingRepository,
} from "../src/data/billing_repository.ts";
import type {
  DatabaseConnection,
  DatabaseParameter,
  DatabaseRow,
} from "../src/data/database.ts";

class FakeConnection implements DatabaseConnection {
  readonly calls: { sql: string; parameters: readonly DatabaseParameter[] }[] =
    [];
  rows: (readonly DatabaseRow[])[] = [];
  outcome = "none";
  query(sql: string, parameters: readonly DatabaseParameter[] = []) {
    this.calls.push({ sql, parameters });
    return Promise.resolve(this.rows.shift() ?? []);
  }
  async transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    try {
      const result = await callback(this);
      this.outcome = "committed";
      return result;
    } catch (error) {
      this.outcome = "rolled_back";
      throw error;
    }
  }
}

Deno.test("organization billing requires explicit active membership, not community role", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  for (const role of ["owner", "billing_manager", "member", "admin", "*"]) {
    connection.rows = [[{ role }]];
    assertEquals(
      await repository.organizationAllows(42, 7, "billing.manage"),
      ["owner", "billing_manager"].includes(role),
    );
  }
  assertEquals(
    await repository.organizationAllows(42, 8, "billing.read"),
    false,
  );
  assertEquals(connection.calls.at(-1)?.parameters, [42, 8]);
  assertEquals(
    connection.calls[0].sql.includes("operator.status='active'"),
    true,
  );
  assertEquals(
    connection.calls[0].sql.includes("organization.status='active'"),
    true,
  );
  assertEquals(
    connection.calls[0].sql.includes("operator_community_roles"),
    false,
  );
});

Deno.test("platform roles do not inherit organization or community owner authority", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  for (const role of ["owner", "moderator", "support", "billing", "admin"]) {
    for (
      const capability of [
        "customers.read",
        "benefits.manage",
        "staff.manage",
      ] as const
    ) {
      connection.rows = [[{ role }]];
      const allowed = role === "admin" ||
        (role === "billing" && capability !== "staff.manage") ||
        (role === "support" && capability === "customers.read");
      assertEquals(await repository.platformAllows(42, capability), allowed);
    }
  }
  assertEquals(await repository.platformAllows(42, "customers.read"), false);
  assertEquals(connection.calls[0].parameters, [42]);
  assertEquals(connection.calls[0].sql.includes("platform_staff_roles"), true);
});

Deno.test("organization provisioning creates an isolated community without payment or staff grants", async () => {
  const connection = new FakeConnection();
  connection.rows = [[{ id: 42 }], [{ id: 7 }], [{ id: 8 }], [{ id: 9 }]];
  const result = await new PostgresBillingRepository(connection)
    .createOrganization(42, " Example ", "example");
  assertEquals(result, { organizationId: 7, communityId: 9 });
  assertEquals(connection.outcome, "committed");
  assertEquals(connection.calls[1].parameters, ["Example", "example"]);
  assertEquals(connection.calls[4].parameters, [7, 42]);
  assertEquals(connection.calls[5].parameters, [42, 9]);
  assertEquals(
    connection.calls[7].sql.includes("session_version=session_version+1"),
    true,
  );
  assertEquals(connection.calls[8].parameters, [7, 42]);
  assertEquals(
    connection.calls.some((call) =>
      /INSERT INTO (platform_staff_roles|organization_access_grants)/u.test(
        call.sql,
      )
    ),
    false,
  );
});

Deno.test("organization provisioning rejects disabled accounts and invalid input", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  await assertRejects(
    () => repository.createOrganization(42, "Example", "example"),
    TypeError,
    "active account required",
  );
  assertEquals(connection.outcome, "rolled_back");
  assertEquals(connection.calls.length, 1);
  await assertRejects(
    () => repository.createOrganization(0, "Example", "example"),
    TypeError,
  );
  await assertRejects(
    () => repository.createOrganization(42, " ", "example"),
    TypeError,
  );
  await assertRejects(
    () => repository.createOrganization(42, "Example", "example/other"),
    TypeError,
  );
  assertEquals(connection.calls.length, 1);
});

Deno.test("self-service signup rejects stale sessions and existing ownership inside the transaction", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  connection.rows = [[{ id: 42, session_version: 4 }]];
  await assertRejects(
    () => repository.createOrganization(42, "Example", "example", 3),
    OrganizationSignupConflict,
  );
  assertEquals(connection.outcome, "rolled_back");
  assertEquals(connection.calls.length, 1);
  connection.rows = [[{ id: 42, session_version: 3 }], [{
    organization_id: 7,
  }]];
  await assertRejects(
    () => repository.createOrganization(42, "Example", "example", 3),
    OrganizationSignupConflict,
  );
  assertEquals(connection.outcome, "rolled_back");
  assertEquals(connection.calls.at(-1)?.parameters, [42]);
  assertEquals(
    connection.calls.some((call) => call.sql.startsWith("INSERT")),
    false,
  );
});

Deno.test("self-service signup binds the current session before provisioning", async () => {
  const connection = new FakeConnection();
  connection.rows = [[{ id: 42, session_version: 3 }], [], [{ id: 7 }], [{
    id: 8,
  }], [{ id: 9 }]];
  assertEquals(
    await new PostgresBillingRepository(connection).createOrganization(
      42,
      "Example",
      "example",
      3,
    ),
    {
      organizationId: 7,
      communityId: 9,
    },
  );
  assertEquals(connection.outcome, "committed");
  assertEquals(connection.calls[0].sql.includes("FOR UPDATE"), true);
});

Deno.test("billing repository normalizes PostgreSQL dates and scopes entitlement reads", async () => {
  const connection = new FakeConnection();
  connection.rows = [[{
    organization_id: "7",
    source: "kickstarter",
    source_id: "campaign:backer",
    kind: "fixed_term",
    status: "active",
    starts_at: new Date("2026-10-01T00:00:00.000Z"),
    ends_at: "2027-01-01T00:00:00.000Z",
  }]];
  const repository = new PostgresBillingRepository(connection);
  const result = await repository.entitlement(
    7,
    new Date("2026-10-17T00:00:00.000Z"),
  );
  assertEquals(result.active, true);
  assertEquals(result.endsAt, "2027-01-01T00:00:00.000Z");
  assertEquals(connection.calls[0].parameters, [7]);
  assertEquals(
    connection.calls[0].sql.includes("organization.status='active'"),
    true,
  );
  assertEquals((await repository.entitlement(8)).active, false);
});

Deno.test("active platform-admin ownership provides revocable complimentary access", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  connection.rows = [[], [{ complimentary: true }]];
  const entitlement = await repository.entitlement(7);
  assertEquals(entitlement.active, true);
  assertEquals(entitlement.complimentary, true);
  assertEquals(entitlement.endsAt, null);
  assertEquals(entitlement.grants, []);
  assertEquals(connection.calls[1].parameters, [7]);
  for (
    const guard of [
      "account.status='active'",
      "staff.role='admin'",
      "owner.role='owner'",
      "owner.organization_id=organization.id",
    ]
  ) {
    assertEquals(connection.calls[1].sql.includes(guard), true);
  }
  connection.rows = [[], [{ complimentary: false }]];
  assertEquals((await repository.entitlement(7)).active, false);
});

Deno.test("admin lists require platform admin and use bounded parameterized pagination", async () => {
  const connection = new FakeConnection();
  const repository = new PostgresBillingRepository(connection);
  assertEquals(await repository.adminRows(42, "accounts", 1), null);
  assertEquals(connection.calls.length, 1);
  connection.rows = [[{ role: "admin" }], [{ id: 42 }]];
  assertEquals(await repository.adminRows(42, "accounts", 2), [{ id: 42 }]);
  assertEquals(connection.calls.at(-1)?.parameters, [50]);
  assertEquals(
    connection.calls.at(-1)?.sql.includes("LIMIT 51 OFFSET $1"),
    true,
  );
});
