import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  APPLICATION_ENV_KEYS,
  AppSettings,
  ConfigError,
} from "../src/core/config.ts";

const baseEnv = (overrides: Record<string, string> = {}) => ({
  QBOT_DATABASE_URL: "postgresql://localhost/qbot4k",
  QBOT_ENABLED_SERVICES: "jobs,analysis",
  ...overrides,
});

const stripeEnv = {
  STRIPE_PUBLISHABLE_KEY: "pk_test_fixturePublic",
  STRIPE_SECRET_KEY: "sk_test_fixturePrivate",
  STRIPE_WEBHOOK_SECRET: "whsec_fixtureWebhook",
  QBOT_STRIPE_PRICE_MONTHLY: "price_fixtureMonthly",
  QBOT_STRIPE_PRICE_ANNUAL: "price_fixtureAnnual",
};

Deno.test("reward configuration validates optional provider identifiers without revealing credentials", () => {
  const env = {
    QBOT_STRIPE_BACKER_COUPON_ID: "fixtureCoupon",
    QBOT_STRIPE_PORTAL_CONFIGURATION_ID: "bpc_fixture",
    QBOT_REWARD_EMAIL_API_KEY: "re_fixtureSecret",
    QBOT_REWARD_EMAIL_FROM: "rewards@example.test",
  };
  const settings = AppSettings.fromEnv(baseEnv(env));
  assertEquals(settings.safeSummary().stripe_portal_configured, true);
  assertEquals(settings.safeSummary().stripe_backer_coupon_configured, true);
  assertEquals(
    JSON.stringify(settings.safeSummary()).includes("re_fixtureSecret"),
    false,
  );
  for (const key of Object.keys(env)) {
    const error = assertThrows(
      () => AppSettings.fromEnv(baseEnv({ [key]: "invalid secret<>" })),
      ConfigError,
      key,
    );
    assertEquals(error.message.includes("invalid secret<>"), false);
  }
});

Deno.test("Stripe configuration uses the supplied environment names and redacts values", () => {
  const settings = AppSettings.fromEnv(baseEnv(stripeEnv));
  assertEquals(settings.stripePublishableKey, stripeEnv.STRIPE_PUBLISHABLE_KEY);
  assertEquals(settings.stripeSecretKey, stripeEnv.STRIPE_SECRET_KEY);
  assertEquals(settings.stripeWebhookSecret, stripeEnv.STRIPE_WEBHOOK_SECRET);
  assertEquals(
    settings.stripePriceMonthly,
    stripeEnv.QBOT_STRIPE_PRICE_MONTHLY,
  );
  assertEquals(settings.stripePriceAnnual, stripeEnv.QBOT_STRIPE_PRICE_ANNUAL);
  const summary = settings.safeSummary();
  assertEquals(summary.stripe_api_configured, true);
  assertEquals(summary.stripe_webhook_configured, true);
  assertEquals(summary.stripe_prices_configured, true);
  assertEquals(summary.stripe_mode, "test");
  for (const [key, value] of Object.entries(stripeEnv)) {
    assertEquals(
      (APPLICATION_ENV_KEYS as readonly string[]).includes(key),
      true,
    );
    assertEquals(JSON.stringify(summary).includes(value), false);
  }
  const defaults = AppSettings.fromEnv(baseEnv());
  assertEquals(defaults.stripeSecretKey, null);
  assertEquals(defaults.stripeWebhookSecret, null);
  assertEquals(defaults.safeSummary().stripe_api_configured, false);
  assertEquals(defaults.safeSummary().stripe_mode, null);
  assertEquals(
    AppSettings.fromEnv(baseEnv({ STRIPE_SECRET_KEY: "rk_live_fixture" }))
      .safeSummary().stripe_mode,
    "live",
  );
});

Deno.test("Stripe validation rejects malformed values and mismatched modes without leaking secrets", () => {
  for (const key of Object.keys(stripeEnv)) {
    const error = assertThrows(
      () => AppSettings.fromEnv(baseEnv({ [key]: "invalid-private-value" })),
      ConfigError,
      key,
    );
    assertEquals(error.message.includes("invalid-private-value"), false);
  }
  assertThrows(
    () =>
      AppSettings.fromEnv(
        baseEnv({ ...stripeEnv, STRIPE_SECRET_KEY: "sk_live_fixture" }),
      ),
    ConfigError,
    "same test/live mode",
  );
  assertThrows(
    () =>
      AppSettings.fromEnv(
        baseEnv({
          ...stripeEnv,
          QBOT_STRIPE_PRICE_ANNUAL: stripeEnv.QBOT_STRIPE_PRICE_MONTHLY,
        }),
      ),
    ConfigError,
    "different prices",
  );
});

Deno.test("settings parse defaults and produce a redacted summary", () => {
  const settings = AppSettings.fromEnv(baseEnv({
    QBOT_LOG_LEVEL: "debug",
    QBOT_MODERATION_SHADOW_MODE: "yes",
    QBOT_SHADOW_READ_UPSTREAM_URL: "http://127.0.0.1:8081",
    QBOT_WEB_READ_ONLY: "true",
  }));
  assertEquals(settings.dashboardPort, 8080);
  assertEquals(settings.enabledServices, ["jobs", "analysis"]);
  assertEquals(settings.twitchChannels, ["its_not_qwerty"]);
  assertEquals(settings.logLevel, "DEBUG");
  assertEquals(settings.moderationShadowMode, true);
  assertEquals(settings.shadowReadUpstreamUrl, "http://127.0.0.1:8081");
  assertEquals(settings.webReadOnly, true);
  assertEquals(settings.customerSignupEnabled, false);
  assertEquals(settings.safeSummary().shadow_read_enabled, true);
  assertEquals(settings.safeSummary().web_read_only, true);
  assertEquals(settings.safeSummary().database_backend, "postgresql");
  assertEquals("twitch_bot_token" in settings.safeSummary(), false);
});

Deno.test("customer signup is explicit and requires a fixed HTTPS OAuth callback", () => {
  const env = baseEnv({
    QBOT_ENABLED_SERVICES: "web",
    QBOT_CUSTOMER_SIGNUP_ENABLED: "true",
    QBOT_DASHBOARD_SESSION_SECRET: "session-secret",
    QBOT_DISCORD_OAUTH_CLIENT_ID: "client",
    QBOT_DISCORD_OAUTH_CLIENT_SECRET: "secret",
    QBOT_LEGAL_ORGANIZATION_NAME: "QBot4K",
    QBOT_LEGAL_CONTACT_EMAIL: "ops@example.test",
    QBOT_LEGAL_JURISDICTION: "Test",
    QBOT_LEGAL_EFFECTIVE_DATE: "2026-01-01",
  });
  assertThrows(() => AppSettings.fromEnv(env), ConfigError, "explicit HTTPS");
  assertThrows(
    () =>
      AppSettings.fromEnv({
        ...env,
        QBOT_DISCORD_OAUTH_REDIRECT_URI: "http://example.test/callback",
      }),
    ConfigError,
  );
  const settings = AppSettings.fromEnv({
    ...env,
    QBOT_DISCORD_OAUTH_REDIRECT_URI: "https://example.test/callback",
  });
  assertEquals(settings.customerSignupEnabled, true);
  assertEquals(settings.operatorGuildIds, []);
  assertEquals(settings.safeSummary().customer_signup_enabled, true);
});

Deno.test("settings redact PostgreSQL credentials from safe summaries", () => {
  const summary = AppSettings.fromEnv(baseEnv({
    QBOT_DATABASE_URL: "postgresql://operator:secret@db.example:5433/qbot4k",
  })).safeSummary();
  assertEquals(summary.database_path, "postgresql://db.example:5433/qbot4k");
  assertEquals(String(summary.database_path).includes("secret"), false);
  assertEquals(String(summary.database_path).includes("operator"), false);
});

Deno.test("settings reject unknown and incomplete service combinations", () => {
  assertThrows(
    () =>
      AppSettings.fromEnv(baseEnv({ QBOT_ENABLED_SERVICES: "jobs,unknown" })),
    ConfigError,
    "Unknown services requested",
  );
  assertThrows(
    () => AppSettings.fromEnv(baseEnv({ QBOT_ENABLED_SERVICES: "twitch" })),
    ConfigError,
    "analysis service is required",
  );
  assertThrows(
    () =>
      AppSettings.fromEnv(
        baseEnv({ QBOT_ENABLED_SERVICES: "twitch,analysis" }),
      ),
    ConfigError,
    "QBOT_TWITCH_BOT_TOKEN or a stored Twitch credential",
  );
  assertThrows(
    () => AppSettings.fromEnv(baseEnv({ QBOT_DASHBOARD_PORT: "70000" })),
    ConfigError,
    "between 1 and 65535",
  );
  assertThrows(
    () =>
      AppSettings.fromEnv(
        baseEnv({ QBOT_SHADOW_READ_UPSTREAM_URL: "https://python.example" }),
      ),
    ConfigError,
    "must be credential-free loopback HTTP",
  );
});

Deno.test("web settings require authentication and legal identity", () => {
  assertThrows(
    () => AppSettings.fromEnv(baseEnv({ QBOT_ENABLED_SERVICES: "web" })),
    ConfigError,
    "Missing web configuration",
  );
  const settings = AppSettings.fromEnv(baseEnv({
    QBOT_ENABLED_SERVICES: "web,jobs",
    QBOT_DASHBOARD_SESSION_SECRET: "session-secret",
    QBOT_DISCORD_OAUTH_CLIENT_ID: "client-id",
    QBOT_DISCORD_OAUTH_CLIENT_SECRET: "client-secret",
    QBOT_OPERATOR_GUILD_IDS: "guild-1",
    QBOT_LEGAL_ORGANIZATION_NAME: "QBot4K",
    QBOT_LEGAL_CONTACT_EMAIL: "ops@example.test",
    QBOT_LEGAL_JURISDICTION: "Test",
    QBOT_LEGAL_EFFECTIVE_DATE: "2026-01-01",
  }));
  assertEquals(settings.enabledServices, ["web", "jobs"]);
  assertEquals(settings.safeSummary().web_auth_configured, true);
  const webEnv = baseEnv({
    QBOT_ENABLED_SERVICES: "web",
    QBOT_DASHBOARD_SESSION_SECRET: "fixture",
    QBOT_DISCORD_OAUTH_CLIENT_ID: "fixture",
    QBOT_DISCORD_OAUTH_CLIENT_SECRET: "fixture",
    QBOT_OPERATOR_GUILD_IDS: "fixture",
    QBOT_LEGAL_ORGANIZATION_NAME: "Fixture",
    QBOT_LEGAL_CONTACT_EMAIL: "fixture@example.test",
    QBOT_LEGAL_JURISDICTION: "Test",
    QBOT_LEGAL_EFFECTIVE_DATE: "2026-10-17",
  });
  assertThrows(
    () =>
      AppSettings.fromEnv({
        ...webEnv,
        STRIPE_SECRET_KEY: stripeEnv.STRIPE_SECRET_KEY,
      }),
    ConfigError,
    "Stripe Checkout requires",
  );
  assertThrows(
    () => AppSettings.fromEnv({ ...webEnv, ...stripeEnv }),
    ConfigError,
    "explicit HTTPS",
  );
  assertEquals(
    AppSettings.fromEnv({
      ...webEnv,
      ...stripeEnv,
      QBOT_DISCORD_OAUTH_REDIRECT_URI: "https://qbot.example/callback",
    }).stripeSecretKey,
    stripeEnv.STRIPE_SECRET_KEY,
  );
});
