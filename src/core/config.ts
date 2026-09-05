import { dirname, isAbsolute, resolve } from "@std/path";

export const SERVICE_ROLES = [
  "web",
  "jobs",
  "twitch",
  "discord",
  "analysis",
] as const;
export type ServiceRole = (typeof SERVICE_ROLES)[number];
export type LogLevel = "CRITICAL" | "ERROR" | "WARNING" | "INFO" | "DEBUG";
export type Environment = Readonly<Record<string, string>>;

export const APPLICATION_ENV_KEYS = [
  "QBOT_DATABASE_URL",
  "QBOT_DATABASE_PATH",
  "QBOT_ENABLED_SERVICES",
  "QBOT_LOG_LEVEL",
  "QBOT_BACKUP_DIR",
  "QBOT_DASHBOARD_HOST",
  "QBOT_DASHBOARD_PORT",
  "QBOT_DASHBOARD_SESSION_SECRET",
  "QBOT_SYSTEMD_SERVICE_NAME",
  "QBOT_TWITCH_CHANNELS",
  "QBOT_TWITCH_JOIN_COMMAND_CHANNEL",
  "QBOT_DISCORD_GUILD_IDS",
  "QBOT_OPERATOR_GUILD_IDS",
  "QBOT_AUDIT_RETENTION_DAYS",
  "QBOT_TWITCH_BOT_TOKEN",
  "QBOT_TWITCH_REFRESH_TOKEN",
  "QBOT_TWITCH_CLIENT_ID",
  "QBOT_TWITCH_CLIENT_SECRET",
  "QBOT_DISCORD_BOT_TOKEN",
  "QBOT_DISCORD_OAUTH_CLIENT_ID",
  "QBOT_DISCORD_OAUTH_CLIENT_SECRET",
  "QBOT_DISCORD_OAUTH_REDIRECT_URI",
  "QBOT_INGEST_API_TOKEN",
  "QBOT_MAINTENANCE_INTERVAL_SECONDS",
  "QBOT_ANALYTICS_INTERVAL_SECONDS",
  "QBOT_BACKUP_INTERVAL_SECONDS",
  "QBOT_BACKUP_RETENTION_COUNT",
  "QBOT_RAW_ARCHIVE_DIR",
  "QBOT_DEFAULT_COMMUNITY_SLUG",
  "QBOT_TWITCH_EVENTSUB_SECRET",
  "QBOT_TWITCH_EVENTSUB_CALLBACK_URL",
  "QBOT_TWITCH_OAUTH_REDIRECT_URI",
  "QBOT_CREDENTIAL_ENCRYPTION_KEY",
  "QBOT_LEGAL_ORGANIZATION_NAME",
  "QBOT_LEGAL_CONTACT_EMAIL",
  "QBOT_LEGAL_JURISDICTION",
  "QBOT_LEGAL_EFFECTIVE_DATE",
  "QBOT_MODERATION_SHADOW_MODE",
  "QBOT_SHADOW_READ_UPSTREAM_URL",
  "QBOT_WEB_READ_ONLY",
] as const;

export class ConfigError extends Error {
  override readonly name = "ConfigError";
}

export function safeDatabasePath(databasePath: string): string {
  if (!databasePath.startsWith("postgres")) return databasePath;
  const parsed = new URL(databasePath);
  return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
}

function parseCsv(value: string | undefined): readonly string[] {
  return Object.freeze(
    (value ?? "").split(",").map((item) => item.trim()).filter(Boolean),
  );
}

function parseInteger(
  name: string,
  value: string | undefined,
  fallback: number,
): number {
  if (value === undefined || value === "") return fallback;
  if (!/^[+-]?\d+$/u.test(value.trim())) {
    throw new ConfigError(`${name} must be an integer`);
  }
  return Number.parseInt(value, 10);
}

function parseBoolean(value: string | undefined, fallback = false): boolean {
  if (value === undefined || value === "") return fallback;
  const normalized = value.trim().toLocaleLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  return fallback;
}

function localPath(value: string): string {
  if (value.startsWith("~/")) {
    let home: string | undefined;
    try {
      home = Deno.env.get("HOME");
    } catch (error) {
      if (!(error instanceof Deno.errors.NotCapable)) throw error;
    }
    return home ? resolve(home, value.slice(2)) : resolve(value);
  }
  return isAbsolute(value) ? value : resolve(value);
}

function readProcessEnvironment(): Record<string, string> {
  const values: Record<string, string> = {};
  for (const key of APPLICATION_ENV_KEYS) {
    try {
      const value = Deno.env.get(key);
      if (value !== undefined) values[key] = value;
    } catch (error) {
      if (!(error instanceof Deno.errors.NotCapable)) throw error;
    }
  }
  return values;
}

function readEnvironmentFile(
  path: string,
  required: boolean,
): Record<string, string> {
  let contents: string;
  try {
    contents = Deno.readTextFileSync(path);
  } catch (error) {
    if (error instanceof Deno.errors.NotFound && required) {
      throw new ConfigError(`Environment file does not exist: ${path}`);
    }
    if (error instanceof Deno.errors.NotFound) return {};
    throw new ConfigError(
      `Unable to read environment file ${path}: ${String(error)}`,
    );
  }
  const values: Record<string, string> = {};
  for (const [index, rawLine] of contents.split(/\r?\n/u).entries()) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const separator = line.indexOf("=");
    if (separator < 0) {
      throw new ConfigError(
        `Invalid environment assignment at ${path}:${index + 1}`,
      );
    }
    let key = line.slice(0, separator).trim();
    if (key.startsWith("export ")) key = key.slice(7).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/u.test(key)) {
      throw new ConfigError(`Invalid environment key at ${path}:${index + 1}`);
    }
    values[key] = line.slice(separator + 1).trim().replace(
      /^(["'])(.*)\1$/u,
      "$2",
    );
  }
  return values;
}

export interface AppSettingsValues {
  databasePath: string;
  backupDir: string;
  dashboardHost: string;
  dashboardPort: number;
  dashboardSessionSecret: string | null;
  systemdServiceName: string;
  logLevel: LogLevel;
  enabledServices: readonly ServiceRole[];
  twitchChannels: readonly string[];
  twitchJoinCommandChannel: string;
  discordGuildIds: readonly string[];
  operatorGuildIds: readonly string[];
  auditRetentionDays: number;
  twitchBotToken: string | null;
  twitchRefreshToken: string | null;
  twitchClientId: string | null;
  twitchClientSecret: string | null;
  discordBotToken: string | null;
  discordOauthClientId: string | null;
  discordOauthClientSecret: string | null;
  discordOauthRedirectUri: string | null;
  ingestApiToken: string | null;
  maintenanceIntervalSeconds: number;
  analyticsIntervalSeconds: number;
  backupIntervalSeconds: number;
  backupRetentionCount: number;
  rawArchiveDir: string;
  defaultCommunitySlug: string;
  twitchEventsubSecret: string | null;
  twitchEventsubCallbackUrl: string | null;
  twitchOauthRedirectUri: string | null;
  credentialEncryptionKey: string | null;
  legalOrganizationName: string | null;
  legalContactEmail: string | null;
  legalJurisdiction: string | null;
  legalEffectiveDate: string | null;
  moderationShadowMode: boolean;
  shadowReadUpstreamUrl: string | null;
  webReadOnly: boolean;
}

const nullable = (env: Environment, key: string): string | null =>
  env[key] || null;

export class AppSettings implements AppSettingsValues {
  readonly databasePath: AppSettingsValues["databasePath"];
  readonly backupDir: AppSettingsValues["backupDir"];
  readonly dashboardHost: AppSettingsValues["dashboardHost"];
  readonly dashboardPort: AppSettingsValues["dashboardPort"];
  readonly dashboardSessionSecret: AppSettingsValues["dashboardSessionSecret"];
  readonly systemdServiceName: AppSettingsValues["systemdServiceName"];
  readonly logLevel: AppSettingsValues["logLevel"];
  readonly enabledServices: AppSettingsValues["enabledServices"];
  readonly twitchChannels: AppSettingsValues["twitchChannels"];
  readonly twitchJoinCommandChannel:
    AppSettingsValues["twitchJoinCommandChannel"];
  readonly discordGuildIds: AppSettingsValues["discordGuildIds"];
  readonly operatorGuildIds: AppSettingsValues["operatorGuildIds"];
  readonly auditRetentionDays: AppSettingsValues["auditRetentionDays"];
  readonly twitchBotToken: AppSettingsValues["twitchBotToken"];
  readonly twitchRefreshToken: AppSettingsValues["twitchRefreshToken"];
  readonly twitchClientId: AppSettingsValues["twitchClientId"];
  readonly twitchClientSecret: AppSettingsValues["twitchClientSecret"];
  readonly discordBotToken: AppSettingsValues["discordBotToken"];
  readonly discordOauthClientId: AppSettingsValues["discordOauthClientId"];
  readonly discordOauthClientSecret:
    AppSettingsValues["discordOauthClientSecret"];
  readonly discordOauthRedirectUri:
    AppSettingsValues["discordOauthRedirectUri"];
  readonly ingestApiToken: AppSettingsValues["ingestApiToken"];
  readonly maintenanceIntervalSeconds:
    AppSettingsValues["maintenanceIntervalSeconds"];
  readonly analyticsIntervalSeconds:
    AppSettingsValues["analyticsIntervalSeconds"];
  readonly backupIntervalSeconds: AppSettingsValues["backupIntervalSeconds"];
  readonly backupRetentionCount: AppSettingsValues["backupRetentionCount"];
  readonly rawArchiveDir: AppSettingsValues["rawArchiveDir"];
  readonly defaultCommunitySlug: AppSettingsValues["defaultCommunitySlug"];
  readonly twitchEventsubSecret: AppSettingsValues["twitchEventsubSecret"];
  readonly twitchEventsubCallbackUrl:
    AppSettingsValues["twitchEventsubCallbackUrl"];
  readonly twitchOauthRedirectUri: AppSettingsValues["twitchOauthRedirectUri"];
  readonly credentialEncryptionKey:
    AppSettingsValues["credentialEncryptionKey"];
  readonly legalOrganizationName: AppSettingsValues["legalOrganizationName"];
  readonly legalContactEmail: AppSettingsValues["legalContactEmail"];
  readonly legalJurisdiction: AppSettingsValues["legalJurisdiction"];
  readonly legalEffectiveDate: AppSettingsValues["legalEffectiveDate"];
  readonly moderationShadowMode: AppSettingsValues["moderationShadowMode"];
  readonly shadowReadUpstreamUrl: AppSettingsValues["shadowReadUpstreamUrl"];
  readonly webReadOnly: AppSettingsValues["webReadOnly"];

  private constructor(values: AppSettingsValues) {
    this.databasePath = values.databasePath;
    this.backupDir = values.backupDir;
    this.dashboardHost = values.dashboardHost;
    this.dashboardPort = values.dashboardPort;
    this.dashboardSessionSecret = values.dashboardSessionSecret;
    this.systemdServiceName = values.systemdServiceName;
    this.logLevel = values.logLevel;
    this.enabledServices = values.enabledServices;
    this.twitchChannels = values.twitchChannels;
    this.twitchJoinCommandChannel = values.twitchJoinCommandChannel;
    this.discordGuildIds = values.discordGuildIds;
    this.operatorGuildIds = values.operatorGuildIds;
    this.auditRetentionDays = values.auditRetentionDays;
    this.twitchBotToken = values.twitchBotToken;
    this.twitchRefreshToken = values.twitchRefreshToken;
    this.twitchClientId = values.twitchClientId;
    this.twitchClientSecret = values.twitchClientSecret;
    this.discordBotToken = values.discordBotToken;
    this.discordOauthClientId = values.discordOauthClientId;
    this.discordOauthClientSecret = values.discordOauthClientSecret;
    this.discordOauthRedirectUri = values.discordOauthRedirectUri;
    this.ingestApiToken = values.ingestApiToken;
    this.maintenanceIntervalSeconds = values.maintenanceIntervalSeconds;
    this.analyticsIntervalSeconds = values.analyticsIntervalSeconds;
    this.backupIntervalSeconds = values.backupIntervalSeconds;
    this.backupRetentionCount = values.backupRetentionCount;
    this.rawArchiveDir = values.rawArchiveDir;
    this.defaultCommunitySlug = values.defaultCommunitySlug;
    this.twitchEventsubSecret = values.twitchEventsubSecret;
    this.twitchEventsubCallbackUrl = values.twitchEventsubCallbackUrl;
    this.twitchOauthRedirectUri = values.twitchOauthRedirectUri;
    this.credentialEncryptionKey = values.credentialEncryptionKey;
    this.legalOrganizationName = values.legalOrganizationName;
    this.legalContactEmail = values.legalContactEmail;
    this.legalJurisdiction = values.legalJurisdiction;
    this.legalEffectiveDate = values.legalEffectiveDate;
    this.moderationShadowMode = values.moderationShadowMode;
    this.shadowReadUpstreamUrl = values.shadowReadUpstreamUrl;
    this.webReadOnly = values.webReadOnly;
    Object.freeze(this);
  }

  static fromEnv(
    env?: Environment,
    options: { envFile?: string; role?: ServiceRole } = {},
  ): AppSettings {
    const envMap: Record<string, string> = env
      ? { ...env }
      : readProcessEnvironment();
    if (options.envFile) {
      Object.assign(
        envMap,
        readEnvironmentFile(localPath(options.envFile), true),
      );
    }
    const databaseRaw = envMap.QBOT_DATABASE_URL || envMap.QBOT_DATABASE_PATH;
    if (!databaseRaw) {
      throw new ConfigError(
        "QBOT_DATABASE_URL or QBOT_DATABASE_PATH is required",
      );
    }

    const requested = parseCsv(
      envMap.QBOT_ENABLED_SERVICES || "web,jobs,analysis",
    );
    const fleetServices =
      (requested.length
        ? requested
        : ["web", "jobs", "analysis"]) as ServiceRole[];
    const unknown = fleetServices.filter((service) =>
      !SERVICE_ROLES.includes(service)
    );
    if (unknown.length) {
      throw new ConfigError(
        `Unknown services requested: ${
          [...new Set(unknown)].sort().join(", ")
        }`,
      );
    }
    if (
      fleetServices.some((service) =>
        service === "discord" || service === "twitch"
      ) && !fleetServices.includes("analysis")
    ) {
      throw new ConfigError(
        "analysis service is required when a collection service is enabled",
      );
    }
    const enabledServices =
      (options.role ? [options.role] : fleetServices) as ServiceRole[];
    const logLevel = (envMap.QBOT_LOG_LEVEL || "INFO")
      .toLocaleUpperCase() as LogLevel;
    if (!["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"].includes(logLevel)) {
      throw new ConfigError(
        "QBOT_LOG_LEVEL must be one of: CRITICAL, DEBUG, ERROR, INFO, WARNING",
      );
    }
    const databasePath = databaseRaw.startsWith("postgres://") ||
        databaseRaw.startsWith("postgresql://")
      ? databaseRaw
      : localPath(databaseRaw);
    const values: AppSettingsValues = {
      databasePath,
      backupDir: localPath(envMap.QBOT_BACKUP_DIR || "./var/backups"),
      dashboardHost: envMap.QBOT_DASHBOARD_HOST || "127.0.0.1",
      dashboardPort: parseInteger(
        "QBOT_DASHBOARD_PORT",
        envMap.QBOT_DASHBOARD_PORT,
        8080,
      ),
      dashboardSessionSecret: nullable(envMap, "QBOT_DASHBOARD_SESSION_SECRET"),
      systemdServiceName: (envMap.QBOT_SYSTEMD_SERVICE_NAME || "qbot4k.service")
        .trim(),
      logLevel,
      enabledServices: Object.freeze([...enabledServices]),
      twitchChannels: parseCsv(envMap.QBOT_TWITCH_CHANNELS).length
        ? parseCsv(envMap.QBOT_TWITCH_CHANNELS)
        : Object.freeze(["its_not_qwerty"]),
      twitchJoinCommandChannel:
        (envMap.QBOT_TWITCH_JOIN_COMMAND_CHANNEL || "its_not_qwerty").trim(),
      discordGuildIds: parseCsv(envMap.QBOT_DISCORD_GUILD_IDS),
      operatorGuildIds: parseCsv(envMap.QBOT_OPERATOR_GUILD_IDS),
      auditRetentionDays: parseInteger(
        "QBOT_AUDIT_RETENTION_DAYS",
        envMap.QBOT_AUDIT_RETENTION_DAYS,
        365,
      ),
      twitchBotToken: nullable(envMap, "QBOT_TWITCH_BOT_TOKEN"),
      twitchRefreshToken: nullable(envMap, "QBOT_TWITCH_REFRESH_TOKEN"),
      twitchClientId: nullable(envMap, "QBOT_TWITCH_CLIENT_ID"),
      twitchClientSecret: nullable(envMap, "QBOT_TWITCH_CLIENT_SECRET"),
      discordBotToken: nullable(envMap, "QBOT_DISCORD_BOT_TOKEN"),
      discordOauthClientId: nullable(envMap, "QBOT_DISCORD_OAUTH_CLIENT_ID"),
      discordOauthClientSecret: nullable(
        envMap,
        "QBOT_DISCORD_OAUTH_CLIENT_SECRET",
      ),
      discordOauthRedirectUri: nullable(
        envMap,
        "QBOT_DISCORD_OAUTH_REDIRECT_URI",
      ),
      ingestApiToken: nullable(envMap, "QBOT_INGEST_API_TOKEN"),
      maintenanceIntervalSeconds: parseInteger(
        "QBOT_MAINTENANCE_INTERVAL_SECONDS",
        envMap.QBOT_MAINTENANCE_INTERVAL_SECONDS,
        60,
      ),
      analyticsIntervalSeconds: parseInteger(
        "QBOT_ANALYTICS_INTERVAL_SECONDS",
        envMap.QBOT_ANALYTICS_INTERVAL_SECONDS,
        300,
      ),
      backupIntervalSeconds: parseInteger(
        "QBOT_BACKUP_INTERVAL_SECONDS",
        envMap.QBOT_BACKUP_INTERVAL_SECONDS,
        3600,
      ),
      backupRetentionCount: parseInteger(
        "QBOT_BACKUP_RETENTION_COUNT",
        envMap.QBOT_BACKUP_RETENTION_COUNT,
        48,
      ),
      rawArchiveDir: localPath(
        envMap.QBOT_RAW_ARCHIVE_DIR ||
          (databasePath.startsWith("postgres")
            ? "./var/raw-events"
            : resolve(dirname(databasePath), "raw-events")),
      ),
      defaultCommunitySlug: (envMap.QBOT_DEFAULT_COMMUNITY_SLUG || "default")
        .trim().toLocaleLowerCase(),
      twitchEventsubSecret: nullable(envMap, "QBOT_TWITCH_EVENTSUB_SECRET"),
      twitchEventsubCallbackUrl: nullable(
        envMap,
        "QBOT_TWITCH_EVENTSUB_CALLBACK_URL",
      ),
      twitchOauthRedirectUri: nullable(
        envMap,
        "QBOT_TWITCH_OAUTH_REDIRECT_URI",
      ),
      credentialEncryptionKey: nullable(
        envMap,
        "QBOT_CREDENTIAL_ENCRYPTION_KEY",
      ),
      legalOrganizationName: nullable(envMap, "QBOT_LEGAL_ORGANIZATION_NAME"),
      legalContactEmail: nullable(envMap, "QBOT_LEGAL_CONTACT_EMAIL"),
      legalJurisdiction: nullable(envMap, "QBOT_LEGAL_JURISDICTION"),
      legalEffectiveDate: nullable(envMap, "QBOT_LEGAL_EFFECTIVE_DATE"),
      moderationShadowMode: parseBoolean(envMap.QBOT_MODERATION_SHADOW_MODE),
      shadowReadUpstreamUrl: nullable(
        envMap,
        "QBOT_SHADOW_READ_UPSTREAM_URL",
      ),
      webReadOnly: parseBoolean(envMap.QBOT_WEB_READ_ONLY),
    };
    AppSettings.validate(values);
    return new AppSettings(values);
  }

  private static validate(settings: AppSettingsValues): void {
    if (!/^[A-Za-z0-9_.@:-]+\.service$/u.test(settings.systemdServiceName)) {
      throw new ConfigError(
        "QBOT_SYSTEMD_SERVICE_NAME must be a valid .service unit name",
      );
    }
    if (settings.dashboardPort <= 0 || settings.dashboardPort > 65535) {
      throw new ConfigError("QBOT_DASHBOARD_PORT must be between 1 and 65535");
    }
    if (settings.shadowReadUpstreamUrl) {
      let upstream: URL;
      try {
        upstream = new URL(settings.shadowReadUpstreamUrl);
      } catch {
        throw new ConfigError("QBOT_SHADOW_READ_UPSTREAM_URL must be a URL");
      }
      if (
        upstream.protocol !== "http:" ||
        !new Set(["127.0.0.1", "localhost", "[::1]"]).has(upstream.hostname) ||
        upstream.username || upstream.password
      ) {
        throw new ConfigError(
          "QBOT_SHADOW_READ_UPSTREAM_URL must be credential-free loopback HTTP",
        );
      }
    }
    if (settings.auditRetentionDays <= 0) {
      throw new ConfigError(
        "QBOT_AUDIT_RETENTION_DAYS must be greater than zero",
      );
    }
    if (!settings.defaultCommunitySlug) {
      throw new ConfigError("QBOT_DEFAULT_COMMUNITY_SLUG must not be empty");
    }
    if (
      settings.twitchEventsubSecret && settings.twitchEventsubSecret.length < 16
    ) {
      throw new ConfigError(
        "QBOT_TWITCH_EVENTSUB_SECRET must be at least 16 characters",
      );
    }
    for (
      const [name, value] of [
        [
          "QBOT_MAINTENANCE_INTERVAL_SECONDS",
          settings.maintenanceIntervalSeconds,
        ],
        ["QBOT_ANALYTICS_INTERVAL_SECONDS", settings.analyticsIntervalSeconds],
        ["QBOT_BACKUP_INTERVAL_SECONDS", settings.backupIntervalSeconds],
        ["QBOT_BACKUP_RETENTION_COUNT", settings.backupRetentionCount],
      ] as const
    ) {
      if (value <= 0) {
        throw new ConfigError(`${name} must be greater than zero`);
      }
    }
    if (settings.enabledServices.includes("web")) {
      const required = new Map<string, unknown>([
        ["QBOT_DASHBOARD_SESSION_SECRET", settings.dashboardSessionSecret],
        ["QBOT_DISCORD_OAUTH_CLIENT_ID", settings.discordOauthClientId],
        ["QBOT_DISCORD_OAUTH_CLIENT_SECRET", settings.discordOauthClientSecret],
        ["QBOT_LEGAL_ORGANIZATION_NAME", settings.legalOrganizationName],
        ["QBOT_LEGAL_CONTACT_EMAIL", settings.legalContactEmail],
        ["QBOT_LEGAL_JURISDICTION", settings.legalJurisdiction],
        ["QBOT_LEGAL_EFFECTIVE_DATE", settings.legalEffectiveDate],
      ]);
      const missing = [...required].filter(([, value]) => !value).map(([key]) =>
        key
      );
      if (missing.length) {
        throw new ConfigError(
          `Missing web configuration: ${missing.join(", ")}`,
        );
      }
      if (!settings.operatorGuildIds.length) {
        throw new ConfigError(
          "QBOT_OPERATOR_GUILD_IDS is required when web service is enabled",
        );
      }
    }
    if (settings.enabledServices.includes("twitch")) {
      // A static bot token is optional when a stored broadcaster credential
      // from the OAuth flow is available to drive ingestion.
      if (
        !settings.twitchBotToken && !settings.credentialEncryptionKey
      ) {
        throw new ConfigError(
          "QBOT_TWITCH_BOT_TOKEN or a stored Twitch credential (QBOT_CREDENTIAL_ENCRYPTION_KEY with a linked channel) is required when twitch service is enabled",
        );
      }
      if (!settings.twitchJoinCommandChannel) {
        throw new ConfigError(
          "QBOT_TWITCH_JOIN_COMMAND_CHANNEL must not be empty when twitch service is enabled",
        );
      }
      if (
        settings.twitchRefreshToken &&
        (!settings.twitchClientId || !settings.twitchClientSecret)
      ) {
        throw new ConfigError(
          "Missing Twitch refresh configuration: QBOT_TWITCH_CLIENT_ID, QBOT_TWITCH_CLIENT_SECRET",
        );
      }
    }
    if (settings.enabledServices.includes("discord")) {
      if (!settings.discordBotToken) {
        throw new ConfigError(
          "QBOT_DISCORD_BOT_TOKEN is required when discord service is enabled",
        );
      }
      if (
        settings.enabledServices.includes("jobs") &&
        settings.enabledServices.includes("twitch") &&
        !settings.discordGuildIds.length
      ) {
        throw new ConfigError(
          "QBOT_DISCORD_GUILD_IDS is required when jobs, twitch, and discord services are enabled",
        );
      }
    }
  }

  safeSummary(): Record<string, unknown> {
    return {
      database_path: safeDatabasePath(this.databasePath),
      database_backend: this.databasePath.startsWith("postgres")
        ? "postgresql"
        : "sqlite",
      backup_dir: this.backupDir,
      dashboard_host: this.dashboardHost,
      dashboard_port: this.dashboardPort,
      systemd_service_name: this.systemdServiceName,
      log_level: this.logLevel,
      enabled_services: [...this.enabledServices],
      twitch_channels: [...this.twitchChannels],
      twitch_join_command_channel: this.twitchJoinCommandChannel,
      discord_guild_ids: [...this.discordGuildIds],
      operator_guild_ids: [...this.operatorGuildIds],
      audit_retention_days: this.auditRetentionDays,
      web_auth_configured: Boolean(
        this.dashboardSessionSecret && this.discordOauthClientId &&
          this.discordOauthClientSecret,
      ),
      shadow_read_enabled: this.shadowReadUpstreamUrl !== null,
      web_read_only: this.webReadOnly,
      twitch_configured: Boolean(this.twitchBotToken),
      twitch_refresh_configured: Boolean(
        this.twitchRefreshToken && this.twitchClientId &&
          this.twitchClientSecret,
      ),
      discord_configured: Boolean(this.discordBotToken),
      ingest_api_token_configured: Boolean(this.ingestApiToken),
      maintenance_interval_seconds: this.maintenanceIntervalSeconds,
      analytics_interval_seconds: this.analyticsIntervalSeconds,
      backup_interval_seconds: this.backupIntervalSeconds,
      backup_retention_count: this.backupRetentionCount,
      raw_archive_dir: this.rawArchiveDir,
      default_community_slug: this.defaultCommunitySlug,
      twitch_eventsub_configured: Boolean(
        this.twitchEventsubSecret && this.twitchEventsubCallbackUrl,
      ),
      twitch_oauth_configured: Boolean(
        this.twitchClientId && this.twitchClientSecret &&
          this.credentialEncryptionKey,
      ),
      legal_identity_configured: Boolean(
        this.legalOrganizationName && this.legalContactEmail &&
          this.legalJurisdiction && this.legalEffectiveDate,
      ),
    };
  }
}
