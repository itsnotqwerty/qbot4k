import {
  AppSettings,
  SERVICE_ROLES,
  type ServiceRole,
} from "./src/core/config.ts";
import {
  type DatabaseHealthSource,
  PostgresDatabase,
} from "./src/data/database.ts";
import { RoleHealthMonitor } from "./src/core/health.ts";
import { ShutdownController } from "./src/core/lifecycle.ts";
import { StructuredLogger } from "./src/core/logging.ts";
import {
  createDiscordOAuthProvider,
  WebAuthController,
} from "./src/web/web_auth.ts";
import { WebDashboardController } from "./src/web/web_dashboard.ts";
import { WebIntelligenceController } from "./src/web/web_intelligence.ts";
import { WebModerationController } from "./src/web/web_moderation.ts";
import { WebCommandsController } from "./src/web/web_commands.ts";
import { WebAuditController } from "./src/web/web_audit.ts";
import { WebAnnouncementsController } from "./src/web/web_announcements.ts";
import { WebSettingsController } from "./src/web/web_settings.ts";
import {
  FetchIntegrationOAuthGateway,
  WebIntegrationsController,
} from "./src/web/web_integrations.ts";
import { WebLiveOpsController } from "./src/web/web_live_ops.ts";
import { MachineIngestionController } from "./src/jobs/machine_ingestion.ts";
import {
  MaintenanceOrchestrator,
  type MaintenanceRunner,
} from "./src/jobs/maintenance.ts";
import type { AnalyticsRefreshService } from "./src/domain/analytics_persistence.ts";
import { type BackupService, PostgresBackupService } from "./src/ops/backup.ts";
import { JobRegistry, ProcessingWorkerPool } from "./src/jobs/jobs.ts";
import {
  MESSAGE_ANALYSIS_JOB_TYPE,
  type MessageAnalysisShadowRunner,
} from "./src/jobs/message_analysis.ts";
import { SOCIAL_SCORE_JOB_TYPE } from "./src/domain/score_materialization.ts";
import { STREAM_SESSION_JOB_TYPES } from "./src/jobs/stream_sessions.ts";
import {
  DiscordGatewayClient,
  NativeDiscordGatewayTransport,
} from "./src/providers/discord/discord_gateway.ts";
import {
  DISCORD_MESSAGE_JOB_TYPE,
  DISCORD_MODERATION_JOB_TYPE,
  FetchDiscordApi,
} from "./src/providers/discord/discord_actions.ts";
import { TwitchTokenManager } from "./src/providers/twitch/twitch_auth.ts";
import { TokenStore } from "./src/security/token_store.ts";
import {
  FetchTwitchModerationApi,
  TWITCH_MODERATION_JOB_TYPE,
} from "./src/providers/twitch/twitch_actions.ts";
import { TWITCH_MESSAGE_JOB_TYPE } from "./src/providers/twitch/twitch_messages.ts";
import {
  NativeTwitchIrcTransport,
  TwitchIrcClient,
} from "./src/providers/twitch/twitch_irc.ts";
import { TwitchAnnouncementSender } from "./src/providers/twitch/twitch_announcements.ts";
import type { AnnouncementDispatchService } from "./src/jobs/announcement_dispatch.ts";
import type { TwitchStreamPollReport } from "./src/providers/twitch/twitch_stream_polling.ts";
import { ProviderLeaseCoordinator } from "./src/providers/provider_lease_coordinator.ts";
import { createShadowReadHandler } from "./src/ops/shadow_read.ts";
import { createReadOnlyWebHandler } from "./src/web/web_read_only.ts";
import { createSecurityHeadersHandler } from "./src/web/web_security_headers.ts";

export interface RoleService {
  start(signal: AbortSignal): Promise<void>;
  stop(): Promise<void>;
}

export interface RuntimeSettings {
  readonly enabledServices: readonly ServiceRole[];
}

export interface RoleRuntimeOptions {
  readonly once?: boolean;
  readonly writeSnapshot?: (snapshot: unknown) => void;
  readonly onReady?: () => void;
  readonly reliabilityStore?: import("./src/core/health.ts").ReliabilityStore;
  readonly logger?: StructuredLogger;
}

export async function runRole(
  role: ServiceRole,
  settings: RuntimeSettings,
  service: RoleService,
  monitor: RoleHealthMonitor,
  lifecycle: ShutdownController,
  options: RoleRuntimeOptions = {},
): Promise<void> {
  if (!settings.enabledServices.includes(role)) {
    throw new TypeError(`role ${role} is not enabled by QBOT_ENABLED_SERVICES`);
  }
  lifecycle.install();
  let heartbeat: ReturnType<typeof setInterval> | null = null;
  let reliability: ReturnType<typeof setInterval> | null = null;
  try {
    await service.start(lifecycle.abortController.signal);
    monitor.setStatus("ready");
    await monitor.recordHeartbeat();
    heartbeat = setInterval(() => {
      void monitor.recordHeartbeat();
    }, 15_000);
    const reliabilityStore = options.reliabilityStore;
    if (reliabilityStore) {
      const { recordReliabilityBuckets } = await import(
        "./src/core/health.ts"
      );
      let reliabilityFailures = 0;
      const record = () =>
        recordReliabilityBuckets(monitor, reliabilityStore).catch((error) => {
          // Best-effort, but log the first few failures so a stale schema
          // (e.g. missing observer column) is diagnosable instead of silent.
          reliabilityFailures += 1;
          if (reliabilityFailures <= 3) {
            (options.logger ?? console).error(
              "reliability bucket recording failed",
              error,
            );
          }
        });
      await record();
      reliability = setInterval(record, 60_000);
    }
    options.onReady?.();
    if (options.once) {
      options.writeSnapshot?.(await monitor.snapshot());
      return;
    }
    await lifecycle.wait();
  } finally {
    if (heartbeat) clearInterval(heartbeat);
    if (reliability) clearInterval(reliability);
    monitor.setStatus("stopping");
    await service.stop();
    monitor.setStatus("down");
    await monitor.recordHeartbeat();
    if (options.reliabilityStore) {
      const { recordReliabilityBuckets } = await import(
        "./src/core/health.ts"
      );
      await recordReliabilityBuckets(monitor, options.reliabilityStore).catch(
        () => {},
      );
    }
    lifecycle.dispose();
  }
}

export class FoundationRoleService implements RoleService {
  start(_signal: AbortSignal): Promise<void> {
    return Promise.resolve();
  }
  stop(): Promise<void> {
    return Promise.resolve();
  }
}

export interface WorkerPool {
  run(signal: AbortSignal): Promise<void>;
}

export class AnalysisRoleService implements RoleService {
  private readonly abortController = new AbortController();
  private running: Promise<void> | null = null;

  constructor(private readonly pools: WorkerPool | readonly WorkerPool[]) {}

  start(signal: AbortSignal): Promise<void> {
    const combined = AbortSignal.any([signal, this.abortController.signal]);
    const pools = Array.isArray(this.pools) ? this.pools : [this.pools];
    this.running = Promise.all(pools.map((pool) => pool.run(combined)))
      .then(() => undefined);
    return Promise.resolve();
  }

  async stop(): Promise<void> {
    this.abortController.abort();
    await this.running;
    this.running = null;
  }
}

export class DiscordRoleService implements RoleService {
  private readonly abortController = new AbortController();
  private running: Promise<void> | null = null;

  constructor(private readonly pools: readonly WorkerPool[]) {}

  start(signal: AbortSignal): Promise<void> {
    const combined = AbortSignal.any([
      signal,
      this.abortController.signal,
    ]);
    this.running = Promise.all(this.pools.map((pool) => pool.run(combined)))
      .then(() => undefined);
    return Promise.resolve();
  }

  async stop(): Promise<void> {
    this.abortController.abort();
    await this.running;
    this.running = null;
  }
}

export class TwitchRoleService implements RoleService {
  private readonly abortController = new AbortController();
  private running: Promise<void> | null = null;

  constructor(private readonly clients: WorkerPool | readonly WorkerPool[]) {}

  start(signal: AbortSignal): Promise<void> {
    const combined = AbortSignal.any([
      signal,
      this.abortController.signal,
    ]);
    const clients = Array.isArray(this.clients) ? this.clients : [this.clients];
    this.running = Promise.all(clients.map((client) => client.run(combined)))
      .then(() => undefined);
    return Promise.resolve();
  }

  async stop(): Promise<void> {
    this.abortController.abort();
    await this.running;
    this.running = null;
  }
}

export class TwitchAnnouncementWorker implements WorkerPool {
  constructor(
    private readonly client: TwitchIrcClient,
    private readonly dispatcher: AnnouncementDispatchService,
    private readonly intervalMilliseconds = 500,
    private readonly onError: (error: unknown) => void = () => undefined,
  ) {}

  async run(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      if (this.client.health().status === "ready") {
        try {
          await this.dispatcher.dispatch();
        } catch (error) {
          this.onError(error);
        }
      }
      await abortableDelay(this.intervalMilliseconds, signal);
    }
  }
}

export class TwitchStreamPollingWorker implements WorkerPool {
  constructor(
    private readonly polling: {
      poll(now?: Date): Promise<TwitchStreamPollReport>;
    },
    private readonly intervalMilliseconds = 60_000,
    private readonly onError: (error: unknown) => void = () => undefined,
  ) {}

  async run(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      try {
        await this.polling.poll();
      } catch (error) {
        this.onError(error);
      }
      await abortableDelay(this.intervalMilliseconds, signal);
    }
  }
}

export class ShadowAnalysisWorker implements WorkerPool {
  constructor(
    private readonly runner: MessageAnalysisShadowRunner,
    private readonly intervalMilliseconds = 500,
    private readonly onError: (error: unknown) => void = () => undefined,
  ) {}

  async run(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      try {
        if (await this.runner.runNext()) continue;
      } catch (error) {
        this.onError(error);
      }
      await abortableDelay(this.intervalMilliseconds, signal);
    }
  }
}

export class JobsRoleService implements RoleService {
  private readonly abortController = new AbortController();
  private running: Promise<void> | null = null;

  constructor(
    private readonly runner: MaintenanceRunner,
    private readonly analytics: AnalyticsRefreshService,
    private readonly backups: BackupService,
    private readonly settings: Pick<
      AppSettings,
      | "maintenanceIntervalSeconds"
      | "analyticsIntervalSeconds"
      | "backupIntervalSeconds"
      | "auditRetentionDays"
      | "rawArchiveDir"
    >,
    private readonly logger: StructuredLogger,
  ) {}

  async start(signal: AbortSignal): Promise<void> {
    const combined = AbortSignal.any([
      signal,
      this.abortController.signal,
    ]);
    await this.runMaintenance();
    await this.runAnalytics();
    await this.runBackup();
    this.running = Promise.all([
      this.runLoop(
        "maintenance",
        this.settings.maintenanceIntervalSeconds,
        () => this.runMaintenance(),
        combined,
      ),
      this.runLoop(
        "analytics",
        this.settings.analyticsIntervalSeconds,
        () => this.runAnalytics(),
        combined,
      ),
      this.runLoop(
        "backup",
        this.settings.backupIntervalSeconds,
        () => this.runBackup(),
        combined,
      ),
    ]).then(() => undefined);
  }

  async stop(): Promise<void> {
    this.abortController.abort();
    await this.running;
    this.running = null;
  }

  private async runLoop(
    name: string,
    intervalSeconds: number,
    operation: () => Promise<void>,
    signal: AbortSignal,
  ): Promise<void> {
    while (!signal.aborted) {
      await abortableDelay(intervalSeconds * 1000, signal);
      if (signal.aborted) return;
      try {
        await operation();
      } catch (error) {
        this.logger.error(`${name} run failed`, error);
      }
    }
  }

  private async runMaintenance(): Promise<void> {
    const report = await this.runner.run(
      new Date(),
      this.settings.auditRetentionDays,
      this.settings.rawArchiveDir,
    );
    this.logger.info("maintenance run complete", { ...report });
  }

  private async runAnalytics(): Promise<void> {
    const report = await this.analytics.refresh(new Date());
    this.logger.info("analytics run complete", { ...report });
  }

  private async runBackup(): Promise<void> {
    const report = await this.backups.create(new Date());
    this.logger.info("backup run complete", { ...report });
  }
}

export class WebRoleService implements RoleService {
  private server: Deno.HttpServer | null = null;

  constructor(
    private readonly settings: AppSettings,
    private readonly monitor: RoleHealthMonitor,
    private readonly database: PostgresDatabase,
    private readonly tokenStore: TokenStore | null = null,
  ) {}

  async start(signal: AbortSignal): Promise<void> {
    const { createApp } = await import("./main.ts");
    const auth = new WebAuthController(
      this.settings,
      createDiscordOAuthProvider(this.settings),
      this.database.operatorAuthStore(),
    );
    const dashboard = new WebDashboardController(
      auth,
      this.database.dashboardQueryService(),
      this.database.dashboardOperations(this.settings.systemdServiceName),
    );
    const moderation = new WebModerationController(
      auth,
      this.database.moderationService(),
    );
    const intelligence = new WebIntelligenceController(
      auth,
      this.database.intelligenceService(),
    );
    const commands = new WebCommandsController(
      auth,
      this.database.commandRegistry(),
    );
    const settingsService = this.database.settingsService();
    const audit = new WebAuditController(
      auth,
      this.database.auditService(),
      async (communityId) => {
        const snapshot = await settingsService.snapshot(communityId);
        return String(snapshot.community.timezone ?? "UTC");
      },
    );
    const announcements = new WebAnnouncementsController(
      auth,
      this.database.announcementService(),
    );
    const webSettings = new WebSettingsController(
      auth,
      this.database.settingsService(),
    );
    const integrations = new WebIntegrationsController(
      auth,
      this.database.integrationService(),
      new FetchIntegrationOAuthGateway(this.settings),
      this.settings,
      {
        reconcile: (input) =>
          this.database.reconcileTwitchEventSub({
            ...input,
            clientId: this.settings.twitchClientId ?? "",
            clientSecret: this.settings.twitchClientSecret ?? "",
            onTokenRefresh: (accessToken, refreshToken) =>
              this.tokenStore?.persistRefreshedTwitchTokens(
                accessToken,
                refreshToken,
              ),
          }),
      },
    );
    const liveOps = new WebLiveOpsController(
      auth,
      this.database.liveOpsService(),
      this.database.twitchControlGateway(
        new TwitchTokenManager({
          initialAccessToken: this.settings.twitchBotToken ?? "",
          refreshToken: this.settings.twitchRefreshToken,
          clientId: this.settings.twitchClientId,
          clientSecret: this.settings.twitchClientSecret,
          onTokenRefresh: (accessToken, refreshToken) =>
            this.tokenStore?.persistRefreshedTwitchTokens(
              accessToken,
              refreshToken,
            ),
        }),
      ),
      this.settings.twitchChannels[0] ?? "",
    );
    const machineIngestion = new MachineIngestionController(
      auth,
      this.database.machineIngestionService(),
      this.database.observationCollector(),
      this.settings,
    );
    const freshApp = createApp(
      this.monitor,
      auth,
      this.settings,
      dashboard,
      moderation,
      commands,
      audit,
      announcements,
      webSettings,
      integrations,
      liveOps,
      machineIngestion,
      intelligence,
      this.database,
    );
    const { attachProdBuildCache } = await import("./main.ts");
    await attachProdBuildCache(freshApp);
    const handler = freshApp.handler();
    const shadowHandler = this.settings.shadowReadUpstreamUrl
      ? createShadowReadHandler(
        handler,
        this.settings.shadowReadUpstreamUrl,
        this.database.shadowComparisonStore(),
      )
      : handler;
    const fencedHandler = this.settings.webReadOnly
      ? createReadOnlyWebHandler(shadowHandler)
      : shadowHandler;
    const runtimeHandler = createSecurityHeadersHandler(fencedHandler);
    this.server = Deno.serve(
      {
        hostname: this.settings.dashboardHost,
        port: this.settings.dashboardPort,
        signal,
        onListen: () => undefined,
      },
      runtimeHandler,
    );
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    await this.server.shutdown();
    this.server = null;
  }
}

function parseArguments(args: readonly string[]): {
  role: ServiceRole;
  once: boolean;
  envFile?: string;
} {
  const role = args[0];
  if (!SERVICE_ROLES.includes(role as ServiceRole)) {
    throw new TypeError(`role must be one of: ${SERVICE_ROLES.join(", ")}`);
  }
  const envFileArgument = args.find((argument) =>
    argument.startsWith("--env-file=")
  );
  return {
    role: role as ServiceRole,
    once: args.includes("--once"),
    ...(envFileArgument
      ? { envFile: envFileArgument.slice("--env-file=".length) }
      : {}),
  };
}

export async function main(args = Deno.args): Promise<void> {
  const parsed = parseArguments(args);
  const settings = AppSettings.fromEnv(undefined, {
    envFile: parsed.envFile,
    role: parsed.role,
  });
  const tokenStore = parsed.envFile ? new TokenStore(parsed.envFile) : null;
  if (!settings.databasePath.startsWith("postgres")) {
    throw new TypeError("Deno runtime requires PostgreSQL");
  }
  const database = new PostgresDatabase(settings.databasePath);
  const logger = new StructuredLogger(
    `qbot4k.${parsed.role}`,
    settings.logLevel,
  );
  const lifecycle = new ShutdownController(logger);
  const monitor = new RoleHealthMonitor(
    database as DatabaseHealthSource,
    parsed.role,
    new Date(),
    database,
  );
  // Prefer the stored broadcaster credential for Twitch when no static bot
  // token is configured, so the OAuth-linked channel drives ingestion.
  const twitchStoredTokens = parsed.role === "twitch" &&
      !settings.twitchBotToken?.trim() && settings.credentialEncryptionKey
    ? await database.twitchInstallationTokens(
      settings.credentialEncryptionKey,
      settings.twitchChannels[0],
    )
    : null;
  const service = parsed.role === "web"
    ? new WebRoleService(settings, monitor, database, tokenStore)
    : parsed.role === "jobs"
    ? new JobsRoleService(
      new MaintenanceOrchestrator(
        database.processingJobStore(),
        database.retentionService(),
        database.rawArchiveService(),
        database.metricsRollupService(),
        database.checkpointReminderService(),
      ),
      database.analyticsRefreshService(),
      new PostgresBackupService(
        settings.databasePath,
        settings.backupDir,
        settings.backupRetentionCount,
      ),
      settings,
      logger,
    )
    : parsed.role === "analysis"
    ? new AnalysisRoleService([
      new ProcessingWorkerPool(
        database.processingJobStore(),
        new JobRegistry()
          .register(
            MESSAGE_ANALYSIS_JOB_TYPE,
            database.messageAnalysisHandler(settings.moderationShadowMode),
          )
          .register(
            SOCIAL_SCORE_JOB_TYPE,
            database.socialScoreHandler(),
          )
          .register(
            STREAM_SESSION_JOB_TYPES[0],
            database.streamSessionHandler(),
          )
          .register(
            STREAM_SESSION_JOB_TYPES[1],
            database.streamSessionHandler(),
          )
          .register(
            STREAM_SESSION_JOB_TYPES[2],
            database.streamSessionHandler(),
          ),
        "analysis",
        `analysis-${Deno.pid}`,
        2,
        500,
        60_000,
        40_000,
        (error) => logger.error("analysis worker failed", error),
      ),
      new ShadowAnalysisWorker(
        database.messageAnalysisShadowRunner(),
        500,
        (error) => logger.error("analysis shadow worker failed", error),
      ),
    ])
    : parsed.role === "discord"
    ? new DiscordRoleService([
      new ProviderLeaseCoordinator(
        "discord",
        `discord-${Deno.pid}`,
        database.providerOwnershipLease(),
        120,
        40_000,
        (error) => logger.error("Discord provider lease failed", error),
      ),
      new DiscordGatewayClient(
        settings.discordBotToken!,
        new NativeDiscordGatewayTransport(),
        database.discordIngestionService(
          new FetchDiscordApi(settings.discordBotToken!),
        ),
        5_000,
        settings.discordGuildIds,
        database.discordInstallationHealth(),
      ),
      new ProcessingWorkerPool(
        database.processingJobStore(),
        new JobRegistry()
          .register(
            DISCORD_MESSAGE_JOB_TYPE,
            database.discordMessageHandler(
              new FetchDiscordApi(settings.discordBotToken!),
            ),
          )
          .register(
            DISCORD_MODERATION_JOB_TYPE,
            database.discordModerationHandler(
              new FetchDiscordApi(settings.discordBotToken!),
            ),
          ),
        "action",
        `discord-action-${Deno.pid}`,
        2,
        500,
        60_000,
        40_000,
        (error) => logger.error("Discord action worker failed", error),
      ),
    ])
    : parsed.role === "twitch"
    ? (() => {
      const tokens = new TwitchTokenManager({
        initialAccessToken: settings.twitchBotToken?.trim() ||
          twitchStoredTokens?.accessToken || "",
        refreshToken: settings.twitchBotToken?.trim()
          ? settings.twitchRefreshToken
          : twitchStoredTokens?.refreshToken ?? settings.twitchRefreshToken,
        clientId: settings.twitchClientId,
        clientSecret: settings.twitchClientSecret,
        onTokenRefresh: (accessToken, refreshToken) =>
          tokenStore?.persistRefreshedTwitchTokens(accessToken, refreshToken),
      });
      const irc = new TwitchIrcClient(
        tokens,
        new NativeTwitchIrcTransport(),
        database.twitchIngestionService(settings.twitchChannels),
        1_000,
        database.twitchInstallationHealth(),
      );
      return new TwitchRoleService([
        new ProviderLeaseCoordinator(
          "twitch",
          `twitch-${Deno.pid}`,
          database.providerOwnershipLease(),
          120,
          40_000,
          (error) => logger.error("Twitch provider lease failed", error),
        ),
        irc,
        new TwitchAnnouncementWorker(
          irc,
          database.announcementDispatcher(new TwitchAnnouncementSender(irc)),
          500,
          (error) => logger.error("Twitch announcement worker failed", error),
        ),
        new TwitchStreamPollingWorker(
          database.twitchStreamPollingService(tokens),
          60_000,
          (error) => logger.error("Twitch stream polling failed", error),
        ),
        new ProcessingWorkerPool(
          database.processingJobStore(),
          new JobRegistry().register(
            TWITCH_MODERATION_JOB_TYPE,
            database.twitchModerationHandler(
              new FetchTwitchModerationApi(tokens),
            ),
          ).register(
            TWITCH_MESSAGE_JOB_TYPE,
            database.twitchMessageHandler(irc),
          ),
          "action",
          `twitch-action-${Deno.pid}`,
          2,
          500,
          60_000,
          40_000,
          (error) => logger.error("Twitch action worker failed", error),
        ),
      ]);
    })()
    : new FoundationRoleService();
  logger.info("starting role", { role: parsed.role });
  try {
    await runRole(parsed.role, settings, service, monitor, lifecycle, {
      once: parsed.once,
      writeSnapshot: (snapshot) => console.log(JSON.stringify(snapshot)),
      onReady: () => logger.info("role ready", { role: parsed.role }),
      reliabilityStore: database,
      logger,
    });
  } finally {
    await database.close();
  }
}

if (import.meta.main) {
  await main();
}

function abortableDelay(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(done, Math.max(0, milliseconds));
    signal.addEventListener("abort", done, { once: true });
    function done() {
      clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}
