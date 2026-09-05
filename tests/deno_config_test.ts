import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import { AppSettings, ConfigError } from "../src/core/config.ts";

const baseEnv = (overrides: Record<string, string> = {}) => ({
  QBOT_DATABASE_URL: "postgresql://localhost/qbot4k",
  QBOT_ENABLED_SERVICES: "jobs,analysis",
  ...overrides,
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
  assertEquals(settings.safeSummary().shadow_read_enabled, true);
  assertEquals(settings.safeSummary().web_read_only, true);
  assertEquals(settings.safeSummary().database_backend, "postgresql");
  assertEquals("twitch_bot_token" in settings.safeSummary(), false);
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
});
