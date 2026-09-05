import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";

export type ModerationItem = Readonly<Record<string, unknown>>;

export interface ModerationSnapshot {
  readonly reviews: readonly ModerationItem[];
  readonly actions: readonly ModerationItem[];
  readonly rules: readonly ModerationItem[];
  readonly reports: readonly ModerationItem[];
  readonly appeals: readonly ModerationItem[];
  readonly ruleVersions: readonly ModerationItem[];
  readonly savedFilters: readonly ModerationItem[];
}

export interface ReviewResolution {
  readonly communityId: number;
  readonly operatorId: number;
  readonly reviewId: number;
  readonly resolution: string;
  readonly note: string;
  readonly actionType?: string | null;
  readonly durationSeconds?: number;
}

export interface BulkModerationInput {
  readonly communityId: number;
  readonly operatorId: number;
  readonly targetPlatformAccountIds: readonly number[];
  readonly actionType: string;
  readonly reason: string;
  readonly durationSeconds?: number;
  readonly dryRun: boolean;
}

export interface UserModerationInput {
  readonly communityId: number;
  readonly operatorId: number;
  readonly userId: number;
  readonly targetPlatformAccountId: number;
  readonly actionType: string;
  readonly reason: string;
}

export interface BulkModerationResult {
  readonly dry_run: boolean;
  readonly action_type: string;
  readonly requested: number;
  readonly results: readonly ModerationItem[];
}

export interface ModerationRuleConfig {
  readonly name: string;
  readonly rule_type: string;
  readonly pattern: string;
  readonly severity: string;
  readonly auto_enforce_action?: string | null;
  readonly action_duration_seconds?: number;
  readonly platform_scope: readonly string[];
}

export interface ModerationWorkQuery {
  readonly queue?: string;
  readonly search?: string;
  readonly severity?: string;
  readonly rule?: string;
  readonly platform?: string;
  readonly startAt?: string;
  readonly endAt?: string;
  readonly assignment?: string;
  readonly page?: number;
}

export interface ModerationWorkResult {
  readonly items: readonly ModerationItem[];
  readonly total: number;
  readonly page: number;
}

export interface ModerationService {
  snapshot(
    communityId: number,
    operatorId?: number,
  ): Promise<ModerationSnapshot>;
  resolveReview(input: ReviewResolution): Promise<number | null>;
  bulk(input: BulkModerationInput): Promise<BulkModerationResult>;
  recordUserAction(input: UserModerationInput): Promise<boolean>;
  assign(
    communityId: number,
    operatorId: number,
    workType: string,
    itemId: number,
  ): Promise<void>;
  resolveMember(
    communityId: number,
    operatorId: number,
    queueType: string,
    itemId: number,
    resolution: string,
    note: string,
  ): Promise<void>;
  createRuleDraft(
    communityId: number,
    operatorId: number,
    config: ModerationRuleConfig,
  ): Promise<number>;
  saveRule(
    communityId: number,
    operatorId: number,
    config: ModerationRuleConfig,
    enabled: boolean,
    enforcementMode: string,
  ): Promise<number>;
  previewRule(
    communityId: number,
    versionId: number,
    samples: readonly string[],
  ): Promise<ModerationItem>;
  publishRule(
    communityId: number,
    operatorId: number,
    versionId: number,
    lifecycleState: string,
  ): Promise<number>;
  rollbackRule(
    communityId: number,
    operatorId: number,
    versionId: number,
  ): Promise<number>;
  addRuleExemption(
    communityId: number,
    operatorId: number,
    ruleId: number,
    exemptionType: string,
    exemptionValue: string,
    reason: string,
  ): Promise<number>;
  saveFilter(
    communityId: number,
    operatorId: number,
    name: string,
    filters: ModerationItem,
  ): Promise<number>;
  listWork(
    communityId: number,
    operatorId: number,
    query: ModerationWorkQuery,
  ): Promise<ModerationWorkResult>;
}

const rows = (values: readonly DatabaseRow[]): readonly ModerationItem[] =>
  Object.freeze(values.map((value) => Object.freeze({ ...value })));

export class PostgresModerationRepository implements ModerationService {
  constructor(private readonly connection: DatabaseConnection) {}

  async snapshot(
    communityId: number,
    operatorId = 0,
  ): Promise<ModerationSnapshot> {
    const reviews = await this.connection.query(
      `SELECT q.id AS review_id, q.message_id, m.platform,
              m.platform_account_id AS target_platform_account_id,
              p.username AS target_username, q.severity, q.queue_reason_code AS reason_code,
              q.status, m.content_raw AS content, q.created_at, q.assigned_operator_id
         FROM review_queue AS q JOIN messages AS m ON m.id = q.message_id
         JOIN platform_accounts AS p ON p.id = m.platform_account_id
        WHERE q.status = 'open' AND m.community_id = $1
        ORDER BY q.created_at DESC, q.id DESC LIMIT 25`,
      [communityId],
    );
    const actions = await this.connection.query(
      `SELECT a.id AS action_id, a.platform, p.username AS target_username,
              a.action_type, a.status, a.provider_status, a.provider_confirmed_at,
              a.reason, a.error_message, a.created_at
         FROM moderation_actions AS a JOIN platform_accounts AS p ON p.id = a.target_platform_account_id
        WHERE a.community_id = $1 ORDER BY a.created_at DESC, a.id DESC LIMIT 25`,
      [communityId],
    );
    const rules = await this.connection.query(
      `SELECT id AS rule_id, name, rule_type, pattern, severity, auto_enforce_action,
              enabled, enforcement_mode, action_duration_seconds
         FROM moderation_rules WHERE community_id = $1 ORDER BY LOWER(name)`,
      [communityId],
    );
    const reports = await this.memberQueue("reports", communityId);
    const appeals = await this.memberQueue("appeals", communityId);
    const ruleVersions = await this.connection.query(
      `SELECT v.id AS version_id,r.name,v.version_number,v.lifecycle_state,v.impact_json,v.created_by_operator_id,v.approved_by_operator_id
         FROM moderation_rule_versions AS v JOIN moderation_rules AS r ON r.id=v.moderation_rule_id
        WHERE v.community_id=$1 ORDER BY v.id DESC`,
      [communityId],
    );
    const savedFilters = operatorId > 0
      ? await this.connection.query(
        "SELECT id,name,filters_json FROM moderation_saved_filters WHERE community_id=$1 AND operator_id=$2 ORDER BY name",
        [communityId, operatorId],
      )
      : [];
    return Object.freeze({
      reviews: rows(reviews),
      actions: rows(actions),
      rules: rows(rules),
      reports,
      appeals,
      ruleVersions: rows(ruleVersions),
      savedFilters: rows(
        savedFilters.map((item) => ({
          ...item,
          filters: JSON.parse(String(item.filters_json)),
        })),
      ),
    });
  }

  async resolveReview(input: ReviewResolution): Promise<number | null> {
    const resolution = input.resolution.trim().toLocaleLowerCase();
    const actionType = input.actionType?.trim().toLocaleLowerCase() || null;
    if (!new Set(["dismissed", "confirmed", "escalated"]).has(resolution)) {
      throw new TypeError("invalid review resolution");
    }
    if (
      actionType !== null &&
      !new Set(["warn", "timeout", "ban"]).has(actionType)
    ) {
      throw new TypeError("invalid review action");
    }
    return await this.connection.transaction(async (connection) => {
      const found = await connection.query(
        `SELECT q.status, q.message_id, m.platform, m.observation_id, m.platform_account_id
           FROM review_queue AS q JOIN messages AS m ON m.id = q.message_id
          WHERE q.id = $1 AND m.community_id = $2 FOR UPDATE`,
        [input.reviewId, input.communityId],
      );
      const review = found[0];
      if (!review) throw new TypeError("review not found");
      if (String(review.status) !== "open") {
        throw new TypeError("review is already resolved");
      }
      let actionId: number | null = null;
      if (resolution === "confirmed" && actionType !== null) {
        const created = await connection.query(
          `INSERT INTO moderation_actions(
             community_id, platform, message_id, target_platform_account_id,
             action_type, actor_type, actor_id, reason, duration_seconds, status)
           VALUES ($1, $2, $3, $4, $5, 'operator', $6, $7, $8, 'pending')
           RETURNING id`,
          [
            input.communityId,
            String(review.platform),
            Number(review.message_id),
            Number(review.platform_account_id),
            actionType,
            input.operatorId,
            input.note.trim() || "Confirmed analyst review",
            Math.max(1, Math.min(input.durationSeconds ?? 600, 2_419_200)),
          ],
        );
        actionId = Number(created[0]?.id);
        if (!Number.isInteger(actionId)) {
          throw new TypeError("moderation action was not created");
        }
        await connection.query(
          `INSERT INTO processing_jobs(
             community_id, stage, job_type, observation_id, payload_json,
             priority, idempotency_key)
           VALUES ($1, 'action', $2, $3, $4, 10, $5)
           ON CONFLICT(idempotency_key) DO NOTHING`,
          [
            input.communityId,
            `${String(review.platform)}.moderation.execute`,
            review.observation_id === null
              ? null
              : Number(review.observation_id),
            JSON.stringify({ message_id: Number(review.message_id) }),
            `review:${input.reviewId}:moderation:${actionType}`,
          ],
        );
      }
      await connection.query(
        `UPDATE review_queue SET status = 'resolved', resolution = $1,
           resolution_note = $2, resolved_by_operator_id = $3,
           assigned_operator_id = COALESCE(assigned_operator_id, $3),
           resolved_at = CURRENT_TIMESTAMP WHERE id = $4`,
        [resolution, input.note.trim(), input.operatorId, input.reviewId],
      );
      await connection.query(
        `INSERT INTO audit_log(actor_type, actor_id, action_type, entity_type, entity_id, payload_json)
         VALUES ('operator', $1, 'moderation.review_resolved', 'review_queue', $2, $3)`,
        [
          input.operatorId,
          input.reviewId,
          JSON.stringify({
            community_id: input.communityId,
            resolution,
            action: actionType,
            action_id: actionId,
            note: input.note.trim(),
          }),
        ],
      );
      return actionId;
    });
  }

  async bulk(input: BulkModerationInput): Promise<BulkModerationResult> {
    const actionType = input.actionType.trim().toLocaleLowerCase();
    const reason = input.reason.trim();
    const targetIds = [...new Set(input.targetPlatformAccountIds.map(Number))];
    if (
      !targetIds.length || targetIds.length > 25 ||
      targetIds.some((id) => !Number.isInteger(id))
    ) {
      throw new TypeError("bulk moderation requires 1 to 25 explicit targets");
    }
    if (!new Set(["warn", "timeout", "ban"]).has(actionType)) {
      throw new TypeError("invalid bulk moderation action");
    }
    if (!reason) throw new TypeError("bulk moderation reason is required");
    const execute = async (
      connection: DatabaseConnection,
    ): Promise<BulkModerationResult> => {
      const results: ModerationItem[] = [];
      for (const targetId of targetIds) {
        const rows = await connection.query(
          `SELECT p.platform, p.username, m.id AS message_id, m.observation_id
             FROM platform_accounts AS p JOIN messages AS m
               ON m.platform_account_id = p.id AND m.community_id = $1
            WHERE p.id = $2 ORDER BY m.created_at DESC, m.id DESC LIMIT 1`,
          [input.communityId, targetId],
        );
        const target = rows[0];
        if (!target) {
          results.push({
            target_platform_account_id: targetId,
            status: "not_found",
          });
          continue;
        }
        const result: Record<string, unknown> = {
          target_platform_account_id: targetId,
          platform: target.platform,
          username: target.username,
          status: input.dryRun ? "eligible" : "queued",
        };
        if (!input.dryRun) {
          const action = await connection.query(
            `INSERT INTO moderation_actions(community_id, platform, message_id,
               target_platform_account_id, action_type, actor_type, actor_id,
               reason, duration_seconds, status, assigned_operator_id)
             VALUES ($1,$2,$3,$4,$5,'operator',$6,$7,$8,'pending',$6) RETURNING id`,
            [
              input.communityId,
              String(target.platform),
              Number(target.message_id),
              targetId,
              actionType,
              input.operatorId,
              reason,
              Math.max(1, Math.min(input.durationSeconds ?? 600, 2_419_200)),
            ],
          );
          result.action_id = Number(action[0]?.id);
          await connection.query(
            `INSERT INTO processing_jobs(community_id,stage,job_type,observation_id,payload_json,priority,idempotency_key)
             VALUES ($1,'action',$2,$3,$4,10,$5) ON CONFLICT(idempotency_key) DO NOTHING`,
            [
              input.communityId,
              `${target.platform}.moderation.execute`,
              target.observation_id === null
                ? null
                : Number(target.observation_id),
              JSON.stringify({ message_id: Number(target.message_id) }),
              `bulk:${input.communityId}:${input.operatorId}:${actionType}:${targetId}:${target.message_id}`,
            ],
          );
        }
        results.push(Object.freeze(result));
      }
      if (!input.dryRun) {
        await connection.query(
          `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
         VALUES ('operator',$1,'moderation.bulk_queued','community',$2,$3)`,
          [
            input.operatorId,
            input.communityId,
            JSON.stringify({
              action_type: actionType,
              reason,
              targets: targetIds,
              results,
            }),
          ],
        );
      }
      return Object.freeze({
        dry_run: input.dryRun,
        action_type: actionType,
        requested: targetIds.length,
        results: Object.freeze(results),
      });
    };
    return input.dryRun
      ? await execute(this.connection)
      : await this.connection.transaction(execute);
  }

  async recordUserAction(input: UserModerationInput): Promise<boolean> {
    const actionType = input.actionType.trim().toLocaleLowerCase();
    const reason = input.reason.trim();
    if (!new Set(["warn", "timeout", "ban", "review"]).has(actionType)) {
      throw new TypeError("Invalid action type");
    }
    if (!reason) throw new TypeError("Reason is required");
    const target = (await this.connection.query(
      `SELECT p.platform FROM platform_accounts AS p
        WHERE p.id=$1 AND p.user_id=$2 AND EXISTS (
          SELECT 1 FROM messages AS m
           WHERE m.platform_account_id=p.id AND m.community_id=$3)`,
      [input.targetPlatformAccountId, input.userId, input.communityId],
    ))[0];
    if (!target) return false;
    await this.connection.query(
      `INSERT INTO moderation_actions(
         community_id,platform,message_id,target_platform_account_id,action_type,
         actor_type,actor_id,reason,status,assigned_operator_id)
       VALUES ($1,$2,NULL,$3,$4,'operator',$5,$6,'completed',$5)`,
      [
        input.communityId,
        String(target.platform),
        input.targetPlatformAccountId,
        actionType,
        input.operatorId,
        reason,
      ],
    );
    return true;
  }

  async assign(
    communityId: number,
    operatorId: number,
    workType: string,
    itemId: number,
  ): Promise<void> {
    const statements: Readonly<Record<string, string>> = {
      review:
        `UPDATE review_queue SET assigned_operator_id = $1 WHERE id = $2 AND status = 'open' AND EXISTS (SELECT 1 FROM messages WHERE messages.id = review_queue.message_id AND messages.community_id = $3) RETURNING id`,
      appeal:
        `UPDATE member_appeals SET assigned_operator_id = $1 WHERE id = $2 AND community_id = $3 AND status = 'open' RETURNING id`,
      report:
        `UPDATE member_reports SET assigned_operator_id = $1 WHERE id = $2 AND community_id = $3 AND status = 'open' RETURNING id`,
    };
    const sql = statements[workType.trim().toLocaleLowerCase()];
    if (!sql) throw new TypeError("invalid moderation work type");
    if (
      !(await this.connection.query(sql, [operatorId, itemId, communityId]))[0]
    ) throw new TypeError("moderation work item not found");
  }

  async resolveMember(
    communityId: number,
    operatorId: number,
    queueType: string,
    itemId: number,
    resolution: string,
    note: string,
  ): Promise<void> {
    const type = queueType.trim().toLocaleLowerCase();
    if (type !== "report" && type !== "appeal") {
      throw new TypeError("invalid member queue type");
    }
    if (!resolution.trim() || !note.trim()) {
      throw new TypeError("resolution and note are required");
    }
    await this.connection.transaction(async (connection) => {
      const table = type === "report" ? "member_reports" : "member_appeals";
      const item = (await connection.query(
        `SELECT status, assigned_operator_id${
          type === "appeal" ? ", moderation_action_id" : ""
        } FROM ${table} WHERE id = $1 AND community_id = $2 FOR UPDATE`,
        [itemId, communityId],
      ))[0];
      if (!item) throw new TypeError(`member ${type} not found`);
      if (String(item.status) !== "open") {
        throw new TypeError(`member ${type} is already resolved`);
      }
      if (type === "appeal" && item.assigned_operator_id !== null) {
        const action = (await connection.query(
          "SELECT actor_type, actor_id FROM moderation_actions WHERE id = $1",
          [Number(item.moderation_action_id)],
        ))[0];
        if (
          action?.actor_type === "operator" &&
          Number(action.actor_id) === operatorId
        ) {
          throw new TypeError(
            "high-severity appeal requires a different reviewer",
          );
        }
      }
      const column = type === "report" ? "resolution" : "disposition";
      await connection.query(
        `UPDATE ${table} SET status = 'resolved', ${column} = $1, resolution_note = $2, resolved_by_operator_id = $3, resolved_at = CURRENT_TIMESTAMP WHERE id = $4 AND community_id = $5`,
        [resolution.trim(), note.trim(), operatorId, itemId, communityId],
      );
      await connection.query(
        `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,$3,$4,$5)`,
        [
          operatorId,
          `member_${type}.resolved`,
          `member_${type}`,
          itemId,
          JSON.stringify({
            community_id: communityId,
            resolution: resolution.trim(),
          }),
        ],
      );
    });
  }

  async createRuleDraft(
    communityId: number,
    operatorId: number,
    config: ModerationRuleConfig,
  ): Promise<number> {
    const normalized = normalizeRule(config);
    return await this.connection.transaction(async (connection) => {
      const existing = (await connection.query(
        "SELECT id FROM moderation_rules WHERE community_id = $1 AND name = $2",
        [communityId, normalized.name],
      ))[0];
      let ruleId = Number(existing?.id);
      if (!Number.isInteger(ruleId)) {
        ruleId = Number(
          (await connection.query(
            `INSERT INTO moderation_rules(community_id,name,rule_type,pattern,severity,auto_enforce_action,enforcement_mode,action_duration_seconds,platform_scope_json,enabled)
           VALUES ($1,$2,$3,$4,$5,$6,'disabled',$7,$8,false) RETURNING id`,
            [
              communityId,
              normalized.name,
              normalized.rule_type,
              normalized.pattern,
              normalized.severity,
              normalized.auto_enforce_action ?? null,
              normalized.action_duration_seconds ?? 600,
              JSON.stringify(normalized.platform_scope),
            ],
          ))[0]?.id,
        );
      }
      const version = (await connection.query(
        "SELECT COALESCE(MAX(version_number), 0) + 1 AS version FROM moderation_rule_versions WHERE moderation_rule_id = $1",
        [ruleId],
      ))[0];
      const versionId = Number(
        (await connection.query(
          `INSERT INTO moderation_rule_versions(community_id,moderation_rule_id,version_number,lifecycle_state,config_json,created_by_operator_id)
         VALUES ($1,$2,$3,'draft',$4,$5) RETURNING id`,
          [
            communityId,
            ruleId,
            Number(version?.version),
            JSON.stringify(normalized),
            operatorId,
          ],
        ))[0]?.id,
      );
      await this.audit(
        connection,
        operatorId,
        "moderation.rule_drafted",
        "moderation_rule_version",
        versionId,
        { community_id: communityId, rule_id: ruleId },
      );
      return versionId;
    });
  }

  async saveRule(
    communityId: number,
    operatorId: number,
    config: ModerationRuleConfig,
    enabled: boolean,
    enforcementMode: string,
  ): Promise<number> {
    const normalized = normalizeRule(config);
    const mode = enforcementMode.trim().toLocaleLowerCase();
    if (!new Set(["disabled", "shadow", "enforce"]).has(mode)) {
      throw new TypeError("invalid rule enforcement mode");
    }
    return await this.connection.transaction(async (connection) => {
      const existing = (await connection.query(
        "SELECT id FROM moderation_rules WHERE community_id=$1 AND name=$2 FOR UPDATE",
        [communityId, normalized.name],
      ))[0];
      const values = [
        normalized.rule_type,
        normalized.pattern,
        normalized.severity,
        normalized.auto_enforce_action ?? null,
        enabled,
        mode,
        normalized.action_duration_seconds ?? 600,
        JSON.stringify(normalized.platform_scope),
      ];
      const ruleId = existing ? Number(existing.id) : Number(
        (await connection.query(
          `INSERT INTO moderation_rules(community_id,name,rule_type,pattern,severity,auto_enforce_action,enabled,enforcement_mode,action_duration_seconds,platform_scope_json) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id`,
          [communityId, normalized.name, ...values],
        ))[0]?.id,
      );
      if (existing) {
        await connection.query(
          `UPDATE moderation_rules SET rule_type=$1,pattern=$2,severity=$3,auto_enforce_action=$4,enabled=$5,enforcement_mode=$6,action_duration_seconds=$7,platform_scope_json=$8,updated_at=CURRENT_TIMESTAMP WHERE id=$9 AND community_id=$10`,
          [...values, ruleId, communityId],
        );
      }
      await this.audit(
        connection,
        operatorId,
        "moderation.rule_saved",
        "moderation_rule",
        ruleId,
        { community_id: communityId, name: normalized.name, mode, enabled },
      );
      return ruleId;
    });
  }

  async previewRule(
    communityId: number,
    versionId: number,
    samples: readonly string[],
  ): Promise<ModerationItem> {
    const version = await this.ruleVersion(
      this.connection,
      communityId,
      versionId,
    );
    const config = JSON.parse(
      String(version.config_json),
    ) as ModerationRuleConfig;
    const pattern = config.pattern.toLocaleLowerCase();
    const matched_indexes = samples.flatMap((sample, index) =>
      sample.toLocaleLowerCase().includes(pattern) ? [index] : []
    );
    const impact = Object.freeze({
      sample_count: samples.length,
      match_count: matched_indexes.length,
      matched_indexes,
    });
    await this.connection.query(
      "UPDATE moderation_rule_versions SET impact_json = $1 WHERE id = $2",
      [JSON.stringify(impact), versionId],
    );
    return impact;
  }

  async publishRule(
    communityId: number,
    operatorId: number,
    versionId: number,
    lifecycleState: string,
  ): Promise<number> {
    const state = lifecycleState.trim().toLocaleLowerCase();
    if (state !== "shadow" && state !== "enforce") {
      throw new TypeError("rule lifecycle state must be shadow or enforce");
    }
    return await this.connection.transaction(async (connection) => {
      const version = await this.ruleVersion(
        connection,
        communityId,
        versionId,
      );
      if (
        state === "enforce" &&
        Number(version.created_by_operator_id) === operatorId
      ) {
        throw new TypeError(
          "enforced rules require approval by a different operator",
        );
      }
      const config = JSON.parse(
        String(version.config_json),
      ) as ModerationRuleConfig;
      const ruleId = Number(version.moderation_rule_id);
      await connection.query(
        `UPDATE moderation_rules SET name=$1,rule_type=$2,pattern=$3,severity=$4,auto_enforce_action=$5,action_duration_seconds=$6,platform_scope_json=$7,enforcement_mode=$8,enabled=true,updated_at=CURRENT_TIMESTAMP WHERE id=$9 AND community_id=$10`,
        [
          config.name,
          config.rule_type,
          config.pattern,
          config.severity,
          config.auto_enforce_action ?? null,
          config.action_duration_seconds ?? 600,
          JSON.stringify(config.platform_scope),
          state,
          ruleId,
          communityId,
        ],
      );
      await connection.query(
        "UPDATE moderation_rule_versions SET lifecycle_state=$1,approved_by_operator_id=$2,approved_at=CURRENT_TIMESTAMP WHERE id=$3",
        [state, operatorId, versionId],
      );
      await this.audit(
        connection,
        operatorId,
        "moderation.rule_published",
        "moderation_rule_version",
        versionId,
        { community_id: communityId, lifecycle_state: state },
      );
      return ruleId;
    });
  }

  async rollbackRule(
    communityId: number,
    operatorId: number,
    versionId: number,
  ): Promise<number> {
    return await this.connection.transaction(async (connection) => {
      const source = await this.ruleVersion(connection, communityId, versionId);
      const config = JSON.parse(
        String(source.config_json),
      ) as ModerationRuleConfig;
      const ruleId = Number(source.moderation_rule_id);
      const state =
        new Set(["shadow", "enforce"]).has(String(source.lifecycle_state))
          ? String(source.lifecycle_state)
          : "shadow";
      const next = (await connection.query(
        "SELECT COALESCE(MAX(version_number),0)+1 AS version FROM moderation_rule_versions WHERE moderation_rule_id=$1",
        [ruleId],
      ))[0];
      await connection.query(
        `UPDATE moderation_rules SET name=$1,rule_type=$2,pattern=$3,severity=$4,auto_enforce_action=$5,action_duration_seconds=$6,platform_scope_json=$7,enforcement_mode=$8,enabled=true,updated_at=CURRENT_TIMESTAMP WHERE id=$9 AND community_id=$10`,
        [
          config.name,
          config.rule_type,
          config.pattern,
          config.severity,
          config.auto_enforce_action ?? null,
          config.action_duration_seconds ?? 600,
          JSON.stringify(config.platform_scope),
          state,
          ruleId,
          communityId,
        ],
      );
      const rollbackId = Number(
        (await connection.query(
          `INSERT INTO moderation_rule_versions(community_id,moderation_rule_id,version_number,lifecycle_state,config_json,impact_json,created_by_operator_id,approved_by_operator_id,approved_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$7,CURRENT_TIMESTAMP) RETURNING id`,
          [
            communityId,
            ruleId,
            Number(next?.version),
            state,
            JSON.stringify(config),
            JSON.stringify({ rollback_of: versionId }),
            operatorId,
          ],
        ))[0]?.id,
      );
      await this.audit(
        connection,
        operatorId,
        "moderation.rule_rolled_back",
        "moderation_rule_version",
        rollbackId,
        { community_id: communityId, rollback_of: versionId },
      );
      return rollbackId;
    });
  }

  async addRuleExemption(
    communityId: number,
    operatorId: number,
    ruleId: number,
    exemptionType: string,
    exemptionValue: string,
    reason: string,
  ): Promise<number> {
    const type = exemptionType.trim().toLocaleLowerCase();
    if (type !== "channel" && type !== "platform_account") {
      throw new TypeError("invalid moderation rule exemption type");
    }
    if (!exemptionValue.trim() || !reason.trim()) {
      throw new TypeError(
        "moderation rule exemption value and reason are required",
      );
    }
    return await this.connection.transaction(async (connection) => {
      if (
        !(await connection.query(
          "SELECT id FROM moderation_rules WHERE id=$1 AND community_id=$2",
          [ruleId, communityId],
        ))[0]
      ) throw new TypeError("moderation rule not found");
      const exemptionId = Number(
        (await connection.query(
          `INSERT INTO moderation_rule_exemptions(community_id,moderation_rule_id,exemption_type,exemption_value,reason,created_by_operator_id) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id`,
          [
            communityId,
            ruleId,
            type,
            exemptionValue.trim(),
            reason.trim(),
            operatorId,
          ],
        ))[0]?.id,
      );
      await this.audit(
        connection,
        operatorId,
        "moderation.rule_exemption_added",
        "moderation_rule_exemption",
        exemptionId,
        { community_id: communityId, rule_id: ruleId },
      );
      return exemptionId;
    });
  }

  async saveFilter(
    communityId: number,
    operatorId: number,
    name: string,
    filters: ModerationItem,
  ): Promise<number> {
    const cleanedName = name.trim();
    if (!cleanedName) throw new TypeError("saved filter name is required");
    await this.connection.query(
      `INSERT INTO moderation_saved_filters(community_id,operator_id,name,filters_json) VALUES ($1,$2,$3,$4) ON CONFLICT(community_id,operator_id,name) DO UPDATE SET filters_json=EXCLUDED.filters_json,updated_at=CURRENT_TIMESTAMP`,
      [communityId, operatorId, cleanedName, JSON.stringify(filters)],
    );
    const saved = (await this.connection.query(
      "SELECT id FROM moderation_saved_filters WHERE community_id=$1 AND operator_id=$2 AND name=$3",
      [communityId, operatorId, cleanedName],
    ))[0];
    return Number(saved?.id);
  }

  async listWork(
    communityId: number,
    operatorId: number,
    query: ModerationWorkQuery,
  ): Promise<ModerationWorkResult> {
    const queue = query.queue?.trim().toLocaleLowerCase() || "unassigned";
    if (
      !new Set([
        "all",
        "unassigned",
        "mine",
        "escalated",
        "appeals",
        "resolved",
      ]).has(queue)
    ) throw new TypeError("invalid moderation work queue");
    const page = Math.max(1, Math.trunc(query.page ?? 1));
    const parameters: (string | number)[] = [communityId];
    const bind = (value: string | number): string => {
      parameters.push(value);
      return `$${parameters.length}`;
    };
    const conditions: string[] = [];
    if (queue === "unassigned") {
      conditions.push("status='open' AND assigned_operator_id IS NULL");
    } else if (queue === "mine") {
      conditions.push(
        `status='open' AND assigned_operator_id=${bind(operatorId)}`,
      );
    } else if (queue === "escalated") {
      conditions.push("work_type='review' AND resolution_state='escalated'");
    } else if (queue === "appeals") {
      conditions.push("work_type='appeal' AND status='open'");
    } else if (queue === "resolved") conditions.push("status<>'open'");
    if (query.search?.trim()) {
      const token = bind(`%${query.search.trim()}%`);
      conditions.push(
        `(username ILIKE ${token} OR reason ILIKE ${token} OR summary ILIKE ${token})`,
      );
    }
    if (query.severity?.trim()) {
      conditions.push(
        `LOWER(severity)=${bind(query.severity.trim().toLocaleLowerCase())}`,
      );
    }
    if (query.rule?.trim()) {
      conditions.push(`reason ILIKE ${bind(`%${query.rule.trim()}%`)}`);
    }
    if (query.platform?.trim()) {
      conditions.push(
        `LOWER(platform)=${bind(query.platform.trim().toLocaleLowerCase())}`,
      );
    }
    if (query.startAt?.trim()) {
      conditions.push(
        `created_at::timestamptz>=${bind(query.startAt.trim())}::timestamptz`,
      );
    }
    if (query.endAt?.trim()) {
      conditions.push(
        `created_at::timestamptz<=${bind(query.endAt.trim())}::timestamptz`,
      );
    }
    if (query.assignment === "unassigned") {
      conditions.push("assigned_operator_id IS NULL");
    } else if (query.assignment === "mine") {
      conditions.push(`assigned_operator_id=${bind(operatorId)}`);
    }
    const union =
      `SELECT 'review' AS work_type,q.id AS item_id,m.platform,p.username,q.severity,q.queue_reason_code AS reason,m.content_raw AS summary,q.assigned_operator_id,q.status,q.resolution AS resolution_state,q.created_at FROM review_queue q JOIN messages m ON m.id=q.message_id JOIN platform_accounts p ON p.id=m.platform_account_id WHERE m.community_id=$1 UNION ALL SELECT 'appeal',a.id,ma.platform,p.username,a.severity,a.reason,ma.action_type,a.assigned_operator_id,a.status,a.disposition,a.created_at FROM member_appeals a JOIN moderation_actions ma ON ma.id=a.moderation_action_id JOIN platform_accounts p ON p.id=a.appellant_platform_account_id WHERE a.community_id=$1 UNION ALL SELECT 'report',r.id,p.platform,p.username,r.severity,r.category,r.summary,r.assigned_operator_id,r.status,r.resolution,r.created_at FROM member_reports r JOIN platform_accounts p ON p.id=r.subject_platform_account_id WHERE r.community_id=$1`;
    const offset = bind((page - 1) * 25);
    const result = await this.connection.query(
      `SELECT *,COUNT(*) OVER() AS total_count,EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-created_at::timestamptz))/3600 AS sla_age_hours FROM (${union}) AS work ${
        conditions.length ? `WHERE ${conditions.join(" AND ")}` : ""
      } ORDER BY created_at DESC,item_id DESC LIMIT 25 OFFSET ${offset}`,
      parameters,
    );
    return Object.freeze({
      items: rows(result),
      total: Number(result[0]?.total_count ?? 0),
      page,
    });
  }

  private async ruleVersion(
    connection: DatabaseConnection,
    communityId: number,
    versionId: number,
  ): Promise<DatabaseRow> {
    const version = (await connection.query(
      "SELECT * FROM moderation_rule_versions WHERE id = $1 AND community_id = $2 FOR UPDATE",
      [versionId, communityId],
    ))[0];
    if (!version) throw new TypeError("moderation rule version not found");
    return version;
  }

  private async audit(
    connection: DatabaseConnection,
    operatorId: number,
    actionType: string,
    entityType: string,
    entityId: number,
    payload: ModerationItem,
  ): Promise<void> {
    await connection.query(
      `INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,$3,$4,$5)`,
      [operatorId, actionType, entityType, entityId, JSON.stringify(payload)],
    );
  }

  private async memberQueue(
    type: "reports" | "appeals",
    communityId: number,
  ): Promise<readonly ModerationItem[]> {
    const sql = type === "reports"
      ? `SELECT r.id AS item_id, 'reports' AS queue_type, p.username, r.severity,
                r.category AS category_or_reason, r.summary, r.assigned_operator_id, r.created_at
           FROM member_reports AS r JOIN platform_accounts AS p ON p.id = r.subject_platform_account_id
          WHERE r.community_id = $1 AND r.status = 'open' ORDER BY r.created_at, r.id LIMIT 25`
      : `SELECT a.id AS item_id, 'appeals' AS queue_type, p.username, a.severity,
                a.reason AS category_or_reason, m.action_type AS summary,
                a.assigned_operator_id, a.created_at
           FROM member_appeals AS a JOIN platform_accounts AS p ON p.id = a.appellant_platform_account_id
           JOIN moderation_actions AS m ON m.id = a.moderation_action_id
          WHERE a.community_id = $1 AND a.status = 'open' ORDER BY a.created_at, a.id LIMIT 25`;
    return rows(await this.connection.query(sql, [communityId]));
  }
}

function normalizeRule(config: ModerationRuleConfig): ModerationRuleConfig {
  const normalized = {
    name: config.name.trim(),
    rule_type: config.rule_type.trim().toLocaleLowerCase(),
    pattern: config.pattern.trim(),
    severity: config.severity.trim().toLocaleLowerCase(),
    auto_enforce_action:
      config.auto_enforce_action?.trim().toLocaleLowerCase() || null,
    action_duration_seconds: Math.max(
      1,
      Math.min(config.action_duration_seconds ?? 600, 2_419_200),
    ),
    platform_scope: [
      ...new Set(
        config.platform_scope.map((value) => value.trim().toLocaleLowerCase()),
      ),
    ],
  };
  if (!normalized.name || !normalized.pattern) {
    throw new TypeError("moderation rule name and pattern are required");
  }
  if (
    !new Set([
      "exact_term",
      "banned_phrase",
      "streamboo_viewer_spam",
      "link_restriction",
      "duplicate_message",
      "egregious_term",
    ]).has(normalized.rule_type)
  ) throw new TypeError("unsupported moderation rule type");
  if (
    !new Set(["low", "medium", "high", "critical"]).has(normalized.severity)
  ) throw new TypeError("unsupported moderation severity");
  if (
    normalized.auto_enforce_action !== null &&
    !new Set(["warn", "timeout", "ban"]).has(normalized.auto_enforce_action)
  ) throw new TypeError("unsupported moderation action");
  if (
    !normalized.platform_scope.length ||
    normalized.platform_scope.some((platform) =>
      platform !== "discord" && platform !== "twitch"
    )
  ) throw new TypeError("platform_scope must contain discord or twitch");
  return Object.freeze(normalized);
}
