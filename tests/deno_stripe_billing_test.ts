import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import Stripe from "npm:stripe@17.7.0";
import { StripeBillingProvider } from "../src/providers/stripe_billing.ts";
import { WebBillingController } from "../src/web/web_billing.ts";
import { WebAdminController } from "../src/web/web_admin.ts";
import type { StripeBillingService } from "../src/data/stripe_billing_repository.ts";
import { BillingRequestError } from "../src/data/stripe_billing_repository.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { createApp } from "../main.ts";
import { createReadOnlyWebHandler } from "../src/web/web_read_only.ts";
import { SCHEMA_SCOPE_INVENTORY } from "../src/data/schema_scope.ts";

const settings = {
  stripeSecretKey: "sk_test_fixture",
  stripeWebhookSecret: "whsec_fixture",
  stripePriceMonthly: "price_monthly",
  stripePriceAnnual: "price_annual",
};

Deno.test("backer coupon and portal validate policy before creating sessions", async () => {
  let discount = 20;
  let allowUpdates = false;
  const requests: { url: string; body: URLSearchParams }[] = [];
  const provider = new StripeBillingProvider({
    ...settings,
    stripeBackerCouponId: "coupon_fixture",
    stripePortalConfigurationId: "bpc_fixture",
  }, (input, init) => {
    const url = String(input);
    requests.push({ url, body: new URLSearchParams(String(init?.body ?? "")) });
    return Promise.resolve(
      Response.json(
        url.includes("/coupons/")
          ? {
            id: "coupon_fixture",
            valid: true,
            livemode: false,
            percent_off: discount,
            duration: "forever",
            amount_off: null,
            max_redemptions: null,
            redeem_by: null,
          }
          : url.includes("/configurations/")
          ? {
            id: "bpc_fixture",
            active: true,
            livemode: false,
            features: {
              subscription_update: { enabled: allowUpdates },
              subscription_cancel: { enabled: true, mode: "at_period_end" },
              payment_method_update: { enabled: true },
              invoice_history: { enabled: true },
            },
          }
          : {
            id: "cs_fixture",
            url: "https://billing.stripe.com/p/session/fixture",
            status: "open",
            expires_at: 9999999999,
          },
      ),
    );
  });
  await provider.createCheckout(
    "cus_fixture",
    "monthly",
    "https://example.test",
    7,
    "reward-checkout",
    "42",
  );
  assertEquals(
    requests.at(-1)!.body.get("discounts[0][coupon]"),
    "coupon_fixture",
  );
  assertEquals(
    requests.at(-1)!.body.get("subscription_data[metadata][qbot_backer_id]"),
    "42",
  );
  discount = 10;
  await assertRejects(() =>
    provider.createCheckout(
      "cus_fixture",
      "monthly",
      "https://example.test",
      7,
      "bad-checkout",
      "42",
    )
  );
  assertEquals(requests.at(-1)!.url.includes("/coupons/"), true);
  assertEquals(
    await provider.portal("cus_fixture", "https://example.test"),
    "https://billing.stripe.com/p/session/fixture",
  );
  assertEquals(requests.at(-1)!.body.get("configuration"), "bpc_fixture");
  allowUpdates = true;
  await assertRejects(() =>
    provider.portal("cus_fixture", "https://example.test")
  );
  assertEquals(requests.at(-1)!.url.includes("/configurations/"), true);
});

Deno.test("reward transition expires Checkout and confirms period-end cancellation with stable idempotency", async () => {
  const requests: {
    url: string;
    method: string;
    body: URLSearchParams;
    key: string | null;
  }[] = [];
  let scheduled = false;
  const provider = new StripeBillingProvider({
    ...settings,
    stripeBackerCouponId: "coupon_fixture",
  }, (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    requests.push({
      url,
      method,
      body: new URLSearchParams(String(init?.body ?? "")),
      key: new Headers(init?.headers).get("idempotency-key"),
    });
    const list = (data: unknown[]) => ({
      object: "list",
      data,
      has_more: false,
    });
    return Promise.resolve(
      Response.json(
        url.includes("/checkout/sessions?")
          ? list([{ id: "cs_open" }])
          : url.includes("/subscriptions?")
          ? list([{
            id: "sub_fixture",
            status: "active",
            schedule: scheduled ? "sub_sched" : null,
            discount: { coupon: { id: "coupon_fixture" } },
            items: { data: [] },
          }])
          : { id: "sub_fixture", cancel_at_period_end: true },
      ),
    );
  });
  assertEquals(await provider.prepareReward("cus_fixture", "backer:42"), null);
  assertEquals(
    requests.some((request) =>
      request.url.endsWith("/cs_open/expire") && request.method === "POST"
    ),
    true,
  );
  const cancel = requests.find((request) =>
    request.url.endsWith("/sub_fixture") && request.method === "POST"
  )!;
  assertEquals(cancel.body.get("cancel_at_period_end"), "true");
  assertEquals(cancel.key, "backer:42:stop-renewal:sub_fixture");
  await provider.removeBackerDiscount("cus_fixture");
  assertEquals(
    requests.some((request) =>
      request.url.endsWith("/sub_fixture/discount") &&
      request.method === "DELETE"
    ),
    true,
  );
  scheduled = true;
  await assertRejects(
    () => provider.prepareReward("cus_fixture", "backer:42"),
    Error,
    "manual reward transition",
  );
});

Deno.test("Stripe persistence has explicit organization and global inbox scopes", () => {
  assertEquals(SCHEMA_SCOPE_INVENTORY.organization_stripe_billing, {
    scope: "organization",
    ownerColumn: "organization_id",
    ownerTable: "organizations",
  });
  assertEquals(SCHEMA_SCOPE_INVENTORY.stripe_event_inbox, { scope: "global" });
});

async function signature(
  payload: string,
  timestamp = Math.floor(Date.now() / 1000),
): Promise<string> {
  return await Stripe.webhooks.generateTestHeaderStringAsync({
    payload,
    secret: settings.stripeWebhookSecret,
    timestamp,
    cryptoProvider: Stripe.createSubtleCryptoProvider(),
  });
}

Deno.test("Stripe verifies raw webhook signatures and rejects stale or changed payloads", async () => {
  const provider = new StripeBillingProvider(settings);
  const body = JSON.stringify({
    id: "evt_fixture",
    type: "invoice.paid",
    livemode: false,
    data: { object: { customer: "cus_fixture" } },
  });
  const header = await signature(body);
  assertEquals((await provider.verify(body, header)).customerId, "cus_fixture");
  await assertRejects(() => provider.verify(body + " ", header));
  await assertRejects(() => provider.verify(body, "t=1,v1=invalid"));
  await assertRejects(async () =>
    provider.verify(body, await signature(body, 1))
  );
});

Deno.test("Stripe Checkout uses approved server prices, customer and idempotency key", async () => {
  const requests: { url: string; body: URLSearchParams; key: string | null }[] =
    [];
  let validPrice = true;
  const provider = new StripeBillingProvider(settings, (input, init) => {
    const url = String(input);
    requests.push({
      url,
      body: new URLSearchParams(String(init?.body ?? "")),
      key: new Headers(init?.headers).get("idempotency-key"),
    });
    return Promise.resolve(Response.json(
      url.includes("/prices/")
        ? {
          id: "price_monthly",
          active: true,
          livemode: false,
          currency: "usd",
          unit_amount: validPrice ? 999 : 100,
          type: "recurring",
          recurring: {
            interval: "month",
            interval_count: 1,
            usage_type: "licensed",
          },
          billing_scheme: "per_unit",
        }
        : {
          id: "cs_fixture",
          status: "open",
          url: "https://checkout.stripe.com/c/pay/fixture",
          expires_at: 9999999999,
        },
    ));
  });
  await provider.validatePrice("monthly");
  validPrice = false;
  await assertRejects(
    () => provider.validatePrice("monthly"),
    TypeError,
    "approved subscription catalog",
  );
  await provider.createCheckout(
    "cus_fixture",
    "annual",
    "https://qbot.example",
    7,
    "attempt-key",
  );
  const request = requests.at(-1)!;
  assertEquals(request.key, "attempt-key");
  assertEquals(request.body.get("customer"), "cus_fixture");
  assertEquals(request.body.get("line_items[0][price]"), "price_annual");
  assertEquals(request.body.get("mode"), "subscription");
  assertEquals(request.body.get("allow_promotion_codes"), "false");
  assertEquals(
    request.body.get("success_url"),
    "https://qbot.example/billing?organization_id=7&checkout=returned",
  );
});

Deno.test("Stripe paid snapshots exclude unpaid, refunded and disputed invoices", async () => {
  let refunded = false;
  let disputed = false;
  let paid = true;
  const provider = new StripeBillingProvider(settings, (input) => {
    const url = new URL(String(input));
    const list = (data: unknown[]) =>
      Promise.resolve(Response.json({ object: "list", data, has_more: false }));
    if (url.pathname === "/v1/subscriptions") {
      return list([{
        id: "sub_fixture",
        status: "active",
        items: { data: [{ quantity: 1, price: { id: "price_monthly" } }] },
      }]);
    }
    if (url.pathname.startsWith("/v1/prices/")) {
      return Promise.resolve(Response.json({
        active: true,
        livemode: false,
        currency: "usd",
        unit_amount: 999,
        type: "recurring",
        recurring: {
          interval: "month",
          interval_count: 1,
          usage_type: "licensed",
        },
        billing_scheme: "per_unit",
      }));
    }
    if (url.pathname === "/v1/invoices") {
      return list([{
        id: "in_fixture",
        amount_paid: paid ? 999 : 0,
        paid_out_of_band: false,
        charge: "ch_fixture",
      }]);
    }
    if (url.pathname === "/v1/charges/ch_fixture") {
      return Promise.resolve(Response.json({
        paid,
        refunded,
        disputed,
        amount_refunded: refunded ? 999 : 0,
      }));
    }
    if (url.pathname === "/v1/invoices/in_fixture/lines") {
      return list([{
        type: "subscription",
        proration: false,
        price: { id: "price_monthly" },
        quantity: 1,
        amount: 999,
        period: { start: 1700000000, end: 1702678400 },
      }]);
    }
    throw new Error(`Unexpected Stripe path ${url.pathname}`);
  });
  assertEquals(
    (await provider.subscriptions("cus_fixture"))[0].endsAt,
    new Date(1702678400000).toISOString(),
  );
  refunded = true;
  assertEquals((await provider.subscriptions("cus_fixture"))[0].endsAt, null);
  refunded = false;
  disputed = true;
  assertEquals((await provider.subscriptions("cus_fixture"))[0].endsAt, null);
  disputed = false;
  paid = false;
  assertEquals((await provider.subscriptions("cus_fixture"))[0].endsAt, null);
});

Deno.test("billing HTTP binds identity, rejects hostile forms and durably stores signed events", async () => {
  let platformAdmin = false;
  let sessionVersion = 1;
  const auth = new WebAuthController(
    {
      dashboardSessionSecret: "fixture",
      discordOauthClientId: null,
      discordOauthClientSecret: null,
      discordOauthRedirectUri: null,
      operatorGuildIds: [],
    },
    { authenticate: () => Promise.reject(new Error("unused")) },
    {
      completeLogin: () => Promise.reject(new Error("unused")),
      switchCommunity: () => Promise.resolve(null),
      auditLogout: () => Promise.resolve(),
      isPlatformAdmin: () => Promise.resolve(platformAdmin),
      resolveSession: () =>
        Promise.resolve({
          status: "active",
          sessionVersion,
          memberships: [],
        }),
    },
  );
  const cookie = await createSessionCookie("fixture", {
    userId: "42",
    username: "Customer",
    role: "viewer",
    communityId: null,
    sessionVersion: 1,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
  });
  const calls: unknown[] = [];
  let failStorage = false;
  let complimentary = false;
  const service: StripeBillingService = {
    organizations: () =>
      Promise.resolve([{ id: 7, name: "Example", complimentary }]),
    checkout: (actor, organization, cadence, origin) => {
      if (organization !== 7) {
        return Promise.reject(new BillingRequestError(403, "Denied"));
      }
      calls.push([actor, organization, cadence, origin]);
      return Promise.resolve("https://checkout.stripe.com/c/pay/fixture");
    },
    receive: (event) => {
      if (failStorage) return Promise.reject(new Error("private DB error"));
      calls.push(event.id);
      return Promise.resolve();
    },
    processPending: () => Promise.resolve(),
  };
  const controller = new WebBillingController(
    auth,
    service,
    new StripeBillingProvider(settings),
    "https://canonical.example",
  );
  const app = createApp(
    undefined,
    auth,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    controller,
    new WebAdminController(auth, {
      rows: (actor, view, page) => {
        assertEquals([actor, view, page], [42, "accounts", 2]);
        return Promise.resolve([{
          id: 42,
          discord_username: "<script>bad</script>",
          status: "active",
          platform_role: "admin",
        }]);
      },
    }),
  ).handler();
  const adminRequest = (
    path = "/admin?view=accounts&page=2",
    authenticated = true,
  ) =>
    new Request(`https://qbot.example${path}`, {
      headers: authenticated ? { cookie: `qbot4k_session=${cookie}` } : {},
    });
  assertEquals((await app(adminRequest(undefined, false))).status, 403);
  assertEquals((await app(adminRequest())).status, 403);
  platformAdmin = true;
  const adminResponse = await app(adminRequest());
  assertEquals(adminResponse.status, 200);
  assertEquals(adminResponse.headers.get("cache-control"), "no-store");
  const adminHtml = await adminResponse.text();
  assertEquals(adminHtml.includes("Platform admin"), true);
  assertEquals(adminHtml.includes("<script>bad</script>"), false);
  assertEquals((await app(adminRequest("/admin?view=secrets"))).status, 400);
  assertEquals((await app(adminRequest("/admin?page=-1"))).status, 400);
  sessionVersion = 2;
  assertEquals((await app(adminRequest())).status, 403);
  sessionVersion = 1;
  complimentary = true;
  const staffBilling = await app(
    new Request("https://qbot.example/billing", {
      headers: { cookie: `qbot4k_session=${cookie}` },
    }),
  );
  const staffHtml = await staffBilling.text();
  assertEquals(staffHtml.includes("Complimentary staff access"), true);
  assertEquals(staffHtml.includes('action="/billing/checkout"'), false);
  assertEquals(staffHtml.includes('href="/admin"'), true);
  complimentary = false;
  const checkout = (
    body: string,
    origin = "https://qbot.example",
    authenticated = true,
  ) =>
    new Request("https://qbot.example/billing/checkout", {
      method: "POST",
      headers: {
        origin,
        "content-type": "application/x-www-form-urlencoded",
        ...(authenticated ? { cookie: `qbot4k_session=${cookie}` } : {}),
      },
      body,
    });
  assertEquals(
    (await app(checkout("organization_id=7&cadence=monthly", undefined, false)))
      .status,
    401,
  );
  assertEquals(
    (await app(
      checkout("organization_id=7&cadence=monthly", "https://evil.example"),
    )).status,
    403,
  );
  assertEquals(
    (await app(checkout("organization_id=999&cadence=monthly"))).status,
    403,
  );
  assertEquals(
    (await app(checkout("organization_id=7&cadence=lifetime"))).status,
    400,
  );
  assertEquals((await app(checkout("x".repeat(4097)))).status, 413);
  assertEquals(
    (await app(
      checkout(
        "organization_id=7&cadence=monthly&operator_id=999&price=price_evil",
      ),
    )).status,
    303,
  );
  assertEquals(calls, [[42, 7, "monthly", "https://canonical.example"]]);
  const body = JSON.stringify({
    id: "evt_http",
    type: "invoice.paid",
    livemode: false,
    data: { object: { customer: "cus_fixture" } },
  });
  const headers = { "stripe-signature": await signature(body) };
  const webhook = () =>
    new Request("https://qbot.example/webhooks/stripe", {
      method: "POST",
      headers,
      body,
    });
  assertEquals((await app(webhook())).status, 200);
  assertEquals(calls.at(-1), "evt_http");
  failStorage = true;
  const failure = await app(webhook());
  assertEquals(failure.status, 503);
  assertEquals((await failure.text()).includes("private"), false);
  assertEquals((await createReadOnlyWebHandler(app)(webhook())).status, 503);
  assertEquals(
    (await createReadOnlyWebHandler(app)(
      checkout("organization_id=7&cadence=monthly"),
    )).status,
    503,
  );
  assertEquals(
    (await app(
      new Request("https://qbot.example/webhooks/stripe", {
        method: "POST",
        body,
      }),
    )).status,
    400,
  );
  assertEquals(
    (await app(
      new Request("https://qbot.example/webhooks/stripe", {
        method: "POST",
        headers,
        body: "x".repeat(1_048_577),
      }),
    )).status,
    413,
  );
});
