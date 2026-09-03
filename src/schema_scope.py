from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeRule:
    scope: str
    owner_column: str | None = None
    owner_table: str | None = None


GLOBAL_TABLES = frozenset({
    "audit_log", "cohort_anomalies", "cohort_baselines", "command_definitions",
    "derived_signal_history", "derived_signal_windows", "derived_signals",
    "evaluation_labels", "external_feed_sources", "graph_metric_history", "graph_metrics",
    "identity_link_suggestions", "metrics_rollups", "model_evaluation_runs", "model_registry",
    "observation_fts", "observation_fts_config", "observation_fts_content",
    "observation_fts_data", "observation_fts_docsize", "observation_fts_idx",
    "operational_metrics", "operator_accounts", "operator_discord_guild_permissions",
    "organizations", "platform_accounts", "raid_playbooks", "reputation_events",
    "saved_queries", "schema_migrations", "server_boost_requests", "service_reliability_buckets",
    "signal_calculation_runs", "simple_command_definitions", "social_score_components",
    "social_score_runs", "stream_states", "threshold_backtests", "twitch_live_announcements",
    "users",
})

ORGANIZATION_TABLES = {
    "api_clients": ("community_id", "communities"),
    "api_request_usage": ("api_client_id", "api_clients"),
    "communities": ("workspace_id", "workspaces"),
    "workspaces": ("organization_id", "organizations"),
}

COMMUNITY_TABLES = {
    "audience_edges": ("community_id", "communities"),
    "data_subject_requests": ("community_id", "communities"),
    "case_activity": ("case_id", "investigation_cases"),
    "case_entities": ("case_id", "investigation_cases"),
    "case_evidence": ("case_id", "investigation_cases"),
    "command_analysis_results": ("observation_id", "observations"),
    "community_announcements": ("community_id", "communities"),
    "community_cohort_anomalies": ("community_id", "communities"),
    "community_cohort_baselines": ("community_id", "communities"),
    "community_derived_signal_history": ("community_id", "communities"),
    "community_derived_signal_windows": ("community_id", "communities"),
    "community_graph_metric_history": ("community_id", "communities"),
    "community_graph_metrics": ("community_id", "communities"),
    "community_identity_link_suggestions": ("community_id", "communities"),
    "community_installations": ("community_id", "communities"),
    "community_intelligence_profiles": ("community_id", "communities"),
    "community_memberships": ("community_id", "communities"),
    "community_onboarding_resources": ("community_id", "communities"),
    "community_policy_settings": ("community_id", "communities"),
    "community_signal_calculation_runs": ("community_id", "communities"),
    "content_analysis": ("observation_id", "observations"),
    "content_entities": ("observation_id", "observations"),
    "coordination_campaign_members": ("campaign_id", "coordination_campaigns"),
    "coordination_campaigns": ("community_id", "communities"),
    "dead_letter_events": ("community_id", "communities"),
    "derived_signal_evidence": ("observation_id", "observations"),
    "discord_install_intents": ("community_id", "communities"),
    "emerging_topics": ("community_id", "communities"),
    "entity_relationships": ("community_id", "communities"),
    "incident_activity": ("incident_id", "operations_incidents"),
    "incident_alerts": ("incident_id", "operations_incidents"),
    "intelligence_alerts": ("community_id", "communities"),
    "intelligence_reports": ("community_id", "communities"),
    "intelligence_sharing_agreements": ("source_community_id", "communities"),
    "investigation_cases": ("community_id", "communities"),
    "legal_holds": ("community_id", "communities"),
	"member_appeals": ("community_id", "communities"),
	"member_reports": ("community_id", "communities"),
    "moderation_saved_filters": ("community_id", "communities"),
    "tenant_slo_samples": ("community_id", "communities"),
    "tenant_job_schedule": ("community_id", "communities"),
    "tenant_quota_policies": ("community_id", "communities"),
    "tenant_quota_usage": ("community_id", "communities"),
    "message_attachments": ("message_id", "messages"),
    "messages": ("community_id", "communities"),
    "moderation_actions": ("community_id", "communities"),
    "moderation_rules": ("community_id", "communities"),
    "moderation_rule_exemptions": ("community_id", "communities"),
    "moderation_rule_versions": ("community_id", "communities"),
    "moderation_shift_schedules": ("community_id", "communities"),
    "moderation_shifts": ("community_id", "communities"),
    "notification_deliveries": ("incident_id", "operations_incidents"),
    "notification_destinations": ("community_id", "communities"),
    "observations": ("community_id", "communities"),
    "operations_incidents": ("community_id", "communities"),
    "operator_community_roles": ("community_id", "communities"),
    "operator_invitations": ("community_id", "communities"),
    "operator_permission_overrides": ("community_id", "communities"),
    "pilot_invitations": ("community_id", "communities"),
    "post_stream_briefings": ("community_id", "communities"),
    "processing_jobs": ("observation_id", "observations"),
    "raid_playbook_runs": ("community_id", "communities"),
    "raw_event_archive": ("community_id", "communities"),
    "relationship_evidence": ("relationship_id", "entity_relationships"),
    "replay_requests": ("community_id", "communities"),
    "review_queue": ("message_id", "messages"),
    "rule_matches": ("message_id", "messages"),
    "stream_cohort_snapshots": ("stream_session_id", "stream_sessions"),
    "stream_sessions": ("community_id", "communities"),
    "topic_evidence": ("community_id", "communities"),
    "topic_history": ("community_id", "communities"),
    "twitch_channels": ("request_source_message_id", "messages"),
    "twitch_control_actions": ("community_id", "communities"),
    "twitch_eventsub_subscriptions": ("community_id", "communities"),
    "twitch_install_intents": ("community_id", "communities"),
    "user_notes": ("community_id", "communities"),
    "welcome_events": ("message_id", "messages"),
}

INSTALLATION_TABLES = {
    "community_announcement_deliveries": ("installation_id", "community_installations"),
    "community_onboarding_members": ("discord_installation_id", "community_installations"),
    "community_onboarding_settings": ("discord_installation_id", "community_installations"),
    "discord_channels": ("guild_id", None),
    "installation_health_events": ("installation_id", "community_installations"),
    "installation_credentials": ("installation_id", "community_installations"),
}


SCHEMA_SCOPE_INVENTORY = {
    **{table: ScopeRule("global") for table in GLOBAL_TABLES},
    **{
        table: ScopeRule("organization", owner_column, owner_table)
        for table, (owner_column, owner_table) in ORGANIZATION_TABLES.items()
    },
    **{
        table: ScopeRule("community", owner_column, owner_table)
        for table, (owner_column, owner_table) in COMMUNITY_TABLES.items()
    },
    **{
        table: ScopeRule("installation", owner_column, owner_table)
        for table, (owner_column, owner_table) in INSTALLATION_TABLES.items()
    },
}