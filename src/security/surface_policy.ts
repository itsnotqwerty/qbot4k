export type SurfaceScope = "global" | "community" | "installation";
export type SurfaceKind =
  | "http"
  | "export"
  | "sse"
  | "bot_command"
  | "job"
  | "direct_lookup"
  | "provider_action";

export interface SurfacePolicy {
  readonly capability: string;
  readonly guard: string;
  readonly scope: SurfaceScope;
  readonly kind: SurfaceKind;
}

const dashboardHandlers = [
  "_serve_analytics",
  "_serve_analytics_export",
  "_serve_announcement_approve",
  "_serve_announcement_cancel",
  "_serve_announcement_create",
  "_serve_announcement_retry",
  "_serve_announcements",
  "_serve_api_actions",
  "_serve_api_add_note",
  "_serve_api_alert_workflow",
  "_serve_api_analytics",
  "_serve_api_audit",
  "_serve_api_bulk_moderation",
  "_serve_api_case",
  "_serve_api_case_export",
  "_serve_api_conversation_context",
  "_serve_api_event",
  "_serve_api_external_observation",
  "_serve_api_health",
  "_serve_api_identity_review",
  "_serve_api_integration_revoke",
  "_serve_api_intelligence",
  "_serve_api_intelligence_report",
  "_serve_api_link_user",
  "_serve_api_live_ops",
  "_serve_api_observation_pivots",
  "_serve_api_overview",
  "_serve_api_review_resolve",
  "_serve_api_reviews",
  "_serve_api_rule_save",
  "_serve_api_rules",
  "_serve_api_save_query",
  "_serve_api_search",
  "_serve_api_signals",
  "_serve_api_slo",
  "_serve_api_user_detail",
  "_serve_api_users",
  "_serve_audit",
  "_serve_commands",
  "_serve_commands_update",
  "_serve_community_switch",
  "_serve_dashboard",
  "_serve_dashboard_go_live",
  "_serve_dashboard_reset_database",
  "_serve_dashboard_restart",
  "_serve_discord_install_callback",
  "_serve_discord_link",
  "_serve_integrations",
  "_serve_intelligence",
  "_serve_intelligence_alert_case",
  "_serve_intelligence_alert_disposition",
  "_serve_intelligence_alert_workflow",
  "_serve_intelligence_case",
  "_serve_intelligence_case_action",
  "_serve_intelligence_report_generate",
  "_serve_live_ops",
  "_serve_live_ops_chat_settings",
  "_serve_live_ops_handoff",
  "_serve_live_ops_incident_action",
  "_serve_live_ops_moderate",
  "_serve_live_ops_notification_destination",
  "_serve_live_ops_playbook",
  "_serve_live_ops_shield_mode",
  "_serve_live_ops_shift_schedule",
  "_serve_live_ops_stream",
  "_serve_login",
  "_serve_logout",
  "_serve_member_queue_resolve",
  "_serve_moderation",
  "_serve_moderation_filter_save",
  "_serve_moderation_review_resolve",
  "_serve_moderation_rule_draft",
  "_serve_moderation_rule_exemption",
  "_serve_moderation_rule_preview",
  "_serve_moderation_rule_publish",
  "_serve_moderation_rule_rollback",
  "_serve_moderation_rule_save",
  "_serve_moderation_work_assign",
  "_serve_oauth_callback",
  "_serve_onboarding",
  "_serve_onboarding_resource_delete",
  "_serve_onboarding_resource_save",
  "_serve_onboarding_update",
  "_serve_onboarding_verify",
  "_serve_operator_access_action",
  "_serve_operator_invitation",
  "_serve_public_home",
  "_serve_search",
  "_serve_search_export",
  "_serve_search_save",
  "_serve_settings",
  "_serve_settings_operator_invite",
  "_serve_settings_update",
  "_serve_signals",
  "_serve_system_health",
  "_serve_twitch_eventsub",
  "_serve_twitch_install_callback",
  "_serve_twitch_link",
  "_serve_user_lifecycle_export",
  "_serve_user_messages",
  "_serve_user_moderation_action",
  "_serve_users",
  "_serve_users_link",
  "_serve_users_unlink",
] as const;

const publicHandlers = new Set([
  "_serve_login",
  "_serve_oauth_callback",
  "_serve_privacy_policy",
  "_serve_public_home",
  "_serve_system_health",
  "_serve_terms_of_service",
]);
const ingestHandlers = new Set([
  "_serve_api_event",
  "_serve_api_external_observation",
]);
const integrationHandlers = new Set([
  "_serve_api_integration_revoke",
  "_serve_discord_install_callback",
  "_serve_discord_link",
  "_serve_integrations",
  "_serve_twitch_install_callback",
  "_serve_twitch_link",
]);
const operatorHandlers = new Set([
  "_serve_operator_access_action",
  "_serve_operator_invitation",
  "_serve_settings_operator_invite",
]);
const announcementHandlers = new Set([
  "_serve_announcement_approve",
  "_serve_announcement_cancel",
  "_serve_announcement_create",
  "_serve_announcement_retry",
  "_serve_announcements",
]);
const ruleHandlers = new Set([
  "_serve_api_rule_save",
  "_serve_api_rules",
  "_serve_moderation_rule_draft",
  "_serve_moderation_rule_exemption",
  "_serve_moderation_rule_preview",
  "_serve_moderation_rule_publish",
  "_serve_moderation_rule_rollback",
  "_serve_moderation_rule_save",
]);
const moderationHandlers = new Set([
  "_serve_api_review_resolve",
  "_serve_live_ops_moderate",
  "_serve_moderation_review_resolve",
  "_serve_moderation_work_assign",
  "_serve_user_moderation_action",
]);
const moderationQueueHandlers = new Set([
  "_serve_api_actions",
  "_serve_api_reviews",
  "_serve_moderation",
  "_serve_moderation_filter_save",
]);
const exportHandlers = new Set([
  "_serve_analytics_export",
  "_serve_api_case_export",
  "_serve_search_export",
  "_serve_user_lifecycle_export",
]);
const auditHandlers = new Set(["_serve_api_audit", "_serve_audit"]);
const settingsHandlers = new Set([
  "_serve_commands",
  "_serve_commands_update",
  "_serve_onboarding",
  "_serve_onboarding_resource_delete",
  "_serve_onboarding_resource_save",
  "_serve_onboarding_update",
  "_serve_onboarding_verify",
  "_serve_settings",
  "_serve_settings_update",
]);
const liveControlHandlers = new Set([
  "_serve_live_ops_chat_settings",
  "_serve_live_ops_handoff",
  "_serve_live_ops_incident_action",
  "_serve_live_ops_notification_destination",
  "_serve_live_ops_playbook",
  "_serve_live_ops_shield_mode",
  "_serve_live_ops_shift_schedule",
]);

function dashboardCapability(handler: string): string {
  if (publicHandlers.has(handler)) return "public.access";
  if (handler === "_serve_twitch_eventsub") return "events.ingest";
  if (ingestHandlers.has(handler)) return "events.write";
  if (integrationHandlers.has(handler)) return "integrations.manage";
  if (operatorHandlers.has(handler)) return "operators.manage";
  if (announcementHandlers.has(handler)) return "announcements.manage";
  if (ruleHandlers.has(handler)) return "rules.manage";
  if (moderationHandlers.has(handler)) return "moderation.manage";
  if (handler === "_serve_api_bulk_moderation") return "moderation.bulk";
  if (handler === "_serve_member_queue_resolve") return "appeals.manage";
  if (moderationQueueHandlers.has(handler)) return "moderation.queues.read";
  if (exportHandlers.has(handler)) return "exports.create";
  if (auditHandlers.has(handler)) return "audit.read";
  if (settingsHandlers.has(handler)) return "settings.manage";
  if (liveControlHandlers.has(handler)) return "live_ops.manage";
  if (
    ["_serve_live_ops", "_serve_api_live_ops", "_serve_live_ops_stream"]
      .includes(handler)
  ) {
    return "live_ops.read";
  }
  if (handler.includes("user")) return "members.read";
  if (
    handler.includes("analytics") || handler.includes("signal") ||
    handler === "_serve_api_slo"
  ) {
    return "analytics.read";
  }
  if (
    handler.includes("case") || handler.includes("alert") ||
    handler.includes("intelligence")
  ) {
    return "cases.manage";
  }
  return "dashboard.access";
}

function dashboardGuard(handler: string): string {
  if (publicHandlers.has(handler)) return "public";
  if (handler === "_serve_twitch_eventsub") return "webhook_signature";
  if (ingestHandlers.has(handler)) return "api_client_or_session";
  if (handler === "_serve_logout") return "optional_session";
  return "session";
}

export const DASHBOARD_SURFACE_POLICIES: ReadonlyMap<string, SurfacePolicy> =
  new Map(
    dashboardHandlers.map((handler) => [
      handler,
      Object.freeze({
        capability: dashboardCapability(handler),
        guard: dashboardGuard(handler),
        scope: publicHandlers.has(handler)
          ? "global"
          : handler === "_serve_twitch_eventsub"
          ? "installation"
          : "community",
        kind: handler === "_serve_live_ops_stream"
          ? "sse"
          : exportHandlers.has(handler)
          ? "export"
          : "http",
      }),
    ]),
  );

const nonHttpPolicyEntries: readonly (readonly [string, SurfacePolicy])[] = [
  ["command:addcom", {
    capability: "settings.manage",
    guard: "command_operator",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:editcom", {
    capability: "settings.manage",
    guard: "command_operator",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:delcom", {
    capability: "settings.manage",
    guard: "command_operator",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:alias", {
    capability: "settings.manage",
    guard: "command_operator",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:verify", {
    capability: "community.read",
    guard: "platform_identity",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:credit", {
    capability: "members.read",
    guard: "installation_context",
    scope: "community",
    kind: "bot_command",
  }],
  ["command:custom", {
    capability: "dashboard.access",
    guard: "installation_context",
    scope: "community",
    kind: "bot_command",
  }],
  ["job:maintenance", {
    capability: "system.maintenance",
    guard: "system",
    scope: "community",
    kind: "job",
  }],
  ["job:twitch_live_announcements", {
    capability: "announcements.manage",
    guard: "system",
    scope: "installation",
    kind: "job",
  }],
  ["job:scheduled_announcements", {
    capability: "announcements.manage",
    guard: "system",
    scope: "installation",
    kind: "job",
  }],
  ["job:onboarding_roles", {
    capability: "members.manage",
    guard: "system",
    scope: "installation",
    kind: "job",
  }],
  ["job:onboarding_checkpoints", {
    capability: "members.manage",
    guard: "system",
    scope: "community",
    kind: "job",
  }],
  ["lookup:installation", {
    capability: "integrations.manage",
    guard: "compound_lookup",
    scope: "installation",
    kind: "direct_lookup",
  }],
  ["lookup:member", {
    capability: "members.read",
    guard: "compound_lookup",
    scope: "community",
    kind: "direct_lookup",
  }],
  ["lookup:moderation", {
    capability: "moderation.queues.read",
    guard: "compound_lookup",
    scope: "community",
    kind: "direct_lookup",
  }],
  ["provider:moderation", {
    capability: "moderation.manage",
    guard: "installation_capability",
    scope: "installation",
    kind: "provider_action",
  }],
  ["provider:announcement", {
    capability: "announcements.manage",
    guard: "installation_capability",
    scope: "installation",
    kind: "provider_action",
  }],
  ["provider:live_control", {
    capability: "live_ops.manage",
    guard: "installation_capability",
    scope: "installation",
    kind: "provider_action",
  }],
];

export const NON_HTTP_SURFACE_POLICIES: ReadonlyMap<string, SurfacePolicy> =
  new Map(
    nonHttpPolicyEntries.map((
      [surface, policy],
    ) => [surface, Object.freeze(policy)]),
  );

export const SURFACE_POLICIES: ReadonlyMap<string, SurfacePolicy> = new Map([
  ...DASHBOARD_SURFACE_POLICIES,
  ...NON_HTTP_SURFACE_POLICIES,
]);

export const INSTALLATION_CAPABILITY_BY_SURFACE: ReadonlyMap<string, string> =
  new Map([
    ["provider:moderation", "moderation_actions"],
    ["provider:announcement", "announcements"],
    ["provider:live_control", "live_controls"],
  ]);

export function requireNonHttpSurface(
  surface: string,
  guard: string,
): SurfacePolicy {
  const policy = NON_HTTP_SURFACE_POLICIES.get(surface);
  if (!policy) throw new TypeError(`unmapped non-HTTP surface: ${surface}`);
  if (policy.guard !== guard) {
    throw new Deno.errors.PermissionDenied(
      `surface ${surface} requires ${policy.guard}`,
    );
  }
  return policy;
}
