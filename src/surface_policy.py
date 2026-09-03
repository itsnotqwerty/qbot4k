from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SurfacePolicy:
    capability: str
    guard: str
    scope: str
    kind: str


_DASHBOARD_HANDLERS = frozenset({
    "_serve_analytics", "_serve_analytics_export", "_serve_announcement_approve",
    "_serve_announcement_cancel", "_serve_announcement_create", "_serve_announcement_retry",
    "_serve_announcements", "_serve_api_actions", "_serve_api_add_note",
    "_serve_api_alert_workflow", "_serve_api_analytics", "_serve_api_audit",
    "_serve_api_bulk_moderation", "_serve_api_case", "_serve_api_case_export",
    "_serve_api_conversation_context", "_serve_api_event", "_serve_api_external_observation",
    "_serve_api_health", "_serve_api_identity_review", "_serve_api_integration_revoke",
    "_serve_api_intelligence", "_serve_api_intelligence_report", "_serve_api_link_user",
    "_serve_api_live_ops", "_serve_api_observation_pivots", "_serve_api_overview",
    "_serve_api_review_resolve", "_serve_api_reviews", "_serve_api_rule_save",
    "_serve_api_rules", "_serve_api_save_query", "_serve_api_search", "_serve_api_signals",
    "_serve_api_slo",
    "_serve_api_user_detail", "_serve_api_users", "_serve_audit", "_serve_commands",
    "_serve_commands_update", "_serve_community_switch", "_serve_dashboard",
    "_serve_dashboard_go_live", "_serve_dashboard_reset_database", "_serve_dashboard_restart",
    "_serve_discord_install_callback", "_serve_discord_link", "_serve_integrations",
    "_serve_intelligence", "_serve_intelligence_alert_case",
    "_serve_intelligence_alert_disposition", "_serve_intelligence_alert_workflow",
    "_serve_intelligence_case", "_serve_intelligence_case_action",
    "_serve_intelligence_report_generate", "_serve_live_ops", "_serve_live_ops_chat_settings",
    "_serve_live_ops_handoff", "_serve_live_ops_incident_action", "_serve_live_ops_moderate",
    "_serve_live_ops_notification_destination", "_serve_live_ops_playbook",
    "_serve_live_ops_shield_mode", "_serve_live_ops_shift_schedule", "_serve_live_ops_stream",
    "_serve_login", "_serve_logout", "_serve_member_queue_resolve", "_serve_moderation",
    "_serve_moderation_filter_save", "_serve_moderation_review_resolve",
    "_serve_moderation_rule_draft", "_serve_moderation_rule_exemption",
    "_serve_moderation_rule_preview", "_serve_moderation_rule_publish",
    "_serve_moderation_rule_rollback", "_serve_moderation_rule_save",
    "_serve_moderation_work_assign", "_serve_oauth_callback",
    "_serve_onboarding", "_serve_onboarding_resource_delete", "_serve_onboarding_resource_save",
    "_serve_onboarding_update", "_serve_onboarding_verify", "_serve_operator_access_action",
    "_serve_operator_invitation",
    "_serve_public_home", "_serve_search", "_serve_search_export", "_serve_search_save",
    "_serve_settings", "_serve_settings_operator_invite", "_serve_settings_update",
    "_serve_signals", "_serve_system_health",
    "_serve_twitch_eventsub",
    "_serve_twitch_install_callback", "_serve_twitch_link", "_serve_user_lifecycle_export",
    "_serve_user_messages", "_serve_user_moderation_action", "_serve_users", "_serve_users_link",
    "_serve_users_unlink",
})

_PUBLIC_HANDLERS = {
    "_serve_login", "_serve_oauth_callback", "_serve_privacy_policy", "_serve_public_home",
    "_serve_system_health", "_serve_terms_of_service",
}
_INGEST_HANDLERS = {"_serve_api_event", "_serve_api_external_observation"}
_INTEGRATION_HANDLERS = {
    "_serve_api_integration_revoke", "_serve_discord_install_callback", "_serve_discord_link",
    "_serve_integrations", "_serve_twitch_install_callback", "_serve_twitch_link",
}
_OPERATOR_HANDLERS = {
    "_serve_operator_access_action", "_serve_operator_invitation",
    "_serve_settings_operator_invite",
}
_ANNOUNCEMENT_HANDLERS = {
    "_serve_announcement_approve", "_serve_announcement_cancel", "_serve_announcement_create",
    "_serve_announcement_retry", "_serve_announcements",
}
_RULE_HANDLERS = {
    "_serve_api_rule_save", "_serve_api_rules", "_serve_moderation_rule_draft",
    "_serve_moderation_rule_exemption", "_serve_moderation_rule_preview",
    "_serve_moderation_rule_publish", "_serve_moderation_rule_rollback",
    "_serve_moderation_rule_save",
}
_MODERATION_HANDLERS = {
    "_serve_api_review_resolve", "_serve_live_ops_moderate", "_serve_moderation_review_resolve",
    "_serve_moderation_work_assign", "_serve_user_moderation_action",
}
_MODERATION_QUEUE_HANDLERS = {
    "_serve_api_actions", "_serve_api_reviews", "_serve_moderation",
    "_serve_moderation_filter_save",
}
_EXPORT_HANDLERS = {
    "_serve_analytics_export", "_serve_api_case_export", "_serve_search_export",
    "_serve_user_lifecycle_export",
}
_AUDIT_HANDLERS = {"_serve_api_audit", "_serve_audit"}
_SETTINGS_HANDLERS = {
    "_serve_commands", "_serve_commands_update", "_serve_onboarding",
    "_serve_onboarding_resource_delete", "_serve_onboarding_resource_save",
    "_serve_onboarding_update", "_serve_onboarding_verify",
    "_serve_settings", "_serve_settings_update",
}
_LIVE_CONTROL_HANDLERS = {
    "_serve_live_ops_chat_settings", "_serve_live_ops_handoff", "_serve_live_ops_incident_action",
    "_serve_live_ops_notification_destination", "_serve_live_ops_playbook",
    "_serve_live_ops_shield_mode", "_serve_live_ops_shift_schedule",
}


def _dashboard_capability(handler: str) -> str:
    if handler in _PUBLIC_HANDLERS:
        return "public.access"
    if handler == "_serve_twitch_eventsub":
        return "events.ingest"
    if handler in _INGEST_HANDLERS:
        return "events.write"
    if handler in _INTEGRATION_HANDLERS:
        return "integrations.manage"
    if handler in _OPERATOR_HANDLERS:
        return "operators.manage"
    if handler in _ANNOUNCEMENT_HANDLERS:
        return "announcements.manage"
    if handler in _RULE_HANDLERS:
        return "rules.manage"
    if handler in _MODERATION_HANDLERS:
        return "moderation.manage"
    if handler == "_serve_api_bulk_moderation":
        return "moderation.bulk"
    if handler == "_serve_member_queue_resolve":
        return "appeals.manage"
    if handler in _MODERATION_QUEUE_HANDLERS:
        return "moderation.queues.read"
    if handler in _EXPORT_HANDLERS:
        return "exports.create"
    if handler in _AUDIT_HANDLERS:
        return "audit.read"
    if handler in _SETTINGS_HANDLERS:
        return "settings.manage"
    if handler in _LIVE_CONTROL_HANDLERS:
        return "live_ops.manage"
    if handler in {"_serve_live_ops", "_serve_api_live_ops", "_serve_live_ops_stream"}:
        return "live_ops.read"
    if "user" in handler:
        return "members.read"
    if "analytics" in handler or "signal" in handler or handler == "_serve_api_slo":
        return "analytics.read"
    if "case" in handler or "alert" in handler or "intelligence" in handler:
        return "cases.manage"
    return "dashboard.access"


def _dashboard_guard(handler: str) -> str:
    if handler in _PUBLIC_HANDLERS:
        return "public"
    if handler == "_serve_twitch_eventsub":
        return "webhook_signature"
    if handler in _INGEST_HANDLERS:
        return "api_client_or_session"
    if handler == "_serve_logout":
        return "optional_session"
    return "session"


DASHBOARD_SURFACE_POLICIES = {
    handler: SurfacePolicy(
        capability=_dashboard_capability(handler),
        guard=_dashboard_guard(handler),
        scope="global" if handler in _PUBLIC_HANDLERS else (
            "installation" if handler == "_serve_twitch_eventsub" else "community"
        ),
        kind="sse" if handler == "_serve_live_ops_stream" else (
            "export" if handler in _EXPORT_HANDLERS else "http"
        ),
    )
    for handler in _DASHBOARD_HANDLERS
}

NON_HTTP_SURFACE_POLICIES = {
    "command:addcom": SurfacePolicy("settings.manage", "command_operator", "community", "bot_command"),
    "command:editcom": SurfacePolicy("settings.manage", "command_operator", "community", "bot_command"),
    "command:delcom": SurfacePolicy("settings.manage", "command_operator", "community", "bot_command"),
    "command:alias": SurfacePolicy("settings.manage", "command_operator", "community", "bot_command"),
    "command:verify": SurfacePolicy("community.read", "platform_identity", "community", "bot_command"),
    "command:credit": SurfacePolicy("members.read", "installation_context", "community", "bot_command"),
    "command:custom": SurfacePolicy("dashboard.access", "installation_context", "community", "bot_command"),
    "job:maintenance": SurfacePolicy("system.maintenance", "system", "community", "job"),
    "job:twitch_live_announcements": SurfacePolicy("announcements.manage", "system", "installation", "job"),
    "job:scheduled_announcements": SurfacePolicy("announcements.manage", "system", "installation", "job"),
    "job:onboarding_roles": SurfacePolicy("members.manage", "system", "installation", "job"),
    "job:onboarding_checkpoints": SurfacePolicy("members.manage", "system", "community", "job"),
    "lookup:installation": SurfacePolicy("integrations.manage", "compound_lookup", "installation", "direct_lookup"),
    "lookup:member": SurfacePolicy("members.read", "compound_lookup", "community", "direct_lookup"),
    "lookup:moderation": SurfacePolicy("moderation.queues.read", "compound_lookup", "community", "direct_lookup"),
    "provider:moderation": SurfacePolicy("moderation.manage", "installation_capability", "installation", "provider_action"),
    "provider:announcement": SurfacePolicy("announcements.manage", "installation_capability", "installation", "provider_action"),
    "provider:live_control": SurfacePolicy("live_ops.manage", "installation_capability", "installation", "provider_action"),
}

SURFACE_POLICIES = {**DASHBOARD_SURFACE_POLICIES, **NON_HTTP_SURFACE_POLICIES}

INSTALLATION_CAPABILITY_BY_SURFACE = {
    "provider:moderation": "moderation_actions",
    "provider:announcement": "announcements",
    "provider:live_control": "live_controls",
}


def require_non_http_surface(surface: str, *, guard: str) -> SurfacePolicy:
    policy = NON_HTTP_SURFACE_POLICIES.get(surface)
    if policy is None:
        raise ValueError(f"unmapped non-HTTP surface: {surface}")
    if policy.guard != guard:
        raise PermissionError(f"surface {surface} requires {policy.guard}")
    return policy
