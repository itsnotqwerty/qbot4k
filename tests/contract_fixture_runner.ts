import { NormalizedMessage } from "../src/core/models.ts";
import {
  formatCommandTemplate,
  parseCommand,
  renderCommandReply,
} from "../src/domain/command_domain.ts";
import {
  evaluateMessageModeration,
  type ModerationRule,
} from "../src/domain/moderation_rules.ts";
import {
  calculateTemporalRisk,
  scoreDeltaForMessage,
  scoreDeltaForModeration,
} from "../src/domain/scoring.ts";
import {
  calculateCountSample,
  calculateFreshnessSample,
  calculateLatencySample,
  calculatePercentageSample,
  type TenantSloSample,
} from "../src/domain/slo.ts";
import {
  calculateQuotaWindow,
  DEFAULT_TENANT_QUOTAS,
  normalizeQuotaType,
  validateQuotaPolicy,
} from "../src/domain/quota.ts";
import {
  permissionDecision,
  platformCapabilities,
} from "../src/security/permissions.ts";
import { calculateDerivedSignals } from "../src/domain/signals.ts";
import {
  calculateCohortBaseline,
  calculateEmergingTopics,
  calculateGraphMetrics,
  calculateIdentitySuggestions,
  calculatePropagationPath,
} from "../src/domain/analytics.ts";
import { understandContent } from "../src/domain/content_analysis.ts";
import {
  detectServerBoostSuccess,
  isServerBoostConfirmation,
  serverBoostCommandName,
} from "../src/domain/server_boosts.ts";
import {
  calculateJoinRaidFinding,
  calculateMessageAbuseFindings,
  validateAntiAbusePolicy,
} from "../src/domain/abuse.ts";
import { calculateCoordinationCampaign } from "../src/domain/campaigns.ts";

type JsonObject = { [key: string]: Json };
type Json = null | boolean | number | string | Json[] | JsonObject;
type Scenario = { id: string; operation: string; input: Json };

export function evaluate(operation: string, value: Json): Json | Promise<Json> {
  if (operation === "authorize_cases") {
    const cases = value as Array<{
      actor_community_id: number;
      requested_community_id: number;
      required_capability: string;
      granted_capabilities: string[];
    }>;
    return cases.map((entry) => {
      const sameTenant =
        entry.actor_community_id === entry.requested_community_id;
      const hasCapability = entry.granted_capabilities.includes(
        entry.required_capability,
      );
      return {
        authorized: sameTenant && hasCapability,
        reason: !sameTenant
          ? "tenant_mismatch"
          : hasCapability
          ? "allowed"
          : "capability_denied",
      };
    });
  }
  if (operation === "select_tenant") {
    const input = value as { community_id: number; records: JsonObject[] };
    return input.records.filter((record) =>
      record.community_id === input.community_id
    );
  }
  if (operation === "project") {
    const input = value as { fields: string[]; record: JsonObject };
    return Object.fromEntries(
      input.fields.map((field) => [field, input.record[field]]),
    );
  }
  if (operation === "sort_jobs") {
    const jobs = value as Array<{ id: number; priority: number }>;
    return [...jobs].sort((left, right) =>
      right.priority - left.priority || left.id - right.id
    );
  }
  if (operation === "parse_command") {
    const parsed = parseCommand(value as string);
    if (parsed === null) return null;
    return {
      name: parsed[0],
      arguments: [...parsed[1]],
    };
  }
  if (operation === "render_command") {
    const input = value as {
      platform: string;
      reply: {
        card: {
          title: string;
          description: string;
          fields?: Array<{ name: string; value: string; inline?: boolean }>;
          footer?: string | null;
          color?: number | null;
        };
        text_only?: boolean;
      };
    };
    return renderCommandReply({
      card: input.reply.card,
      textOnly: input.reply.text_only,
    }, input.platform) as Json;
  }
  if (operation === "format_command_template") {
    const input = value as {
      template: string;
      values?: JsonObject;
      responses?: Record<string, string>;
    };
    return formatCommandTemplate(
      input.template,
      input.values,
      (_lower, upper) => upper,
      (method, url) => input.responses?.[`${method} ${url}`] ?? null,
    );
  }
  if (operation === "normalize_provider") {
    const input = value as JsonObject;
    return {
      external_event_id: String(input.external_event_id),
      platform: (input.platform as string).trim().toLowerCase(),
      username: (input.username as string).trim(),
    };
  }
  if (operation === "moderate_message") {
    const input = value as {
      message: {
        platform: string;
        platform_user_id: string;
        username: string;
        channel_id: string;
        content_raw: string;
        sent_at: string;
        is_moderator?: boolean;
        metadata?: JsonObject;
      };
      rules: Array<{
        id: number;
        name: string;
        rule_type: string;
        pattern: string;
        severity: string;
        auto_enforce_action: string | null;
        enabled: boolean;
        enforcement_mode: string;
        action_duration_seconds: number;
      }>;
    };
    const message = new NormalizedMessage({
      platform: input.message.platform,
      platformUserId: input.message.platform_user_id,
      username: input.message.username,
      channelId: input.message.channel_id,
      contentRaw: input.message.content_raw,
      sentAt: input.message.sent_at,
      isModerator: input.message.is_moderator,
      metadata: input.message.metadata,
    });
    const rules: ModerationRule[] = input.rules.map((rule) => ({
      id: rule.id,
      name: rule.name,
      ruleType: rule.rule_type,
      pattern: rule.pattern,
      severity: rule.severity,
      autoEnforceAction: rule.auto_enforce_action,
      enabled: rule.enabled,
      enforcementMode: rule.enforcement_mode,
      actionDurationSeconds: rule.action_duration_seconds,
    }));
    return evaluateMessageModeration(message, rules).map((finding) => ({
      rule_id: finding.ruleId,
      rule_name: finding.ruleName,
      rule_type: finding.ruleType,
      severity: finding.severity,
      reason_code: finding.reasonCode,
      auto_enforce_action: finding.autoEnforceAction,
      enforcement_mode: finding.enforcementMode,
      action_duration_seconds: finding.actionDurationSeconds,
    }));
  }
  if (operation === "score_message") {
    const score = scoreDeltaForMessage(value as string);
    return score === null ? null : [...score];
  }
  if (operation === "score_moderation") {
    const input = value as {
      severity: string;
      action_type?: string | null;
      reason_code?: string | null;
    };
    return [...scoreDeltaForModeration({
      severity: input.severity,
      actionType: input.action_type,
      reasonCode: input.reason_code,
    })];
  }
  if (operation === "calculate_slo") {
    const items = value as Array<{
      kind: "latency" | "percentage" | "count" | "freshness";
      name?: string;
      target?: number;
      values?: number[];
      value?: number | null;
    }>;
    return items.map((item) => {
      let result: TenantSloSample;
      if (item.kind === "latency") {
        result = calculateLatencySample(item.name!, item.target!, item.values!);
      } else if (item.kind === "percentage") {
        result = calculatePercentageSample(
          item.name!,
          item.target!,
          item.values!,
        );
      } else if (item.kind === "count") {
        result = calculateCountSample(item.name!, item.target!, item.value!);
      } else {
        result = calculateFreshnessSample(item.value ?? null);
      }
      return {
        metric_name: result.metricName,
        value: result.value,
        target_value: result.targetValue,
        status: result.status,
        evidence_count: result.evidenceCount,
      };
    });
  }
  if (operation === "calculate_quota") {
    const input = value as {
      quota_type: string;
      limit_count: number;
      window_seconds: number;
      epoch: number;
    };
    const quotaType = normalizeQuotaType(input.quota_type);
    const policy = validateQuotaPolicy(
      input.limit_count,
      input.window_seconds,
    );
    const window = calculateQuotaWindow(input.epoch, policy.windowSeconds);
    const defaults = DEFAULT_TENANT_QUOTAS[quotaType];
    return {
      quota_type: quotaType,
      limit_count: policy.limitCount,
      window_seconds: policy.windowSeconds,
      window_epoch: window.windowEpoch,
      retry_after_seconds: window.retryAfterSeconds,
      default_limit_count: defaults.limitCount,
      default_window_seconds: defaults.windowSeconds,
    };
  }
  if (operation === "permission_decisions") {
    const input = value as {
      decisions: Array<{
        role: string | null;
        capability: string;
        override?: "grant" | "deny" | null;
      }>;
      platform: string;
    };
    return {
      decisions: input.decisions.map((item) =>
        permissionDecision(item.role, item.capability, item.override)
      ),
      platform_capabilities: [...platformCapabilities(input.platform)].sort(),
    };
  }
  if (operation === "calculate_signals") {
    const input = value as {
      evidence: {
        user_id: number;
        message_count: number;
        channel_count: number;
        platform_count: number;
        account_count: number;
        eligible_message_count: number;
        positive_count: number;
        negative_count: number;
        negative_points: number;
        reply_count: number;
        welcome_positive_count: number;
        welcome_count: number;
        welcome_duplicate_count: number;
        finding_count: number;
        severity_points: number;
        moderation_penalty_points: number;
        window_start?: string | null;
        window_end?: string | null;
      };
      calculated_at: string;
      signal_keys: string[];
    };
    const selected = new Set(input.signal_keys);
    return calculateDerivedSignals({
      userId: input.evidence.user_id,
      messageCount: input.evidence.message_count,
      channelCount: input.evidence.channel_count,
      platformCount: input.evidence.platform_count,
      accountCount: input.evidence.account_count,
      eligibleMessageCount: input.evidence.eligible_message_count,
      positiveCount: input.evidence.positive_count,
      negativeCount: input.evidence.negative_count,
      negativePoints: input.evidence.negative_points,
      replyCount: input.evidence.reply_count,
      welcomePositiveCount: input.evidence.welcome_positive_count,
      welcomeCount: input.evidence.welcome_count,
      welcomeDuplicateCount: input.evidence.welcome_duplicate_count,
      findingCount: input.evidence.finding_count,
      severityPoints: input.evidence.severity_points,
      moderationPenaltyPoints: input.evidence.moderation_penalty_points,
      windowStart: input.evidence.window_start,
      windowEnd: input.evidence.window_end,
    }, input.calculated_at).filter((signal) => selected.has(signal.signalKey))
      .map(
        (signal) => ({
          user_id: signal.userId,
          signal_key: signal.signalKey,
          value: signal.value,
          confidence: signal.confidence,
          evidence_count: signal.evidenceCount,
          window_start: signal.windowStart,
          window_end: signal.windowEnd,
          details: signal.details as JsonObject,
          analyzer_version: signal.analyzerVersion,
          calculated_at: signal.calculatedAt,
        }),
      );
  }
  if (operation === "calculate_cohort_baseline") {
    const baseline = calculateCohortBaseline(value as number[]);
    return {
      sample_size: baseline.sampleSize,
      mean_value: baseline.meanValue,
      stddev_value: baseline.stddevValue,
      median_value: baseline.medianValue,
      p90_value: baseline.p90Value,
    };
  }
  if (operation === "calculate_emerging_topics") {
    const input = value as {
      observations: Array<{
        observation_id: number;
        text: string;
        context_id: string;
        container_id: string;
        occurred_at: string;
      }>;
      community_id: number;
      now: string;
      topic_keys?: string[];
    };
    const selected = new Set(input.topic_keys ?? []);
    return calculateEmergingTopics(
      input.observations.map((item) => ({
        observationId: item.observation_id,
        text: item.text,
        contextId: item.context_id,
        containerId: item.container_id,
        occurredAt: item.occurred_at,
      })),
      input.community_id,
      input.now,
    )
      .filter((topic) => selected.size === 0 || selected.has(topic.topicKey))
      .map((topic) => ({
        topic_key: topic.topicKey,
        topic_kind: topic.topicKind,
        label: topic.label,
        current_count: topic.currentCount,
        baseline_rate: topic.baselineRate,
        velocity: topic.velocity,
        context_count: topic.contextCount,
        community_count: topic.communityCount,
        unusualness: topic.unusualness,
        first_observed_at: topic.firstObservedAt,
        last_observed_at: topic.lastObservedAt,
        cluster_terms: [...topic.clusterTerms],
        cross_community_diffusion: topic.crossCommunityDiffusion,
        evidence: topic.evidence.map((item) => [...item]),
      }));
  }
  if (operation === "calculate_graph_metrics") {
    const input = value as {
      edges: Array<{
        source_user_id: number;
        target_user_id: number;
        strength: number;
        last_observed_at: string;
      }>;
      calculated_at: string;
    };
    return calculateGraphMetrics(
      input.edges.map((edge) => ({
        sourceUserId: edge.source_user_id,
        targetUserId: edge.target_user_id,
        strength: edge.strength,
        lastObservedAt: edge.last_observed_at,
      })),
      input.calculated_at,
    ).map((metric) => ({
      user_id: metric.userId,
      in_degree: metric.inDegree,
      out_degree: metric.outDegree,
      weighted_degree: metric.weightedDegree,
      betweenness: metric.betweenness,
      pagerank: metric.pagerank,
      cluster_id: metric.clusterId,
      is_bridge: metric.isBridge,
      influence_score: metric.influenceScore,
    }));
  }
  if (operation === "calculate_identity_suggestions") {
    const input = value as {
      accounts: Array<{
        account_id: number;
        platform: string;
        platform_user_id: string;
        username: string;
        user_id: number | null;
        context: string;
      }>;
      minimum_confidence?: number;
    };
    return calculateIdentitySuggestions(
      input.accounts.map((account) => ({
        accountId: account.account_id,
        platform: account.platform,
        platformUserId: account.platform_user_id,
        username: account.username,
        userId: account.user_id,
        context: account.context,
      })),
      input.minimum_confidence,
    ).map((suggestion) => ({
      left_platform_account_id: suggestion.leftPlatformAccountId,
      right_platform_account_id: suggestion.rightPlatformAccountId,
      confidence: suggestion.confidence,
      username_similarity: suggestion.usernameSimilarity,
      identifier_similarity: suggestion.identifierSimilarity,
      shared_context: suggestion.sharedContext,
      manual_approval_required: suggestion.manualApprovalRequired,
      model_version: suggestion.modelVersion,
    }));
  }
  if (operation === "calculate_propagation_path") {
    const input = value as {
      occurrences: Array<{
        source_user_id: number;
        target_user_id: number;
        occurred_at: string;
      }>;
      source_user_id: number;
      target_user_id: number;
    };
    return [...calculatePropagationPath(
      input.occurrences.map((item) => ({
        sourceUserId: item.source_user_id,
        targetUserId: item.target_user_id,
        occurredAt: item.occurred_at,
      })),
      input.source_user_id,
      input.target_user_id,
    )];
  }
  if (operation === "calculate_temporal_risk") {
    const result = calculateTemporalRisk((value as Array<{
      window_name: string;
      value: number;
      confidence: number;
      evidence_count: number;
    }>).map((window) => ({
      windowName: window.window_name,
      value: window.value,
      confidence: window.confidence,
      evidenceCount: window.evidence_count,
    })));
    return result === null ? null : {
      value: result.value,
      confidence: result.confidence,
      evidence_count: result.evidenceCount,
      window_values: { ...result.windowValues },
    };
  }
  if (operation === "server_boost_detection") {
    return (value as Array<{
      content?: string;
      interaction_command_name?: string;
      embed_text?: string;
      author_is_bot: Json;
    }>).map((item) => {
      const content = item.content ?? "";
      const interactionCommandName = item.interaction_command_name ?? "";
      const embedText = item.embed_text ?? "";
      const metadata = {
        author_is_bot: item.author_is_bot,
        interaction_command_name: interactionCommandName,
        embed_text: embedText,
      };
      return {
        command_name: serverBoostCommandName(content, interactionCommandName),
        success_command: detectServerBoostSuccess(
          content,
          interactionCommandName,
          embedText,
        ),
        is_confirmation: isServerBoostConfirmation({
          contentRaw: content,
          metadata,
        }),
      };
    });
  }
  if (operation === "anti_abuse_decisions") {
    const input = value as {
      policy: {
        enabled: boolean;
        enforcement_mode: string;
        message_burst_limit: number;
        message_burst_window_seconds: number;
        mention_limit: number;
        join_raid_limit: number;
        join_raid_window_seconds: number;
      };
      message_input: {
        community_id: number;
        platform_account_id: number;
        occurred_at: string;
        recent_message_count: number;
        mention_count: number;
        is_moderator: boolean;
      };
      join_input: {
        community_id: number;
        occurred_at: string;
        join_count: number;
      };
    };
    const policy = validateAntiAbusePolicy({
      enabled: input.policy.enabled,
      enforcementMode: input.policy.enforcement_mode,
      messageBurstLimit: input.policy.message_burst_limit,
      messageBurstWindowSeconds: input.policy.message_burst_window_seconds,
      mentionLimit: input.policy.mention_limit,
      joinRaidLimit: input.policy.join_raid_limit,
      joinRaidWindowSeconds: input.policy.join_raid_window_seconds,
    });
    const messageFindings = calculateMessageAbuseFindings(policy, {
      communityId: input.message_input.community_id,
      platformAccountId: input.message_input.platform_account_id,
      occurredAt: input.message_input.occurred_at,
      recentMessageCount: input.message_input.recent_message_count,
      mentionCount: input.message_input.mention_count,
      isModerator: input.message_input.is_moderator,
    });
    const joinFinding = calculateJoinRaidFinding(policy, {
      communityId: input.join_input.community_id,
      occurredAt: input.join_input.occurred_at,
      joinCount: input.join_input.join_count,
    });
    const findingJson = (finding: typeof joinFinding) =>
      finding === null ? null : {
        reason_code: finding.reasonCode,
        title: finding.title,
        severity: finding.severity,
        window_seconds: finding.windowSeconds,
        dedupe_key: finding.dedupeKey,
        enforcement_required: finding.enforcementRequired,
      };
    return {
      policy: {
        enabled: policy.enabled,
        enforcement_mode: policy.enforcementMode,
        message_burst_limit: policy.messageBurstLimit,
        message_burst_window_seconds: policy.messageBurstWindowSeconds,
        mention_limit: policy.mentionLimit,
        join_raid_limit: policy.joinRaidLimit,
        join_raid_window_seconds: policy.joinRaidWindowSeconds,
      },
      message_findings: messageFindings.map((finding) => findingJson(finding)!),
      join_finding: findingJson(joinFinding),
    };
  }
  if (operation === "calculate_coordination_campaign") {
    const input = value as {
      current: { observation_id: number; text: string; user_id: number | null };
      candidates: Array<{
        observation_id: number;
        text: string;
        user_id: number | null;
      }>;
    };
    const toObservation = (item: typeof input.current) => ({
      observationId: item.observation_id,
      text: item.text,
      userId: item.user_id,
    });
    return calculateCoordinationCampaign(
      toObservation(input.current),
      input.candidates.map(toObservation),
    ).then((result): Json =>
      result === null ? null : {
        campaign_key: result.campaignKey,
        campaign_type: result.campaignType,
        severity: result.severity,
        message_count: result.messageCount,
        actor_count: result.actorCount,
        confidence: result.confidence,
        domains: [...result.domains],
        tokens: [...result.tokens],
        members: result.members.map((member) => ({
          observation_id: member.observationId,
          user_id: member.userId,
          similarity: member.similarity,
        })),
      }
    );
  }
  if (operation === "understand_content") {
    const input = value as {
      text: string;
      attributes?: JsonObject;
    };
    const result = understandContent(input.text, input.attributes);
    return {
      language_code: result.languageCode,
      language_confidence: result.languageConfidence,
      sentiment_label: result.sentimentLabel,
      sentiment_score: result.sentimentScore,
      intent_label: result.intentLabel,
      intent_confidence: result.intentConfidence,
      threat_level: result.threatLevel,
      threat_score: result.threatScore,
      indicators: [...result.indicators],
      entities: result.entities.map((entity) => [...entity]),
      conversation: result.conversation as JsonObject,
    };
  }
  if (operation === "normalize_html") {
    return (value as string).replace(/\s+/g, " ").trim();
  }
  throw new Error(`unsupported fixture operation: ${operation}`);
}

if (import.meta.main) {
  const fixture = JSON.parse(await Deno.readTextFile(Deno.args[0])) as {
    scenarios: Scenario[];
  };
  const output = Object.fromEntries(
    await Promise.all(fixture.scenarios.map(async (scenario) =>
      [
        scenario.id,
        await evaluate(scenario.operation, scenario.input),
      ] as const
    )),
  );
  console.log(JSON.stringify(output));
}
