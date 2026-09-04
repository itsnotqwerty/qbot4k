import {
  CONTENT_ANALYZER_VERSION,
  understandContent,
} from "../domain/content_analysis.ts";
import type { DatabaseConnection } from "../data/database.ts";
import { PermanentJobError, type ProcessingJob } from "./jobs.ts";
import { normalizedMessageFromObservation } from "../core/models.ts";
import {
  evaluateEgregiousContent,
  evaluateMessageModeration,
  type ModerationFinding,
  type ModerationRule,
} from "../domain/moderation_rules.ts";
import { consumeTenantQuota } from "../domain/quota.ts";

export const MESSAGE_ANALYSIS_JOB_TYPE = "analyze.message.created";

export interface MessageAnalysisShadowRunner {
  runNext(): Promise<boolean>;
}

class ShadowRollback extends Error {}

export class PostgresMessageAnalysisShadowRunner
  implements MessageAnalysisShadowRunner {
  constructor(private readonly connection: DatabaseConnection) {}

  async runNext(): Promise<boolean> {
    const selectedJobs: ProcessingJob[] = [];
    let failure: unknown;
    try {
      await this.connection.transaction(async (connection) => {
        const row = (await connection.query(
          `SELECT job.* FROM processing_jobs AS job
           JOIN processing_job_ownership AS ownership
             ON ownership.job_type=job.job_type
          WHERE job.job_type=$1 AND ownership.shadow_runtime='deno'
            AND ownership.owner_runtime<>'deno'
            AND job.status IN ('pending','retry')
            AND job.available_at::timestamptz<=CURRENT_TIMESTAMP
            AND NOT EXISTS (
              SELECT 1 FROM processing_job_shadow_runs AS shadow
               WHERE shadow.processing_job_id=job.id AND shadow.runtime='deno'
            )
          ORDER BY job.priority,job.available_at,job.id
          FOR UPDATE OF job SKIP LOCKED LIMIT 1`,
          [MESSAGE_ANALYSIS_JOB_TYPE],
        ))[0];
        if (!row) return;
        const job = decodeShadowJob(row);
        selectedJobs.push(job);
        await new PostgresMessageAnalysisRepository(connection, true).handle(
          job,
        );
        throw new ShadowRollback("shadow analysis completed");
      });
    } catch (error) {
      if (!(error instanceof ShadowRollback)) failure = error;
    }
    const job = selectedJobs[0];
    if (!job) return false;
    await this.connection.query(
      `INSERT INTO processing_job_shadow_runs(
         processing_job_id,runtime,status,result_json
       ) VALUES ($1,'deno',$2,$3)
       ON CONFLICT(processing_job_id,runtime) DO NOTHING`,
      [
        job.id,
        failure ? "failed" : "matched",
        JSON.stringify({
          job_type: job.jobType,
          rolled_back: true,
          ...(failure ? { error: errorMessage(failure) } : {}),
        }),
      ],
    );
    return true;
  }
}

export class PostgresMessageAnalysisRepository {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly forceShadow = false,
  ) {}

  async handle(job: ProcessingJob): Promise<void> {
    if (job.stage !== "analysis" || job.jobType !== MESSAGE_ANALYSIS_JOB_TYPE) {
      throw new PermanentJobError(`unsupported analysis job: ${job.jobType}`);
    }
    if (job.observationId === null) {
      throw new PermanentJobError("Message analysis job has no observation_id");
    }
    const observationId = job.observationId;
    await this.connection.transaction(async (connection) => {
      const observation = (await connection.query(
        `SELECT o.*,actor.platform_user_id AS actor_platform_user_id,
                actor.username AS actor_username,actor.user_id AS actor_user_id
           FROM observations AS o
           LEFT JOIN platform_accounts AS actor
             ON actor.id=o.actor_platform_account_id
          WHERE o.id=$1 AND o.community_id=$2 FOR UPDATE OF o`,
        [observationId, job.communityId],
      ))[0];
      if (!observation) {
        throw new PermanentJobError(
          `Observation ${observationId} does not exist`,
        );
      }
      if (String(observation.event_type) !== "message.created") {
        throw new PermanentJobError(
          `Expected message.created, received ${
            String(observation.event_type)
          }`,
        );
      }
      const policy = (await connection.query(
        "SELECT moderation_shadow_mode FROM community_policy_settings WHERE community_id=$1",
        [job.communityId],
      ))[0];
      if (!policy) {
        throw new PermanentJobError(
          `Community ${job.communityId} has no policy settings`,
        );
      }
      let message;
      try {
        message = normalizedMessageFromObservation(observation);
      } catch (error) {
        throw new PermanentJobError(
          `Malformed observation ${observationId}: ${errorMessage(error)}`,
        );
      }
      const existing = (await connection.query(
        "SELECT id FROM messages WHERE observation_id=$1",
        [observationId],
      ))[0];
      if (existing) return;

      const platformAccountId = integer(
        observation.actor_platform_account_id,
        "actor_platform_account_id",
      );
      const userId = observation.actor_user_id === null ||
          observation.actor_user_id === undefined
        ? await createCanonicalUser(
          connection,
          platformAccountId,
          message.username,
        )
        : integer(observation.actor_user_id, "actor_user_id");
      const inserted = (await connection.query(
        `INSERT INTO messages(
           observation_id,installation_id,platform,platform_message_id,
           platform_account_id,user_id,community_id,channel_id,content_raw,
           content_normalized,sent_at
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
         ON CONFLICT(platform,platform_message_id) DO UPDATE SET
           observation_id=COALESCE(messages.observation_id,EXCLUDED.observation_id)
         RETURNING id`,
        [
          observationId,
          observation.installation_id === null
            ? null
            : integer(observation.installation_id, "installation_id"),
          message.platform,
          message.platformMessageId,
          platformAccountId,
          userId,
          job.communityId,
          message.channelId,
          message.contentRaw,
          message.contentNormalized,
          message.sentAt,
        ],
      ))[0];
      if (!inserted) throw new Error("message projection was not created");
      const messageId = integer(inserted.id, "message_id");
      const rules = await loadRules(
        connection,
        job.communityId,
        message.platform,
        message.channelId,
        platformAccountId,
      );
      const findings = [
        ...evaluateMessageModeration(message, rules),
        ...rules.filter((rule) => rule.name === "builtin:egregious_content")
          .flatMap((rule) => evaluateEgregiousContent(message, rule)),
      ];
      const shadow = this.forceShadow || truthy(policy.moderation_shadow_mode);
      for (const finding of uniqueFindings(findings)) {
        await recordFinding(connection, {
          finding,
          job,
          messageId,
          platformAccountId,
          userId,
          installationId: observation.installation_id === null
            ? null
            : integer(observation.installation_id, "installation_id"),
          platform: message.platform,
          shadow,
        });
      }
      await persistContent(
        connection,
        job.communityId,
        observationId,
        userId,
        message.contentRaw,
        parseObject(observation.attributes_json),
      );
    });
  }
}

async function createCanonicalUser(
  connection: DatabaseConnection,
  platformAccountId: number,
  displayName: string,
): Promise<number> {
  const inserted = (await connection.query(
    "INSERT INTO users(primary_display_name) VALUES ($1) RETURNING id",
    [displayName],
  ))[0];
  if (!inserted) throw new Error("canonical user was not created");
  const userId = integer(inserted.id, "user_id");
  await connection.query(
    "UPDATE platform_accounts SET user_id=$1,updated_at=CURRENT_TIMESTAMP WHERE id=$2 AND user_id IS NULL",
    [userId, platformAccountId],
  );
  return userId;
}

async function loadRules(
  connection: DatabaseConnection,
  communityId: number,
  platform: string,
  channelId: string,
  platformAccountId: number,
): Promise<readonly ModerationRule[]> {
  const rows = await connection.query(
    `SELECT id,name,rule_type,pattern,severity,auto_enforce_action,enabled,
            enforcement_mode,action_duration_seconds,platform_scope_json
       FROM moderation_rules AS rule
      WHERE enabled=1 AND community_id=$1
        AND NOT EXISTS (
          SELECT 1 FROM moderation_rule_exemptions AS exemption
           WHERE exemption.community_id=$1
             AND exemption.moderation_rule_id=rule.id
             AND ((exemption.exemption_type='channel' AND exemption.exemption_value=$2)
               OR (exemption.exemption_type='platform_account' AND exemption.exemption_value=$3)))
      ORDER BY id`,
    [communityId, channelId, String(platformAccountId)],
  );
  return rows.filter((row) =>
    stringArray(row.platform_scope_json).includes(platform)
  )
    .map((row) =>
      Object.freeze({
        id: integer(row.id, "rule_id"),
        name: String(row.name),
        ruleType: String(row.rule_type),
        pattern: String(row.pattern),
        severity: String(row.severity),
        autoEnforceAction: row.auto_enforce_action === null
          ? null
          : String(row.auto_enforce_action),
        enabled: truthy(row.enabled),
        enforcementMode: String(row.enforcement_mode),
        actionDurationSeconds: integer(
          row.action_duration_seconds,
          "action_duration_seconds",
        ),
      })
    );
}

interface FindingContext {
  readonly finding: ModerationFinding;
  readonly job: ProcessingJob;
  readonly messageId: number;
  readonly platformAccountId: number;
  readonly userId: number;
  readonly installationId: number | null;
  readonly platform: string;
  readonly shadow: boolean;
}

async function recordFinding(
  connection: DatabaseConnection,
  context: FindingContext,
): Promise<void> {
  const finding = context.finding;
  await connection.query(
    `INSERT INTO rule_matches(
       message_id,moderation_rule_id,severity,reason_code,confidence,recommended_action
     ) VALUES ($1,$2,$3,$4,1.0,$5)`,
    [
      context.messageId,
      finding.ruleId,
      finding.severity,
      finding.reasonCode,
      finding.autoEnforceAction,
    ],
  );
  if (["high", "critical"].includes(finding.severity.toLocaleLowerCase())) {
    await connection.query(
      `INSERT INTO intelligence_alerts(
         community_id,user_id,observation_id,alert_type,severity,title,summary,
         confidence,dedupe_key
       ) VALUES ($1,$2,$3,'moderation_finding',$4,$5,$6,1.0,$7)
       ON CONFLICT(dedupe_key) DO NOTHING`,
      [
        context.job.communityId,
        context.userId,
        context.job.observationId,
        finding.severity,
        finding.ruleName.replace(/^builtin:/u, "").replaceAll("_", " "),
        `${finding.reasonCode} matched message ${context.messageId} on ${context.platform}`,
        `moderation:${context.messageId}:${finding.ruleId}`,
      ],
    );
  }
  const enforce = !context.shadow && finding.enforcementMode === "enforce" &&
    finding.autoEnforceAction !== null;
  if (!enforce) {
    await connection.query(
      `INSERT INTO review_queue(message_id,status,severity,queue_reason_code)
       VALUES ($1,'open',$2,$3)`,
      [context.messageId, finding.severity, finding.reasonCode],
    );
    return;
  }
  await consumeTenantQuota(connection, context.job.communityId, "moderation");
  const action = (await connection.query(
    `INSERT INTO moderation_actions(
       community_id,installation_id,platform,message_id,target_platform_account_id,
       user_id,action_type,actor_type,reason,duration_seconds,status
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,'system',$8,$9,'pending') RETURNING id`,
    [
      context.job.communityId,
      context.installationId,
      context.platform,
      context.messageId,
      context.platformAccountId,
      context.userId,
      finding.autoEnforceAction,
      finding.reasonCode,
      finding.actionDurationSeconds,
    ],
  ))[0];
  if (!action) throw new Error("moderation action was not created");
  await connection.query(
    `INSERT INTO processing_jobs(
       community_id,stage,job_type,observation_id,payload_json,priority,idempotency_key
     ) VALUES ($1,'action',$2,$3,$4,10,$5)
     ON CONFLICT(idempotency_key) DO NOTHING`,
    [
      context.job.communityId,
      `${context.platform}.moderation.execute`,
      context.job.observationId,
      JSON.stringify({ message_id: context.messageId }),
      `message:${context.messageId}:moderation:v1`,
    ],
  );
}

async function persistContent(
  connection: DatabaseConnection,
  communityId: number,
  observationId: number,
  userId: number,
  text: string,
  attributes: Readonly<Record<string, unknown>>,
): Promise<void> {
  const result = understandContent(text, attributes);
  await connection.query(
    `INSERT INTO content_analysis(
       observation_id,analyzer_version,language_code,language_confidence,
       sentiment_label,sentiment_score,intent_label,intent_confidence,
       threat_level,threat_score,conversation_json,indicators_json,analyzed_at
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,CURRENT_TIMESTAMP)
     ON CONFLICT(observation_id) DO UPDATE SET
       analyzer_version=EXCLUDED.analyzer_version,
       language_code=EXCLUDED.language_code,
       language_confidence=EXCLUDED.language_confidence,
       sentiment_label=EXCLUDED.sentiment_label,
       sentiment_score=EXCLUDED.sentiment_score,
       intent_label=EXCLUDED.intent_label,
       intent_confidence=EXCLUDED.intent_confidence,
       threat_level=EXCLUDED.threat_level,
       threat_score=EXCLUDED.threat_score,
       conversation_json=EXCLUDED.conversation_json,
       indicators_json=EXCLUDED.indicators_json,
       analyzed_at=CURRENT_TIMESTAMP`,
    [
      observationId,
      CONTENT_ANALYZER_VERSION,
      result.languageCode,
      result.languageConfidence,
      result.sentimentLabel,
      result.sentimentScore,
      result.intentLabel,
      result.intentConfidence,
      result.threatLevel,
      result.threatScore,
      JSON.stringify(result.conversation),
      JSON.stringify(result.indicators),
    ],
  );
  await connection.query(
    "DELETE FROM content_entities WHERE observation_id=$1",
    [
      observationId,
    ],
  );
  for (const entity of result.entities) {
    await connection.query(
      `INSERT INTO content_entities(
         observation_id,entity_type,entity_value,normalized_value,confidence,
         start_offset,end_offset
       ) VALUES ($1,$2,$3,$4,$5,$6,$7)`,
      [observationId, ...entity],
    );
  }
  if (!["high", "critical"].includes(result.threatLevel)) return;
  await connection.query(
    `INSERT INTO intelligence_alerts(
       community_id,user_id,observation_id,alert_type,severity,title,summary,
       confidence,dedupe_key
     ) VALUES ($1,$2,$3,'content_threat',$4,'Potential Threat',$5,$6,$7)
     ON CONFLICT(dedupe_key) DO NOTHING`,
    [
      communityId,
      userId,
      observationId,
      result.threatLevel,
      `Content analysis identified: ${result.indicators.join(", ")}`,
      result.threatScore,
      `content-threat:${observationId}:v${CONTENT_ANALYZER_VERSION}`,
    ],
  );
}

function uniqueFindings(
  findings: readonly ModerationFinding[],
): readonly ModerationFinding[] {
  return [
    ...new Map(findings.map((finding) => [finding.ruleId, finding])).values(),
  ];
}

function parseObject(value: unknown): Readonly<Record<string, unknown>> {
  const parsed = typeof value === "string" ? JSON.parse(value || "{}") : value;
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : {};
}

function stringArray(value: unknown): readonly string[] {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function truthy(value: unknown): boolean {
  return value === true || value === 1 || value === "1";
}

function integer(value: unknown, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new PermanentJobError(`${name} is invalid`);
  }
  return parsed;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function decodeShadowJob(
  row: Readonly<Record<string, unknown>>,
): ProcessingJob {
  const stage = String(row.stage);
  if (stage !== "analysis" && stage !== "action") {
    throw new PermanentJobError("shadow job stage is invalid");
  }
  const payload = parseObject(row.payload_json);
  return Object.freeze({
    id: integer(row.id, "job_id"),
    communityId: integer(row.community_id, "community_id"),
    stage,
    jobType: String(row.job_type),
    observationId: row.observation_id === null ? null : integer(
      row.observation_id,
      "observation_id",
    ),
    payload,
    attempts: integer(row.attempts, "attempts"),
    maxAttempts: integer(row.max_attempts, "max_attempts"),
  });
}
