import { assertEquals } from "jsr:@std/assert@1.0.14";
import { Buffer } from "node:buffer";
import { chromium, expect } from "@playwright/test";
import { createApp } from "../main.ts";
import { createSessionCookie } from "../src/security/security.ts";
import { WebAuthController } from "../src/web/web_auth.ts";
import { WebAccountsController } from "../src/web/web_accounts.ts";
import { WebBillingController } from "../src/web/web_billing.ts";
import { StripeBillingProvider } from "../src/providers/stripe_billing.ts";
import { WebAdminController } from "../src/web/web_admin.ts";
import { WebRewardsController } from "../src/web/web_rewards.ts";
import type { BackerService } from "../src/data/backer_repository.ts";

for (
  const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]
) {
  Deno.test(`account signup browser ${viewport.width}px`, async () => {
    let writes = 0;
    let platformAdmin = false;
    const secret = "local-browser-fixture";
    const auth = new WebAuthController({
      dashboardSessionSecret: secret,
      discordOauthClientId: "fixture",
      discordOauthClientSecret: "fixture",
      discordOauthRedirectUri: "https://example.test/callback",
      operatorGuildIds: [],
      customerSignupEnabled: true,
    }, {
      authenticate: () => {
        throw new Error("External OAuth disabled in browser fixture");
      },
    }, {
      completeLogin: () => {
        throw new Error("unused");
      },
      switchCommunity: () => Promise.resolve(null),
      auditLogout: () => Promise.resolve(),
      isPlatformAdmin: () => Promise.resolve(platformAdmin),
      resolveSession: () =>
        Promise.resolve({
          status: "active",
          sessionVersion: 1,
          memberships: [],
        }),
    });
    const accounts = new WebAccountsController(auth, {
      createOrganization: (operatorId, name, slug, version) => {
        assertEquals([operatorId, name, version], [
          42,
          "Example Organization",
          1,
        ]);
        if (slug === "taken") return Promise.reject({ code: "23505" });
        writes++;
        return Promise.resolve({ organizationId: 7, communityId: 9 });
      },
    }, true);
    let checkoutRequests = 0;
    const billing = new WebBillingController(
      auth,
      {
        organizations: () =>
          Promise.resolve([{
            id: 7,
            name: "Example Organization",
            subscription_status: "none",
            complimentary: platformAdmin,
          }]),
        checkout: (actor, organization, cadence, origin) => {
          assertEquals([actor, organization, cadence, origin], [
            42,
            7,
            "annual",
            "https://example.test",
          ]);
          checkoutRequests++;
          return Promise.resolve(
            "https://checkout.stripe.com/c/pay/browserFixture",
          );
        },
        receive: () => Promise.resolve(),
        processPending: () => Promise.resolve(),
      },
      new StripeBillingProvider({
        stripeSecretKey: "sk_test_fixture",
        stripeWebhookSecret: "whsec_fixture",
        stripePriceMonthly: "price_monthly",
        stripePriceAnnual: "price_annual",
      }),
      "https://example.test",
    );
    let importConfirmed = false;
    let claimed = false;
    const rewards: BackerService = {
      activate: () => Promise.resolve(),
      campaign: () => Promise.resolve(),
      resend: () => Promise.resolve(),
      retry: () => Promise.resolve(),
      process: () => Promise.resolve(),
      overview: () =>
        Promise.resolve({
          campaigns: [{ id: "fixture", live_enabled: false }],
          imports: [],
          backers: [],
          audit: [],
        }),
      preview: (_actor, campaign, csv) => {
        assertEquals(campaign, "fixture");
        assertEquals(csv.includes("backer@example.test"), true);
        return Promise.resolve("preview-fixture");
      },
      previewDetail: () =>
        Promise.resolve({
          campaign: "fixture",
          confirmed: importConfirmed,
          count: 1,
          rows: [{
            backer: "fixture1",
            reward: "sixMonths",
            collected_cents: 5000,
            eligible: true,
          }],
        }),
      confirm: () => {
        importConfirmed = true;
        return Promise.resolve();
      },
      claim: (actor, version, organization, code) => {
        assertEquals([actor, version, organization, code], [
          42,
          1,
          7,
          "a".repeat(64),
        ]);
        claimed = true;
        return Promise.resolve();
      },
      benefits: () =>
        Promise.resolve(
          claimed
            ? [{ campaign_id: "fixture", fulfillment_status: "pending" }]
            : [],
        ),
    };
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
      accounts,
      billing,
      new WebAdminController(auth, {
        rows: (_actor, view) =>
          Promise.resolve(
            view === "organizations"
              ? [{
                id: 7,
                name: "Example Organization",
                status: "active",
                communities: 1,
                complimentary: true,
              }]
              : [{
                id: 42,
                discord_username: "Apollyon",
                status: "active",
                platform_role: "admin",
              }],
          ),
      }),
      new WebRewardsController(auth, rewards, {
        organizations: () =>
          Promise.resolve([{ id: 7, name: "Example Organization" }]),
        checkout: () => Promise.reject(new Error("unused")),
        receive: () => Promise.resolve(),
        processPending: () => Promise.resolve(),
      }, false),
    ).handler();
    const server = Deno.serve(
      { hostname: "127.0.0.1", port: 0, onListen: () => {} },
      (request) =>
        new URL(request.url).pathname === "/login"
          ? new Response("Sign in again", {
            headers: { "content-type": "text/plain" },
          })
          : app(request),
    );
    let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
    try {
      browser = await chromium.launch({ headless: true });
      const context = await browser.newContext({ viewport });
      const baseUrl = `http://127.0.0.1:${server.addr.port}`;
      const cookie = await createSessionCookie(secret, {
        userId: "42",
        username: "Customer",
        role: "viewer",
        communityId: null,
        sessionVersion: 1,
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
      });
      await context.addCookies([{
        name: "qbot4k_session",
        value: cookie,
        url: baseUrl,
      }]);
      const page = await context.newPage();
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${baseUrl}/account`);
      await expect(page.getByRole("heading", { name: "Create organization" }))
        .toBeVisible();
      await page.getByLabel("Organization name").fill("Example Organization");
      await page.getByLabel("Organization slug").fill("INVALID");
      await page.getByRole("button", {
        name: "Create organization",
        exact: true,
      }).click();
      assertEquals(writes, 0);
      assertEquals(
        await page.getByLabel("Organization slug").evaluate((element) =>
          (element as HTMLInputElement).validity.patternMismatch
        ),
        true,
      );
      await page.getByLabel("Organization slug").fill("taken");
      await page.getByRole("button", {
        name: "Create organization",
        exact: true,
      }).click();
      await expect(page.getByRole("alert")).toContainText("already in use");
      await expect(page.getByLabel("Organization name")).toHaveValue(
        "Example Organization",
      );
      assertEquals(
        await page.evaluate(() =>
          document.documentElement.scrollWidth <= globalThis.innerWidth
        ),
        true,
      );
      await Deno.mkdir("test-results", { recursive: true });
      await page.screenshot({
        path: `test-results/account-${viewport.width}.png`,
        fullPage: true,
      });
      await page.getByLabel("Organization slug").fill("example");
      await page.getByRole("button", {
        name: "Create organization",
        exact: true,
      }).click();
      await expect(page).toHaveURL(`${baseUrl}/login`);
      assertEquals(writes, 1);
      await page.goto(`${baseUrl}/billing`);
      await expect(page.getByRole("heading", { name: "Billing", exact: true }))
        .toBeVisible();
      await page.getByLabel("Subscription", { exact: true }).selectOption(
        "annual",
      );
      assertEquals(
        await page.evaluate(() =>
          document.documentElement.scrollWidth <= globalThis.innerWidth
        ),
        true,
      );
      await page.screenshot({
        path: `test-results/billing-${viewport.width}.png`,
        fullPage: true,
      });
      await page.route(
        "https://checkout.stripe.com/**",
        (route) =>
          route.fulfill({
            status: 200,
            contentType: "text/html",
            body: "<h1>Stripe Checkout fixture</h1>",
          }),
      );
      await page.getByRole("button", { name: "Continue to Stripe Checkout" })
        .click();
      await expect(page).toHaveURL(
        "https://checkout.stripe.com/c/pay/browserFixture",
      );
      assertEquals(checkoutRequests, 1);
      platformAdmin = true;
      await page.goto(`${baseUrl}/billing`);
      await expect(
        page.getByText("Complimentary staff access. No subscription required."),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Continue to Stripe Checkout" }),
      ).toHaveCount(0);
      await page.screenshot({
        path: `test-results/staff-billing-${viewport.width}.png`,
        fullPage: true,
      });
      await page.getByText("More", { exact: true }).click();
      await page.getByRole("link", { name: "Platform admin", exact: true })
        .click();
      await expect(page.getByRole("heading", { name: "Platform admin" }))
        .toBeVisible();
      await expect(
        page.getByRole("cell", { name: "Complimentary", exact: true }),
      ).toBeVisible();
      assertEquals(
        await page.evaluate(() =>
          document.documentElement.scrollWidth <= globalThis.innerWidth
        ),
        true,
      );
      await page.screenshot({
        path: `test-results/admin-${viewport.width}.png`,
        fullPage: true,
      });
      await page.getByRole("link", { name: "Accounts", exact: true }).click();
      await expect(page.getByRole("cell", { name: "Apollyon" })).toBeVisible();
      await page.goto(`${baseUrl}/admin/rewards?campaign=fixture`);
      await expect(page.getByRole("heading", { name: "Backer fulfillment" }))
        .toBeVisible();
      await expect(page.getByRole("button", { name: "Activate fulfillment" }))
        .toBeDisabled();
      await page.getByLabel("Verified backer CSV").setInputFiles({
        name: "fixture.csv",
        mimeType: "text/csv",
        buffer: Buffer.from("backer_id,email\nfixture1,backer@example.test"),
      });
      await page.getByRole("button", { name: "Preview import" }).click();
      await expect(
        page.getByRole("heading", { name: "Import preview: 1 backers" }),
      ).toBeVisible();
      assertEquals(importConfirmed, false);
      assertEquals(
        (await page.content()).includes("backer@example.test"),
        false,
      );
      await page.getByLabel(
        "I verified collection, refunds, reward IDs and every preview row.",
      ).check();
      await page.screenshot({
        path: `test-results/rewards-admin-${viewport.width}.png`,
        fullPage: true,
      });
      assertEquals(
        await page.evaluate(() =>
          document.documentElement.scrollWidth <= globalThis.innerWidth
        ),
        true,
      );
      await page.getByRole("button", { name: "Confirm import" }).click();
      await expect(page).toHaveURL(`${baseUrl}/admin/rewards?campaign=fixture`);
      assertEquals(importConfirmed, true);
      platformAdmin = false;
      await page.goto(`${baseUrl}/billing/rewards`);
      await page.getByLabel("Claim code").fill("a".repeat(64));
      await page.getByLabel("I own this organization", { exact: false })
        .check();
      await page.screenshot({
        path: `test-results/rewards-customer-${viewport.width}.png`,
        fullPage: true,
      });
      assertEquals(
        await page.evaluate(() =>
          document.documentElement.scrollWidth <= globalThis.innerWidth
        ),
        true,
      );
      await page.getByRole("button", { name: "Claim reward" }).click();
      await expect(page.getByRole("cell", { name: "pending", exact: true }))
        .toBeVisible();
      assertEquals(claimed, true);
      assertEquals(
        (await page.goto(`${baseUrl}/admin/rewards`))?.status(),
        403,
      );
      const denied = await page.goto(`${baseUrl}/admin`);
      assertEquals(denied?.status(), 403);
      assertEquals(errors, []);
    } finally {
      await browser?.close();
      await server.shutdown();
    }
  });
}
