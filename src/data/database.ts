import postgres from "postgres";
import { TenantContext } from "../core/contexts.ts";
import { FernetCipher } from "../security/fernet.ts";
import { OperatorAuthRepository, ScopedRepository } from "./repository.ts";
import type { OperatorAuthStore } from "../web/web_auth.ts";
import {
  DashboardQueryRepository,
  type DashboardQueryService,
} from "../web/web_queries.ts";
import {
  type ModerationService,
  PostgresModerationRepository,
} from "../domain/moderation.ts";
import {
  type CommandRegistry,
  PostgresCommandRegistry,
} from "../web/web_commands.ts";
import {
  type AuditService,
  PostgresAuditRepository,
} from "../web/web_audit.ts";
import {
  type AnnouncementService,
  PostgresAnnouncementRepository,
} from "../web/web_announcements.ts";
import {
  type OnboardingService,
  PostgresOnboardingRepository,
} from "../web/web_onboarding.ts";
import {
  PostgresSettingsRepository,
  type SettingsService,
} from "../web/web_settings.ts";
import {
  type IntegrationService,
  PostgresIntegrationRepository,
} from "../web/web_integrations.ts";
import {
  type LiveOpsService,
  PostgresLiveOpsRepository,
} from "../web/web_live_ops.ts";
import { PostgresTwitchControlGateway } from "../providers/twitch/twitch_control.ts";
import { TwitchTokenManager } from "../providers/twitch/twitch_auth.ts";
import { PostgresTwitchEventSubReconciler } from "../providers/twitch/twitch_eventsub_control.ts";
import type { TwitchGrant } from "../web/web_integrations.ts";
import {
  PostgresTwitchStreamPoller,
  type TwitchStreamPollReport,
} from "../providers/twitch/twitch_stream_polling.ts";
import {
  PostgresProviderOwnershipLease,
  type ProviderOwnershipLease,
} from "../providers/provider_ownership.ts";
import {
  type JobHandler,
  PostgresProcessingJobRepository,
  type ProcessingJobStore,
} from "../jobs/jobs.ts";
import { PostgresStreamSessionRepository } from "../jobs/stream_sessions.ts";
import {
  type MessageAnalysisShadowRunner,
  PostgresMessageAnalysisRepository,
  PostgresMessageAnalysisShadowRunner,
} from "../jobs/message_analysis.ts";
import {
  type DiscordIngestionService,
  PostgresDiscordIngestionService,
  PostgresDiscordInstallationHealth,
} from "../providers/discord/discord_ingestion.ts";
import type { DiscordInstallationHealthSink } from "../providers/discord/discord_gateway.ts";
import {
  PostgresTwitchIngestionService,
  PostgresTwitchInstallationHealth,
  type TwitchIngestionService,
} from "../providers/twitch/twitch_ingestion.ts";
import type { TwitchInstallationHealthSink } from "../providers/twitch/twitch_irc.ts";
import {
  type DiscordApi,
  PostgresDiscordActionRepository,
} from "../providers/discord/discord_actions.ts";
import {
  PostgresTwitchActionRepository,
  type TwitchModerationApi,
} from "../providers/twitch/twitch_actions.ts";
import {
  PostgresTwitchMessageRepository,
  type TwitchMessageSender,
} from "../providers/twitch/twitch_messages.ts";
import {
  PostgresSocialScoreRepository,
} from "../domain/score_materialization.ts";
import {
  type ObservationCollector,
  PostgresObservationRepository,
} from "../domain/observations.ts";
import {
  type MachineIngestionService,
  PostgresMachineIngestionRepository,
} from "../jobs/machine_ingestion.ts";
import {
  PostgresRetentionRepository,
  type RetentionService,
} from "../ops/retention.ts";
import {
  PostgresRawArchiveRepository,
  type RawArchiveService,
} from "../ops/raw_archive.ts";
import {
  type NotificationService,
  PostgresNotificationRepository,
} from "../domain/notifications.ts";
import {
  type MetricsRollupService,
  PostgresMetricsRollupRepository,
} from "../jobs/maintenance.ts";
import {
  type AnnouncementDispatchService,
  type AnnouncementSender,
  PostgresAnnouncementDispatcher,
} from "../jobs/announcement_dispatch.ts";
import { PostgresDashboardOperations } from "../web/dashboard_operations.ts";
import type { DashboardOperations } from "../web/web_dashboard.ts";
import {
  PostgresShadowComparisonStore,
  type ShadowComparisonStore,
} from "../ops/shadow_read.ts";
import {
  type CheckpointReminderService,
  type OnboardingAutomationService,
  type OnboardingRoleGateway,
  PostgresOnboardingAutomation,
} from "../domain/onboarding_automation.ts";
import {
  type AnalyticsRefreshService,
  PostgresAnalyticsRepository,
} from "../domain/analytics_persistence.ts";
import {
  type IntelligenceService,
  PostgresIntelligenceRepository,
} from "../web/web_intelligence.ts";

export type DatabaseRow = Readonly<Record<string, unknown>>;
export type DatabaseParameter =
  | string
  | number
  | boolean
  | Uint8Array
  | Date
  | null;

export interface DatabaseConnection {
  query(
    sql: string,
    parameters?: readonly DatabaseParameter[],
  ): Promise<readonly DatabaseRow[]>;
  transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T>;
}

export const EXPECTED_SCHEMA_VERSION = 28;

export interface DatabaseHealth {
  readonly status: "ready" | "degraded";
  readonly backend: "postgresql";
  readonly path: string;
  readonly tableCount: number;
  readonly integrity: "ok" | "migration_pending" | "unavailable";
  readonly schemaVersion: number;
  readonly error?: string;
}

export interface DatabaseHealthSource {
  health(): Promise<DatabaseHealth>;
}

export async function probeDatabaseHealth(
  connection: DatabaseConnection,
  databaseUrl: string,
): Promise<DatabaseHealth> {
  try {
    const rows = await connection.query(
      `SELECT
         (SELECT COUNT(*) FROM information_schema.tables
           WHERE table_schema = 'public' AND table_type = 'BASE TABLE') AS table_count,
         (SELECT COALESCE(MAX(version), 0) FROM schema_migrations) AS schema_version`,
    );
    const tableCount = Number(rows[0]?.table_count ?? 0);
    const schemaVersion = Number(rows[0]?.schema_version ?? 0);
    const ready = Number.isInteger(schemaVersion) &&
      schemaVersion === EXPECTED_SCHEMA_VERSION;
    return Object.freeze({
      status: ready ? "ready" : "degraded",
      backend: "postgresql",
      path: databaseUrl,
      tableCount: Number.isInteger(tableCount) ? tableCount : 0,
      integrity: ready ? "ok" : "migration_pending",
      schemaVersion: Number.isInteger(schemaVersion) ? schemaVersion : 0,
    });
  } catch (error) {
    return Object.freeze({
      status: "degraded",
      backend: "postgresql",
      path: databaseUrl,
      tableCount: 0,
      integrity: "unavailable",
      schemaVersion: 0,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

type PostgresSql = ReturnType<typeof postgres>;
type ReservedPostgresSql = Awaited<ReturnType<PostgresSql["reserve"]>>;

class PostgresConnection implements DatabaseConnection {
  constructor(private readonly sql: PostgresSql | ReservedPostgresSql) {}

  async query(
    query: string,
    parameters: readonly DatabaseParameter[] = [],
  ): Promise<readonly DatabaseRow[]> {
    const rows = await this.sql.unsafe(query, [...parameters]);
    return rows.map((row) => Object.freeze({ ...row }));
  }

  async transaction<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    if ("begin" in this.sql && typeof this.sql.begin === "function") {
      const result = await this.sql.begin(async (transaction) => {
        return await callback(new PostgresConnection(transaction));
      });
      return result as T;
    }

    await this.sql.unsafe("BEGIN");
    try {
      const result = await callback(this);
      await this.sql.unsafe("COMMIT");
      return result;
    } catch (error) {
      await this.sql.unsafe("ROLLBACK");
      throw error;
    }
  }
}

export class PostgresDatabase {
  private readonly pool: PostgresSql;
  private readonly healthTarget: string;

  constructor(databaseUrl: string, options: { maxConnections?: number } = {}) {
    if (
      !databaseUrl.startsWith("postgres://") &&
      !databaseUrl.startsWith("postgresql://")
    ) {
      throw new TypeError("PostgreSQL database URL is required");
    }
    const parsedUrl = new URL(databaseUrl);
    this.healthTarget =
      `${parsedUrl.protocol}//${parsedUrl.host}${parsedUrl.pathname}`;
    this.pool = postgres(databaseUrl, {
      host: parsedUrl.hostname,
      port: Number(parsedUrl.port || "5432"),
      username: decodeURIComponent(parsedUrl.username),
      database: decodeURIComponent(parsedUrl.pathname.slice(1)),
      password: parsedUrl.password
        ? decodeURIComponent(parsedUrl.password)
        : () => "",
      max: options.maxConnections ?? 10,
      transform: { undefined: null },
    });
  }

  async request<T>(
    tenant: TenantContext,
    callback: (repository: ScopedRepository) => Promise<T>,
  ): Promise<T> {
    const reserved = await this.pool.reserve();
    try {
      return await callback(
        new ScopedRepository(new PostgresConnection(reserved), tenant),
      );
    } finally {
      reserved.release();
    }
  }

  async health(): Promise<DatabaseHealth> {
    let reserved: ReservedPostgresSql | undefined;
    try {
      reserved = await this.pool.reserve();
      return await probeDatabaseHealth(
        new PostgresConnection(reserved),
        this.healthTarget,
      );
    } catch (error) {
      return Object.freeze({
        status: "degraded",
        backend: "postgresql",
        path: this.healthTarget,
        tableCount: 0,
        integrity: "unavailable",
        schemaVersion: 0,
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      reserved?.release();
    }
  }

  processingJobStore(): ProcessingJobStore {
    const request = async <T>(
      callback: (repository: PostgresProcessingJobRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresProcessingJobRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      enqueue: (input) => request((repository) => repository.enqueue(input)),
      claim: (stage, workerId) =>
        request((repository) => repository.claim(stage, workerId)),
      complete: (jobId, workerId) =>
        request((repository) => repository.complete(jobId, workerId)),
      renewLease: (jobId, workerId, leaseSeconds) =>
        request((repository) =>
          repository.renewLease(jobId, workerId, leaseSeconds)
        ),
      retry: (jobId, workerId, error, delaySeconds) =>
        request((repository) =>
          repository.retry(jobId, workerId, error, delaySeconds)
        ),
      fail: (jobId, workerId, error) =>
        request((repository) => repository.fail(jobId, workerId, error)),
      recoverExpired: () =>
        request((repository) => repository.recoverExpired()),
      replayDeadLetter: (deadLetterId, replayedAt) =>
        request((repository) =>
          repository.replayDeadLetter(deadLetterId, replayedAt)
        ),
    };
  }

  messageAnalysisHandler(forceShadow = false): JobHandler {
    return async (job) => {
      const reserved = await this.pool.reserve();
      try {
        await new PostgresMessageAnalysisRepository(
          new PostgresConnection(reserved),
          forceShadow,
        ).handle(job);
      } finally {
        reserved.release();
      }
    };
  }

  messageAnalysisShadowRunner(): MessageAnalysisShadowRunner {
    return {
      runNext: async () => {
        const reserved = await this.pool.reserve();
        try {
          return await new PostgresMessageAnalysisShadowRunner(
            new PostgresConnection(reserved),
          ).runNext();
        } finally {
          reserved.release();
        }
      },
    };
  }

  discordIngestionService(discordApi?: DiscordApi): DiscordIngestionService {
    const channelName = discordApi
      ? async (channelId: string) => {
        const channel = await discordApi.getChannel(channelId);
        return channel
          ? {
            name: String(channel.name ?? channelId),
            type: Number(channel.type ?? 0),
          }
          : null;
      }
      : undefined;
    return {
      ingest: (eventName, data) =>
        this.withConnection((connection) =>
          new PostgresDiscordIngestionService(
            connection,
            new PostgresObservationRepository(connection),
            channelName,
          ).ingest(eventName, data)
        ),
    };
  }

  providerOwnershipLease(): ProviderOwnershipLease {
    const request = <T>(
      callback: (lease: PostgresProviderOwnershipLease) => Promise<T>,
    ) =>
      this.withConnection((connection) =>
        callback(new PostgresProviderOwnershipLease(connection))
      );
    return {
      installations: (platform) =>
        request((lease) => lease.installations(platform)),
      acquire: (id, holder, seconds) =>
        request((lease) => lease.acquire(id, holder, seconds)),
      renew: (id, holder, seconds) =>
        request((lease) => lease.renew(id, holder, seconds)),
      release: (id, holder) => request((lease) => lease.release(id, holder)),
      owns: (id, holder) => request((lease) => lease.owns(id, holder)),
      active: (id) => request((lease) => lease.active(id)),
      releaseAll: (holder) => request((lease) => lease.releaseAll(holder)),
    };
  }

  discordInstallationHealth(): DiscordInstallationHealthSink {
    return {
      ready: (guildIds) =>
        this.withConnection((connection) =>
          new PostgresDiscordInstallationHealth(connection).ready(guildIds)
        ),
      failed: (error) =>
        this.withConnection((connection) =>
          new PostgresDiscordInstallationHealth(connection).failed(error)
        ),
    };
  }

  discordMessageHandler(api: DiscordApi): JobHandler {
    return (job) =>
      this.withConnection((connection) =>
        new PostgresDiscordActionRepository(connection, api).sendMessage(job)
      );
  }

  discordModerationHandler(api: DiscordApi): JobHandler {
    return (job) =>
      this.withConnection((connection) =>
        new PostgresDiscordActionRepository(connection, api).moderate(job)
      );
  }

  twitchModerationHandler(api: TwitchModerationApi): JobHandler {
    return (job) =>
      this.withConnection((connection) =>
        new PostgresTwitchActionRepository(connection, api).moderate(job)
      );
  }

  twitchMessageHandler(sender: TwitchMessageSender): JobHandler {
    return (job) =>
      this.withConnection((connection) =>
        new PostgresTwitchMessageRepository(connection, sender).sendMessage(job)
      );
  }

  socialScoreHandler(): JobHandler {
    return async (job) => {
      await this.withConnection((connection) =>
        new PostgresSocialScoreRepository(connection).calculate(
          Number(job.payload.user_id),
        )
      );
    };
  }

  streamSessionHandler(): JobHandler {
    return async (job) => {
      await this.withConnection((connection) =>
        new PostgresStreamSessionRepository(connection).handle(job)
      );
    };
  }

  twitchIngestionService(
    bootstrapChannels: readonly string[] = [],
  ): TwitchIngestionService {
    return {
      channels: async () => {
        const channels = await this.withConnection((connection) =>
          new PostgresTwitchIngestionService(
            connection,
            new PostgresObservationRepository(connection),
          ).channels()
        );
        return channels.length
          ? channels
          : Object.freeze([...bootstrapChannels]);
      },
      ingest: (payload) =>
        this.withConnection((connection) =>
          new PostgresTwitchIngestionService(
            connection,
            new PostgresObservationRepository(connection),
          ).ingest(payload)
        ),
    };
  }

  twitchInstallationHealth(): TwitchInstallationHealthSink {
    return {
      ready: (channels) =>
        this.withConnection((connection) =>
          new PostgresTwitchInstallationHealth(connection).ready(channels)
        ),
      failed: (error) =>
        this.withConnection((connection) =>
          new PostgresTwitchInstallationHealth(connection).failed(error)
        ),
    };
  }

  async twitchInstallationTokens(
    encryptionKey: string,
    broadcasterLogin?: string,
  ): Promise<{ accessToken: string; refreshToken: string | null } | null> {
    return await this.withConnection(async (connection) => {
      const row = (await connection.query(
        `SELECT c.access_token_ciphertext, c.refresh_token_ciphertext
           FROM installation_credentials AS c
           JOIN community_installations AS i ON i.id = c.installation_id
          WHERE i.platform = 'twitch'
            AND i.status IN ('pending', 'active', 'degraded')
            AND ($1 = '' OR LOWER(i.display_name) = LOWER($1)
              OR LOWER(i.metadata_json::jsonb->>'broadcaster_login') = LOWER($1))
          ORDER BY i.updated_at DESC, i.id DESC LIMIT 1`,
        [broadcasterLogin ?? ""],
      ))[0];
      if (!row) return null;
      const cipher = await FernetCipher.fromKey(encryptionKey);
      const decode = (value: unknown): string =>
        value instanceof Uint8Array
          ? new TextDecoder().decode(value)
          : String(value);
      return {
        accessToken: await cipher.decrypt(decode(row.access_token_ciphertext)),
        refreshToken: row.refresh_token_ciphertext == null
          ? null
          : await cipher.decrypt(decode(row.refresh_token_ciphertext)),
      };
    });
  }

  twitchStreamPollingService(
    tokens: TwitchTokenManager,
  ): { poll(now?: Date): Promise<TwitchStreamPollReport> } {
    return {
      poll: (now) =>
        this.withConnection((connection) =>
          new PostgresTwitchStreamPoller(
            connection,
            new PostgresObservationRepository(connection),
            tokens,
          ).poll(now)
        ),
    };
  }

  retentionService(): RetentionService {
    return {
      purge: (now, auditRetentionDays) =>
        this.withConnection((connection) =>
          new PostgresRetentionRepository(connection).purge(
            now,
            auditRetentionDays,
          )
        ),
    };
  }

  rawArchiveService(): RawArchiveService {
    return {
      flush: (archiveRoot, limit) =>
        this.withConnection((connection) =>
          new PostgresRawArchiveRepository(connection).flush(
            archiveRoot,
            limit,
          )
        ),
    };
  }

  notificationService(): NotificationService {
    return {
      queueIncident: (incidentId, force) =>
        this.withConnection((connection) =>
          new PostgresNotificationRepository(connection).queueIncident(
            incidentId,
            force,
          )
        ),
      dispatchPending: (communityId, limit) =>
        this.withConnection((connection) =>
          new PostgresNotificationRepository(connection).dispatchPending(
            communityId,
            limit,
          )
        ),
    };
  }

  metricsRollupService(): MetricsRollupService {
    return {
      refresh: (now) =>
        this.withConnection((connection) =>
          new PostgresMetricsRollupRepository(connection).refresh(now)
        ),
    };
  }

  announcementDispatcher(
    sender: AnnouncementSender,
  ): AnnouncementDispatchService {
    return {
      dispatch: (now, limit, perCommunityLimit) =>
        this.withConnection((connection) =>
          new PostgresAnnouncementDispatcher(connection, sender).dispatch(
            now,
            limit,
            perCommunityLimit,
          )
        ),
    };
  }

  onboardingAutomation(
    roles: OnboardingRoleGateway,
  ): OnboardingAutomationService {
    return {
      dispatchNewcomerRoles: (limit) =>
        this.withConnection((connection) =>
          new PostgresOnboardingAutomation(connection, roles)
            .dispatchNewcomerRoles(limit)
        ),
      queueCheckpointReminders: (now, limit) =>
        this.withConnection((connection) =>
          new PostgresOnboardingAutomation(connection, roles)
            .queueCheckpointReminders(now, limit)
        ),
    };
  }

  checkpointReminderService(): CheckpointReminderService {
    return {
      queueCheckpointReminders: (now, limit) =>
        this.withConnection((connection) =>
          new PostgresOnboardingAutomation(connection)
            .queueCheckpointReminders(now, limit)
        ),
    };
  }

  analyticsRefreshService(): AnalyticsRefreshService {
    return {
      refresh: (now) =>
        this.withConnection((connection) =>
          new PostgresAnalyticsRepository(connection).refresh(now)
        ),
    };
  }

  observationCollector(): ObservationCollector {
    return {
      collect: async (observation) => {
        const reserved = await this.pool.reserve();
        try {
          return await new PostgresObservationRepository(
            new PostgresConnection(reserved),
          ).collect(observation);
        } finally {
          reserved.release();
        }
      },
    };
  }

  machineIngestionService(): MachineIngestionService {
    const request = async <T>(
      callback: (repository: PostgresMachineIngestionRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresMachineIngestionRepository(
            new PostgresConnection(reserved),
          ),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      authorizeApiClient: (plaintextKey, communityId) =>
        request((repository) =>
          repository.authorizeApiClient(plaintextKey, communityId)
        ),
      resolveTwitchInstallation: (broadcasterId) =>
        request((repository) =>
          repository.resolveTwitchInstallation(broadcasterId)
        ),
      recordSubscription: (communityId, subscription) =>
        request((repository) =>
          repository.recordSubscription(communityId, subscription)
        ),
      markSubscription: (subscriptionId, status) =>
        request((repository) =>
          repository.markSubscription(subscriptionId, status)
        ),
      upsertExternalSource: (input) =>
        request((repository) => repository.upsertExternalSource(input)),
    };
  }

  operatorAuthStore(): OperatorAuthStore {
    const request = async <T>(
      callback: (repository: OperatorAuthRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new OperatorAuthRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      completeLogin: (identity, role) =>
        request((repository) => repository.completeLogin(identity, role)),
      switchCommunity: (operatorId, communityId, previousCommunityId) =>
        request((repository) =>
          repository.switchCommunity(
            operatorId,
            communityId,
            previousCommunityId,
          )
        ),
      auditLogout: (operatorId) =>
        request((repository) => repository.auditLogout(operatorId)),
      resolveSession: (operatorId) =>
        request((repository) => repository.resolveSession(operatorId)),
    };
  }

  dashboardQueryService(): DashboardQueryService {
    const request = async <T>(
      callback: (repository: DashboardQueryRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new DashboardQueryRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      overview: (communityId) =>
        request((repository) => repository.overview(communityId)),
      users: (communityId, query) =>
        request((repository) => repository.users(communityId, query)),
      search: (communityId, query) =>
        request((repository) => repository.search(communityId, query)),
      signals: (communityId, query) =>
        request((repository) => repository.signals(communityId, query)),
      analytics: (communityId) =>
        request((repository) => repository.analytics(communityId)),
      saveQuery: (operatorId, name, query, filters) =>
        request((repository) =>
          repository.saveQuery(operatorId, name, query, filters)
        ),
      observationPivots: (communityId, observationId) =>
        request((repository) =>
          repository.observationPivots(communityId, observationId)
        ),
      userDetail: (communityId, userId) =>
        request((repository) => repository.userDetail(communityId, userId)),
      linkUser: (communityId, operatorId, userId, platform, platformUserId) =>
        request((repository) =>
          repository.linkUser(
            communityId,
            operatorId,
            userId,
            platform,
            platformUserId,
          )
        ),
      linkUsersByName: (
        communityId,
        operatorId,
        selectedUserId,
        platform,
        usernames,
      ) =>
        request((repository) =>
          repository.linkUsersByName(
            communityId,
            operatorId,
            selectedUserId,
            platform,
            usernames,
          )
        ),
      addUserNote: (communityId, operatorId, userId, body) =>
        request((repository) =>
          repository.addUserNote(
            communityId,
            operatorId,
            userId,
            body,
          )
        ),
      unlinkUser: (communityId, operatorId, userId, platformAccountId) =>
        request((repository) =>
          repository.unlinkUser(
            communityId,
            operatorId,
            userId,
            platformAccountId,
          )
        ),
      reviewIdentitySuggestion: (
        communityId,
        operatorId,
        suggestionId,
        decision,
      ) =>
        request((repository) =>
          repository.reviewIdentitySuggestion(
            communityId,
            operatorId,
            suggestionId,
            decision,
          )
        ),
      slo: (communityId) =>
        request((repository) => repository.slo(communityId)),
    };
  }

  intelligenceService(): IntelligenceService {
    const request = async <T>(
      callback: (repository: PostgresIntelligenceRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresIntelligenceRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId, query) =>
        request((repository) => repository.snapshot(communityId, query)),
      caseDetail: (communityId, caseId) =>
        request((repository) => repository.caseDetail(communityId, caseId)),
      caseAction: (communityId, operatorId, caseId, input) =>
        request((repository) =>
          repository.caseAction(communityId, operatorId, caseId, input)
        ),
      caseFromAlert: (communityId, operatorId, alertId) =>
        request((repository) =>
          repository.caseFromAlert(communityId, operatorId, alertId)
        ),
      disposeAlert: (communityId, operatorId, alertId, disposition) =>
        request((repository) =>
          repository.disposeAlert(
            communityId,
            operatorId,
            alertId,
            disposition,
          )
        ),
      updateAlert: (communityId, operatorId, alertId, input) =>
        request((repository) =>
          repository.updateAlert(communityId, operatorId, alertId, input)
        ),
      generateReport: (communityId, reportType, userId) =>
        request((repository) =>
          repository.generateReport(communityId, reportType, userId)
        ),
      report: (communityId, reportId) =>
        request((repository) => repository.report(communityId, reportId)),
    };
  }

  moderationService(): ModerationService {
    const request = async <T>(
      callback: (repository: PostgresModerationRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresModerationRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId, operatorId) =>
        request((repository) => repository.snapshot(communityId, operatorId)),
      resolveReview: (input) =>
        request((repository) => repository.resolveReview(input)),
      bulk: (input) => request((repository) => repository.bulk(input)),
      recordUserAction: (input) =>
        request((repository) => repository.recordUserAction(input)),
      assign: (communityId, operatorId, workType, itemId) =>
        request((repository) =>
          repository.assign(communityId, operatorId, workType, itemId)
        ),
      resolveMember: (
        communityId,
        operatorId,
        queueType,
        itemId,
        resolution,
        note,
      ) =>
        request((repository) =>
          repository.resolveMember(
            communityId,
            operatorId,
            queueType,
            itemId,
            resolution,
            note,
          )
        ),
      createRuleDraft: (communityId, operatorId, config) =>
        request((repository) =>
          repository.createRuleDraft(communityId, operatorId, config)
        ),
      saveRule: (communityId, operatorId, config, enabled, enforcementMode) =>
        request((repository) =>
          repository.saveRule(
            communityId,
            operatorId,
            config,
            enabled,
            enforcementMode,
          )
        ),
      previewRule: (communityId, versionId, samples) =>
        request((repository) =>
          repository.previewRule(communityId, versionId, samples)
        ),
      publishRule: (communityId, operatorId, versionId, lifecycleState) =>
        request((repository) =>
          repository.publishRule(
            communityId,
            operatorId,
            versionId,
            lifecycleState,
          )
        ),
      rollbackRule: (communityId, operatorId, versionId) =>
        request((repository) =>
          repository.rollbackRule(communityId, operatorId, versionId)
        ),
      addRuleExemption: (
        communityId,
        operatorId,
        ruleId,
        exemptionType,
        exemptionValue,
        reason,
      ) =>
        request((repository) =>
          repository.addRuleExemption(
            communityId,
            operatorId,
            ruleId,
            exemptionType,
            exemptionValue,
            reason,
          )
        ),
      saveFilter: (communityId, operatorId, name, filters) =>
        request((repository) =>
          repository.saveFilter(communityId, operatorId, name, filters)
        ),
      listWork: (communityId, operatorId, query) =>
        request((repository) =>
          repository.listWork(communityId, operatorId, query)
        ),
    };
  }

  commandRegistry(): CommandRegistry {
    const request = async <T>(
      callback: (registry: PostgresCommandRegistry) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresCommandRegistry(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      list: () => request((registry) => registry.list()),
      update: (input) => request((registry) => registry.update(input)),
    };
  }

  auditService(): AuditService {
    const request = async <T>(
      callback: (audit: PostgresAuditRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresAuditRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return { list: (query) => request((audit) => audit.list(query)) };
  }

  announcementService(): AnnouncementService {
    const request = async <T>(
      callback: (repository: PostgresAnnouncementRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresAnnouncementRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      list: (communityId) =>
        request((repository) => repository.list(communityId)),
      create: (communityId, operatorId, input) =>
        request((repository) =>
          repository.create(communityId, operatorId, input)
        ),
      transition: (
        communityId,
        operatorId,
        announcementId,
        action,
        scheduledAt,
      ) =>
        request((repository) =>
          repository.transition(
            communityId,
            operatorId,
            announcementId,
            action,
            scheduledAt,
          )
        ),
    };
  }

  dashboardOperations(serviceName: string): DashboardOperations {
    return {
      goLive: (communityId, operatorId) =>
        this.withConnection((connection) =>
          new PostgresDashboardOperations(connection, serviceName)
            .goLive(communityId, operatorId)
        ),
      restart: (operatorId) =>
        this.withConnection((connection) =>
          new PostgresDashboardOperations(connection, serviceName)
            .restart(operatorId)
        ),
      resetDatabase: (operatorId) =>
        this.withConnection((connection) =>
          new PostgresDashboardOperations(connection, serviceName)
            .resetDatabase(operatorId)
        ),
    };
  }

  shadowComparisonStore(): ShadowComparisonStore {
    return {
      record: (comparison) =>
        this.withConnection((connection) =>
          new PostgresShadowComparisonStore(connection).record(comparison)
        ),
    };
  }

  onboardingService(): OnboardingService {
    const request = async <T>(
      callback: (repository: PostgresOnboardingRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresOnboardingRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId) =>
        request((repository) => repository.snapshot(communityId)),
      configure: (communityId, operatorId, input) =>
        request((repository) =>
          repository.configure(communityId, operatorId, input)
        ),
      saveResource: (communityId, operatorId, input) =>
        request((repository) =>
          repository.saveResource(communityId, operatorId, input)
        ),
      deleteResource: (communityId, operatorId, resourceId) =>
        request((repository) =>
          repository.deleteResource(communityId, operatorId, resourceId)
        ),
      verify: (communityId, operatorId, platformUserId, evidence) =>
        request((repository) =>
          repository.verify(
            communityId,
            operatorId,
            platformUserId,
            evidence,
          )
        ),
    };
  }

  settingsService(): SettingsService {
    const request = async <T>(
      callback: (repository: PostgresSettingsRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresSettingsRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId) =>
        request((repository) => repository.snapshot(communityId)),
      update: (communityId, operatorId, input) =>
        request((repository) =>
          repository.update(communityId, operatorId, input)
        ),
      invite: (communityId, operatorId, discordUserId, role, expiresHours) =>
        request((repository) =>
          repository.invite(
            communityId,
            operatorId,
            discordUserId,
            role,
            expiresHours,
          )
        ),
      access: (communityId, operatorId, entityId, action, reason) =>
        request((repository) =>
          repository.access(
            communityId,
            operatorId,
            entityId,
            action,
            reason,
          )
        ),
    };
  }

  integrationService(): IntegrationService {
    const request = async <T>(
      callback: (repository: PostgresIntegrationRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresIntegrationRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId, operatorId) =>
        request((repository) => repository.snapshot(communityId, operatorId)),
      createDiscordIntent: (input) =>
        request((repository) => repository.createDiscordIntent(input)),
      completeDiscordIntent: (state) =>
        request((repository) => repository.completeDiscordIntent(state)),
      createTwitchIntent: (input) =>
        request((repository) => repository.createTwitchIntent(input)),
      completeTwitchIntent: (state, grant, encryptionKey, configured) =>
        request((repository) =>
          repository.completeTwitchIntent(
            state,
            grant,
            encryptionKey,
            configured,
          )
        ),
      revoke: (communityId, operatorId, installationId) =>
        request((repository) =>
          repository.revoke(communityId, operatorId, installationId)
        ),
    };
  }

  liveOpsService(): LiveOpsService {
    const request = async <T>(
      callback: (repository: PostgresLiveOpsRepository) => Promise<T>,
    ): Promise<T> => {
      const reserved = await this.pool.reserve();
      try {
        return await callback(
          new PostgresLiveOpsRepository(new PostgresConnection(reserved)),
        );
      } finally {
        reserved.release();
      }
    };
    return {
      snapshot: (communityId) =>
        request((repository) => repository.snapshot(communityId)),
      context: (communityId, observationId) =>
        request((repository) => repository.context(communityId, observationId)),
      moderate: (communityId, operatorId, input) =>
        request((repository) =>
          repository.moderate(communityId, operatorId, input)
        ),
      incident: (communityId, operatorId, incidentId, action, input) =>
        request((repository) =>
          repository.incident(
            communityId,
            operatorId,
            incidentId,
            action,
            input,
          )
        ),
      handoff: (communityId, operatorId, incomingOperatorId, note) =>
        request((repository) =>
          repository.handoff(
            communityId,
            operatorId,
            incomingOperatorId,
            note,
          )
        ),
      shifts: (communityId) =>
        request((repository) => repository.shifts(communityId)),
      schedule: (communityId, operatorId, input) =>
        request((repository) =>
          repository.schedule(communityId, operatorId, input)
        ),
      playbook: (communityId, operatorId, key, incidentId) =>
        request((repository) =>
          repository.playbook(communityId, operatorId, key, incidentId)
        ),
      completePlaybook: (runId, completed) =>
        request((repository) => repository.completePlaybook(runId, completed)),
      createDestination: (communityId, input) =>
        request((repository) =>
          repository.createDestination(communityId, input)
        ),
    };
  }

  twitchControlGateway(
    tokens: TwitchTokenManager,
  ): PostgresTwitchControlGateway {
    return new PostgresTwitchControlGateway(
      {
        query: (sql, parameters) =>
          this.withConnection((connection) =>
            connection.query(sql, parameters)
          ),
        transaction: (callback) =>
          this.withConnection((connection) => connection.transaction(callback)),
      },
      tokens,
    );
  }

  async reconcileTwitchEventSub(input: {
    communityId: number;
    installationId: number;
    grant: TwitchGrant;
    callbackUrl: string;
    secret: string;
    clientId: string;
    clientSecret: string;
    onTokenRefresh?: (
      accessToken: string,
      refreshToken: string | null,
    ) => Promise<void> | void;
  }): Promise<void> {
    const holder = `web-eventsub-${Deno.pid}-${crypto.randomUUID()}`;
    const leases = this.providerOwnershipLease();
    if (!await leases.acquire(input.installationId, holder, 60)) {
      throw new TypeError("Twitch installation ownership lease is unavailable");
    }
    try {
      await this.withConnection((connection) =>
        new PostgresTwitchEventSubReconciler(
          connection,
          new TwitchTokenManager({
            initialAccessToken: input.grant.accessToken,
            refreshToken: input.grant.refreshToken,
            clientId: input.clientId,
            clientSecret: input.clientSecret,
            onTokenRefresh: input.onTokenRefresh,
          }),
          input.callbackUrl,
          input.secret,
        ).reconcile(input.communityId, input.installationId, [
          {
            type: "stream.online",
            condition: { broadcaster_user_id: input.grant.broadcasterId },
          },
          {
            type: "stream.offline",
            condition: { broadcaster_user_id: input.grant.broadcasterId },
          },
        ])
      );
    } finally {
      await leases.release(input.installationId, holder);
    }
  }

  private async withConnection<T>(
    callback: (connection: DatabaseConnection) => Promise<T>,
  ): Promise<T> {
    const reserved = await this.pool.reserve();
    try {
      return await callback(new PostgresConnection(reserved));
    } finally {
      reserved.release();
    }
  }

  async close(): Promise<void> {
    await this.pool.end();
  }
}

class TestRollback<T> extends Error {
  constructor(readonly result: T) {
    super("test transaction rollback");
  }
}

export async function withTestTransaction<T>(
  connection: DatabaseConnection,
  tenant: TenantContext,
  callback: (repository: ScopedRepository) => Promise<T>,
): Promise<T> {
  try {
    await connection.transaction(async (transaction) => {
      const result = await callback(new ScopedRepository(transaction, tenant));
      throw new TestRollback(result);
    });
  } catch (error) {
    if (error instanceof TestRollback) return error.result as T;
    throw error;
  }
  throw new Error("test transaction committed unexpectedly");
}
