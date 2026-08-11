from __future__ import annotations

import re
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping

from .intelligence.powerusers import (
    POWERUSER_THRESHOLD,
    default_social_score_for_name,
    record_reputation_evidence,
    score_delta_for_message,
    score_delta_for_moderation,
)
from .models import IngestionResult, NormalizedMessage, Observation, ObservationResult, CollectedObservation
from .moderation import ModerationFinding, ModerationRule, evaluate_egregious_content, evaluate_message_moderation

BUILTIN_EGREGIOUS_RULE_NAME = "builtin:egregious_content"
BUILTIN_STREAMBOO_RULE_NAME = "builtin:streamboo_viewer_spam"
RESERVED_COMMAND_NAMES = {"addcom", "delcom", "editcom", "alias"}
WELCOME_DUPLICATE_WINDOW_MINUTES = 10
WELCOME_BONUS_DELTA = 1
WELCOME_DUPLICATE_PENALTY_DELTA = -3
_WELCOME_TARGET_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
_WELCOME_TARGET_HANDLE_PATTERN = re.compile(r"@([a-zA-Z0-9_]{2,64})")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    primary_display_name TEXT NOT NULL,
    current_reputation_score INTEGER NOT NULL DEFAULT 500,
    candidate_flag INTEGER NOT NULL DEFAULT 0,
    score_confidence REAL NOT NULL DEFAULT 0.0,
    score_model_version INTEGER,
    score_calculated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_accounts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    guild_or_channel_context TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    detached_from_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, platform_user_id)
);

CREATE TABLE IF NOT EXISTS operator_accounts (
    id INTEGER PRIMARY KEY,
    discord_user_id TEXT NOT NULL UNIQUE,
    discord_username TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    platform_message_id TEXT,
    platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    channel_id TEXT NOT NULL,
    content_raw TEXT NOT NULL,
    content_normalized TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    edited_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, platform_message_id)
);

CREATE TABLE IF NOT EXISTS message_attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    attachment_index INTEGER NOT NULL,
    attachment_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS welcome_events (
    id INTEGER PRIMARY KEY,
    sender_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
    target_identifier TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twitch_channels (
    id INTEGER PRIMARY KEY,
    channel_name TEXT NOT NULL UNIQUE,
    requested_by_platform_account_id INTEGER REFERENCES platform_accounts(id) ON DELETE SET NULL,
    request_source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    join_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS moderation_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    severity TEXT NOT NULL,
    auto_enforce_action TEXT,
    enforcement_mode TEXT NOT NULL DEFAULT 'enforce',
    action_duration_seconds INTEGER NOT NULL DEFAULT 600,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rule_matches (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    moderation_rule_id INTEGER NOT NULL REFERENCES moderation_rules(id),
    severity TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    confidence REAL,
    recommended_action TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    target_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id INTEGER,
    reason TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 600,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS reputation_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    delta INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_notes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operator_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'open',
    severity TEXT NOT NULL,
    queue_reason_code TEXT NOT NULL,
    assigned_operator_id INTEGER,
    resolution TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    resolved_by_operator_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS server_boost_requests (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    requester_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requester_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
    command_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fulfilled_at TEXT
);

CREATE TABLE IF NOT EXISTS command_definitions (
    command_name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description_template TEXT NOT NULL,
    footer_template TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS simple_command_definitions (
    command_name TEXT PRIMARY KEY,
    response_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discord_channels (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    channel_type INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twitch_live_announcements (
    id INTEGER PRIMARY KEY,
    twitch_channel_name TEXT NOT NULL,
    twitch_stream_id TEXT NOT NULL,
    discord_guild_id TEXT NOT NULL,
    discord_channel_id TEXT NOT NULL,
    announced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(twitch_channel_name, twitch_stream_id, discord_guild_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id INTEGER,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics_rollups (
    id INTEGER PRIMARY KEY,
    metric_name TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    bucket_size TEXT NOT NULL,
    dimension_json TEXT NOT NULL DEFAULT '{}',
    value REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(metric_name, bucket_start, bucket_size, dimension_json)
);

CREATE TABLE IF NOT EXISTS service_reliability_buckets (
    service_name TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    is_up INTEGER NOT NULL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(service_name, bucket_start)
);

CREATE INDEX IF NOT EXISTS idx_platform_accounts_user_id
    ON platform_accounts(user_id);

CREATE INDEX IF NOT EXISTS idx_messages_sent_at
    ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_platform_account
    ON messages(platform_account_id);
CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id
    ON message_attachments(message_id, attachment_index);
CREATE INDEX IF NOT EXISTS idx_welcome_events_sender_target_created_at
    ON welcome_events(sender_platform_account_id, target_identifier, created_at);
CREATE INDEX IF NOT EXISTS idx_twitch_channels_status
    ON twitch_channels(status, channel_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_rules_name
    ON moderation_rules(name);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_created_at
    ON moderation_actions(created_at);
CREATE INDEX IF NOT EXISTS idx_reputation_events_user_id
    ON reputation_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_queue_status_created_at
    ON review_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_server_boost_requests_lookup
    ON server_boost_requests(platform, channel_id, command_name, status, requested_at);
CREATE INDEX IF NOT EXISTS idx_server_boost_requests_expires_at
    ON server_boost_requests(expires_at);
CREATE INDEX IF NOT EXISTS idx_command_definitions_enabled
    ON command_definitions(enabled, command_name);
CREATE INDEX IF NOT EXISTS idx_simple_command_definitions_enabled
    ON simple_command_definitions(enabled, command_name);
CREATE INDEX IF NOT EXISTS idx_discord_channels_guild_name
    ON discord_channels(guild_id, channel_name);
CREATE INDEX IF NOT EXISTS idx_twitch_live_announcements_lookup
    ON twitch_live_announcements(twitch_channel_name, twitch_stream_id, discord_guild_id);
CREATE INDEX IF NOT EXISTS idx_service_reliability_buckets_lookup
    ON service_reliability_buckets(service_name, bucket_start);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    event_type TEXT NOT NULL,
    external_event_id TEXT,
    actor_platform_account_id INTEGER
        REFERENCES platform_accounts(id) ON DELETE SET NULL,
    target_platform_account_id INTEGER
        REFERENCES platform_accounts(id) ON DELETE SET NULL,
    container_id TEXT,
    context_id TEXT,
    text_raw TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    schema_version INTEGER NOT NULL DEFAULT 1,

    UNIQUE(platform, event_type, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_observations_occurred_at
    ON observations(occurred_at);

CREATE INDEX IF NOT EXISTS idx_observations_platform_type_time
    ON observations(platform, event_type, occurred_at);

CREATE INDEX IF NOT EXISTS idx_observations_actor_time
    ON observations(actor_platform_account_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_observations_context_time
    ON observations(context_id, occurred_at);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,
    job_type TEXT NOT NULL,
    observation_id INTEGER REFERENCES observations(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL DEFAULT '{}',

    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,

    available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at TEXT,
    claimed_by TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    last_error TEXT,

    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK(stage IN ('analysis', 'action')),
    CHECK(status IN (
        'pending',
        'running',
        'completed',
        'retry',
        'failed',
        'cancelled'
    ))
);

CREATE INDEX IF NOT EXISTS idx_processing_jobs_available
    ON processing_jobs(stage, status, available_at, priority);

CREATE INDEX IF NOT EXISTS idx_processing_jobs_observation
    ON processing_jobs(observation_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_observation_id
    ON messages(observation_id)
    WHERE observation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS command_analysis_results (
    observation_id INTEGER NOT NULL
        REFERENCES observations(id) ON DELETE CASCADE,
    analyzer_version INTEGER NOT NULL,
    command_name TEXT,
    matched INTEGER NOT NULL,
    rendered_payload_json TEXT,
    analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (observation_id, analyzer_version),

    CHECK (matched IN (0, 1))
);

CREATE TABLE IF NOT EXISTS derived_signals (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_key TEXT NOT NULL,
    analyzer_version INTEGER NOT NULL,
    value_real REAL NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    window_start TEXT,
    window_end TEXT,
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, signal_key, analyzer_version),
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(evidence_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_derived_signals_user
    ON derived_signals(user_id, signal_key);

CREATE INDEX IF NOT EXISTS idx_derived_signals_key_value
    ON derived_signals(signal_key, value_real DESC);

CREATE TABLE IF NOT EXISTS signal_calculation_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger_observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    analyzer_version INTEGER NOT NULL,
    calculated_at TEXT NOT NULL,
    UNIQUE(user_id, trigger_observation_id, analyzer_version)
);

CREATE TABLE IF NOT EXISTS derived_signal_windows (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_key TEXT NOT NULL,
    window_name TEXT NOT NULL,
    analyzer_version INTEGER NOT NULL,
    value_real REAL NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    window_start TEXT,
    window_end TEXT,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, signal_key, window_name, analyzer_version)
);

CREATE TABLE IF NOT EXISTS derived_signal_history (
    id INTEGER PRIMARY KEY,
    calculation_run_id INTEGER NOT NULL REFERENCES signal_calculation_runs(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_key TEXT NOT NULL,
    window_name TEXT NOT NULL,
    analyzer_version INTEGER NOT NULL,
    value_real REAL NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    window_start TEXT,
    window_end TEXT,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_signal_evidence (
    signal_history_id INTEGER NOT NULL REFERENCES derived_signal_history(id) ON DELETE CASCADE,
    observation_id INTEGER REFERENCES observations(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    contribution TEXT NOT NULL DEFAULT 'supporting',
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY(signal_history_id, observation_id, message_id)
);

CREATE TABLE IF NOT EXISTS intelligence_alerts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    signal_history_id INTEGER REFERENCES derived_signal_history(id) ON DELETE SET NULL,
    observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    disposition TEXT,
    assigned_operator_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    acknowledged_at TEXT,
    suppressed_until TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS investigation_cases (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    owner_operator_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS case_entities (
    case_id INTEGER NOT NULL REFERENCES investigation_cases(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'subject',
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(case_id, user_id)
);

CREATE TABLE IF NOT EXISTS case_evidence (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES investigation_cases(id) ON DELETE CASCADE,
    observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    signal_history_id INTEGER REFERENCES derived_signal_history(id) ON DELETE SET NULL,
    alert_id INTEGER REFERENCES intelligence_alerts(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS case_activity (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES investigation_cases(id) ON DELETE CASCADE,
    operator_id INTEGER,
    activity_type TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_relationships (
    id INTEGER PRIMARY KEY,
    source_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    context_key TEXT NOT NULL DEFAULT '',
    strength REAL NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_user_id, target_user_id, relationship_type, context_key)
);

CREATE TABLE IF NOT EXISTS relationship_evidence (
    relationship_id INTEGER NOT NULL REFERENCES entity_relationships(id) ON DELETE CASCADE,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(relationship_id, observation_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_evidence_time
    ON relationship_evidence(relationship_id, occurred_at);

CREATE TABLE IF NOT EXISTS stream_states (
    platform TEXT NOT NULL,
    stream_key TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    category TEXT,
    started_at TEXT,
    ended_at TEXT,
    latest_observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(platform, stream_key)
);

CREATE TABLE IF NOT EXISTS intelligence_reports (
    id INTEGER PRIMARY KEY,
    report_type TEXT NOT NULL,
    subject_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    generator_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS social_score_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger_signal_run_id INTEGER REFERENCES signal_calculation_runs(id) ON DELETE SET NULL,
    model_version INTEGER NOT NULL,
    score INTEGER NOT NULL,
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    band TEXT NOT NULL,
    explanation_json TEXT NOT NULL DEFAULT '{}',
    calculated_at TEXT NOT NULL,
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(evidence_count >= 0)
);

CREATE TABLE IF NOT EXISTS social_score_components (
    id INTEGER PRIMARY KEY,
    score_run_id INTEGER NOT NULL REFERENCES social_score_runs(id) ON DELETE CASCADE,
    component_key TEXT NOT NULL,
    label TEXT NOT NULL,
    raw_value REAL NOT NULL,
    normalized_value REAL NOT NULL,
    weight REAL NOT NULL,
    contribution REAL NOT NULL,
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_signal_history_user_time ON derived_signal_history(user_id, calculated_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_windows_key_value ON derived_signal_windows(signal_key, window_name, value_real DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evidence_observation ON derived_signal_evidence(observation_id, message_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_alerts_status ON intelligence_alerts(status, severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_status ON investigation_cases(status, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_relationships_source ON entity_relationships(source_user_id, strength DESC);
CREATE INDEX IF NOT EXISTS idx_reports_generated ON intelligence_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_score_runs_user_time ON social_score_runs(user_id, calculated_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_score_components_run ON social_score_components(score_run_id, ABS(contribution) DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS observation_fts USING fts5(
    text,
    platform UNINDEXED,
    event_type UNINDEXED,
    container_id UNINDEXED,
    context_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS observations_fts_insert
AFTER INSERT ON observations BEGIN
    INSERT INTO observation_fts (
        rowid, text, platform, event_type, container_id, context_id
    ) VALUES (
        new.id, COALESCE(new.text_raw, ''), new.platform, new.event_type,
        COALESCE(new.container_id, ''), COALESCE(new.context_id, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS observations_fts_update
AFTER UPDATE OF text_raw, platform, event_type, container_id, context_id ON observations BEGIN
    DELETE FROM observation_fts WHERE rowid = old.id;
    INSERT INTO observation_fts (
        rowid, text, platform, event_type, container_id, context_id
    ) VALUES (
        new.id, COALESCE(new.text_raw, ''), new.platform, new.event_type,
        COALESCE(new.container_id, ''), COALESCE(new.context_id, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS observations_fts_delete
AFTER DELETE ON observations BEGIN
    DELETE FROM observation_fts WHERE rowid = old.id;
END;

CREATE TABLE IF NOT EXISTS saved_queries (
    id INTEGER PRIMARY KEY,
    operator_id INTEGER,
    name TEXT NOT NULL,
    query_text TEXT NOT NULL DEFAULT '',
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(operator_id, name)
);

CREATE TABLE IF NOT EXISTS content_analysis (
    observation_id INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    analyzer_version INTEGER NOT NULL,
    language_code TEXT NOT NULL,
    language_confidence REAL NOT NULL,
    sentiment_label TEXT NOT NULL,
    sentiment_score REAL NOT NULL,
    intent_label TEXT NOT NULL,
    intent_confidence REAL NOT NULL,
    threat_level TEXT NOT NULL,
    threat_score REAL NOT NULL,
    conversation_json TEXT NOT NULL DEFAULT '{}',
    indicators_json TEXT NOT NULL DEFAULT '[]',
    analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_entities (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    confidence REAL NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    UNIQUE(observation_id, entity_type, normalized_value, start_offset)
);

CREATE INDEX IF NOT EXISTS idx_content_entities_value
    ON content_entities(entity_type, normalized_value, observation_id);

CREATE TABLE IF NOT EXISTS external_feed_sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    trust_weight REAL NOT NULL DEFAULT 0.5,
    enabled INTEGER NOT NULL DEFAULT 1,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    last_observed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS emerging_topics (
    id INTEGER PRIMARY KEY,
    topic_key TEXT NOT NULL UNIQUE,
    topic_kind TEXT NOT NULL,
    label TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    baseline_rate REAL NOT NULL,
    velocity REAL NOT NULL,
    context_count INTEGER NOT NULL,
    community_count INTEGER NOT NULL,
    unusualness REAL NOT NULL,
    first_observed_at TEXT,
    last_observed_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_history (
    id INTEGER PRIMARY KEY,
    topic_key TEXT NOT NULL,
    topic_kind TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    baseline_rate REAL NOT NULL,
    velocity REAL NOT NULL,
    context_count INTEGER NOT NULL,
    community_count INTEGER NOT NULL,
    unusualness REAL NOT NULL,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_evidence (
    topic_key TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    context_key TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(topic_key, observation_id)
);

CREATE INDEX IF NOT EXISTS idx_topics_velocity
    ON emerging_topics(velocity DESC, unusualness DESC);
CREATE INDEX IF NOT EXISTS idx_topic_history_time
    ON topic_history(topic_key, calculated_at DESC);

CREATE TABLE IF NOT EXISTS graph_metrics (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    in_degree REAL NOT NULL,
    out_degree REAL NOT NULL,
    weighted_degree REAL NOT NULL,
    betweenness REAL NOT NULL,
    pagerank REAL NOT NULL,
    cluster_id INTEGER,
    is_bridge INTEGER NOT NULL DEFAULT 0,
    influence_score REAL NOT NULL,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_metric_history (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    in_degree REAL NOT NULL,
    out_degree REAL NOT NULL,
    weighted_degree REAL NOT NULL,
    betweenness REAL NOT NULL,
    pagerank REAL NOT NULL,
    cluster_id INTEGER,
    is_bridge INTEGER NOT NULL DEFAULT 0,
    influence_score REAL NOT NULL,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_link_suggestions (
    id INTEGER PRIMARY KEY,
    left_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
    right_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    model_version INTEGER NOT NULL,
    reviewed_by_operator_id INTEGER,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(left_platform_account_id, right_platform_account_id, model_version),
    CHECK(left_platform_account_id < right_platform_account_id),
    CHECK(status IN ('pending', 'approved', 'rejected', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_identity_suggestions_status
    ON identity_link_suggestions(status, confidence DESC);

CREATE TABLE IF NOT EXISTS cohort_baselines (
    id INTEGER PRIMARY KEY,
    cohort_type TEXT NOT NULL,
    cohort_key TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    mean_value REAL NOT NULL,
    stddev_value REAL NOT NULL,
    median_value REAL NOT NULL,
    p90_value REAL NOT NULL,
    calculated_at TEXT NOT NULL,
    UNIQUE(cohort_type, cohort_key, signal_key)
);

CREATE TABLE IF NOT EXISTS cohort_anomalies (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cohort_type TEXT NOT NULL,
    cohort_key TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    observed_value REAL NOT NULL,
    baseline_mean REAL NOT NULL,
    z_score REAL NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    calculated_at TEXT NOT NULL,
    UNIQUE(user_id, cohort_type, cohort_key, signal_key)
);

CREATE INDEX IF NOT EXISTS idx_cohort_anomalies_score
    ON cohort_anomalies(ABS(z_score) DESC, confidence DESC);

CREATE TABLE IF NOT EXISTS evaluation_labels (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    alert_id INTEGER REFERENCES intelligence_alerts(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    label_key TEXT NOT NULL,
    label_value TEXT NOT NULL,
    score_key TEXT,
    score_value REAL,
    model_version INTEGER,
    operator_id INTEGER,
    source TEXT NOT NULL DEFAULT 'operator',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_evaluation_runs (
    id INTEGER PRIMARY KEY,
    model_key TEXT NOT NULL,
    model_version INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    score_distribution_json TEXT NOT NULL,
    calculated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threshold_backtests (
    id INTEGER PRIMARY KEY,
    evaluation_run_id INTEGER NOT NULL REFERENCES model_evaluation_runs(id) ON DELETE CASCADE,
    threshold REAL NOT NULL,
    true_positive INTEGER NOT NULL,
    false_positive INTEGER NOT NULL,
    true_negative INTEGER NOT NULL,
    false_negative INTEGER NOT NULL,
    precision REAL NOT NULL,
    recall REAL NOT NULL,
    false_positive_rate REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_model_time
    ON model_evaluation_runs(model_key, calculated_at DESC);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operational_metrics (
    metric_name TEXT NOT NULL,
    dimension_key TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(metric_name, dimension_key, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_operational_metrics_time
    ON operational_metrics(metric_name, observed_at DESC);
"""

DEFAULT_COMMAND_DEFINITIONS = (
    {
        "command_name": "credit",
        "title": "Social Credit Profile",
        "description_template": "Profile for {display_name}",
        "footer_template": "{platform} user: {author_username}",
        "enabled": 1,
    },
)


def connect_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT OR IGNORE INTO observation_fts(
                rowid, text, platform, event_type, container_id, context_id
            )
            SELECT id, COALESCE(text_raw, ''), platform, event_type,
                   COALESCE(container_id, ''), COALESCE(context_id, '')
            FROM observations
            """
        )
        _migrate_schema(connection)
        _seed_default_command_definitions(connection)
        _seed_builtin_moderation_rules(connection)
        _backfill_social_scores(connection)


def _migrate_schema(connection: sqlite3.Connection) -> None:
    processing_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(processing_jobs)").fetchall()
    }
    if "lease_expires_at" not in processing_columns:
        connection.execute("ALTER TABLE processing_jobs ADD COLUMN lease_expires_at TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_processing_jobs_lease ON processing_jobs(status, lease_expires_at)"
    )
    user_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(users)").fetchall()}
    if "score_confidence" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN score_confidence REAL NOT NULL DEFAULT 0.0")
    if "score_model_version" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN score_model_version INTEGER")
    if "score_calculated_at" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN score_calculated_at TEXT")

    platform_account_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(platform_accounts)").fetchall()
    }
    if "detached_from_user_id" not in platform_account_columns:
        connection.execute(
            "ALTER TABLE platform_accounts ADD COLUMN detached_from_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
        )

    message_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "user_id" not in message_columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
        )

    moderation_action_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(moderation_actions)").fetchall()
    }
    if "user_id" not in moderation_action_columns:
        connection.execute(
            "ALTER TABLE moderation_actions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
        )

    migration_columns: tuple[tuple[str, str, str], ...] = (
        ("messages", "edited_at", "TEXT"),
        ("messages", "deleted_at", "TEXT"),
        ("moderation_rules", "enforcement_mode", "TEXT NOT NULL DEFAULT 'enforce'"),
        ("moderation_rules", "action_duration_seconds", "INTEGER NOT NULL DEFAULT 600"),
        ("moderation_actions", "duration_seconds", "INTEGER NOT NULL DEFAULT 600"),
        ("moderation_actions", "error_message", "TEXT"),
        ("moderation_actions", "completed_at", "TEXT"),
        ("intelligence_alerts", "observation_id", "INTEGER REFERENCES observations(id) ON DELETE SET NULL"),
        ("intelligence_alerts", "acknowledged_at", "TEXT"),
        ("intelligence_alerts", "suppressed_until", "TEXT"),
        ("evaluation_labels", "score_key", "TEXT"),
        ("evaluation_labels", "score_value", "REAL"),
        ("evaluation_labels", "model_version", "INTEGER"),
        ("review_queue", "resolution", "TEXT"),
        ("review_queue", "resolution_note", "TEXT NOT NULL DEFAULT ''"),
        ("review_queue", "resolved_by_operator_id", "INTEGER"),
    )
    for table_name, column_name, definition in migration_columns:
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )

    migrations = (
        (1, "processing job leases"),
        (2, "canonical identity attribution"),
        (3, "intelligence platform P0-P3"),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
        migrations,
    )

    connection.execute(
        """
        UPDATE platform_accounts AS account
        SET detached_from_user_id = (
            SELECT CAST(json_extract(link_event.payload_json, '$.user_id') AS INTEGER)
            FROM audit_log AS link_event
            WHERE link_event.entity_type = 'platform_account'
              AND link_event.entity_id = account.id
              AND link_event.action_type = 'user_account_link'
            ORDER BY link_event.id DESC
            LIMIT 1
        )
        WHERE account.user_id IS NULL
          AND account.detached_from_user_id IS NULL
          AND (
              SELECT recent_event.action_type
              FROM audit_log AS recent_event
              WHERE recent_event.entity_type = 'platform_account'
                AND recent_event.entity_id = account.id
                AND recent_event.action_type IN ('user_account_link', 'user_account_unlink')
              ORDER BY recent_event.id DESC
              LIMIT 1
          ) = 'user_account_unlink'
        """
    )
    connection.execute(
        """
        UPDATE messages AS message
        SET user_id = COALESCE(
            (
                SELECT account.user_id
                FROM platform_accounts AS account
                WHERE account.id = message.platform_account_id
            ),
            (
                SELECT event.user_id
                FROM reputation_events AS event
                WHERE event.source_id = message.id
                  AND event.source_type IN ('message', 'moderation')
                ORDER BY event.id DESC
                LIMIT 1
            ),
            (
                SELECT account.detached_from_user_id
                FROM platform_accounts AS account
                WHERE account.id = message.platform_account_id
            )
        )
        WHERE message.user_id IS NULL
        """
    )
    connection.execute(
        """
        UPDATE moderation_actions AS action
        SET user_id = COALESCE(
            (SELECT message.user_id FROM messages AS message WHERE message.id = action.message_id),
            (
                SELECT COALESCE(account.user_id, account.detached_from_user_id)
                FROM platform_accounts AS account
                WHERE account.id = action.target_platform_account_id
            )
        )
        WHERE action.user_id IS NULL
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, sent_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_moderation_actions_user_id ON moderation_actions(user_id, created_at)"
    )
    _cleanup_legacy_merged_users(connection)


def _cleanup_legacy_merged_users(connection: sqlite3.Connection) -> None:
    from .intelligence.userprofiles import _merge_canonical_users

    merge_candidates = connection.execute(
        """
        SELECT events.user_id, MIN(messages.user_id)
        FROM reputation_events AS events
        INNER JOIN messages ON messages.id = events.source_id
        WHERE events.source_type IN ('message', 'moderation')
          AND messages.user_id IS NOT NULL
          AND messages.user_id != events.user_id
          AND NOT EXISTS (
              SELECT 1 FROM platform_accounts WHERE platform_accounts.user_id = events.user_id
          )
        GROUP BY events.user_id
        HAVING COUNT(DISTINCT messages.user_id) = 1
        """
    ).fetchall()
    for source_user_id_raw, target_user_id_raw in merge_candidates:
        source_user_id = int(source_user_id_raw)
        target_user_id = int(target_user_id_raw)
        if source_user_id == target_user_id:
            continue
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (source_user_id,)).fetchone() is None:
            continue
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (target_user_id,)).fetchone() is None:
            continue
        _merge_canonical_users(
            connection,
            source_user_id=source_user_id,
            target_user_id=target_user_id,
        )
        connection.execute(
            "UPDATE users SET score_model_version = NULL WHERE id = ?",
            (target_user_id,),
        )

    connection.execute(
        """
        DELETE FROM users
        WHERE NOT EXISTS (SELECT 1 FROM platform_accounts WHERE platform_accounts.user_id = users.id)
          AND NOT EXISTS (SELECT 1 FROM messages WHERE messages.user_id = users.id)
          AND NOT EXISTS (SELECT 1 FROM user_notes WHERE user_notes.user_id = users.id)
          AND NOT EXISTS (SELECT 1 FROM server_boost_requests WHERE server_boost_requests.requester_user_id = users.id)
          AND NOT EXISTS (SELECT 1 FROM intelligence_alerts WHERE intelligence_alerts.user_id = users.id)
          AND NOT EXISTS (SELECT 1 FROM case_entities WHERE case_entities.user_id = users.id)
          AND NOT EXISTS (
              SELECT 1 FROM entity_relationships
              WHERE entity_relationships.source_user_id = users.id OR entity_relationships.target_user_id = users.id
          )
          AND NOT EXISTS (SELECT 1 FROM intelligence_reports WHERE intelligence_reports.subject_user_id = users.id)
          AND NOT EXISTS (
              SELECT 1 FROM reputation_events
              WHERE reputation_events.user_id = users.id
                AND reputation_events.source_type != 'initial_calibration'
          )
          AND EXISTS (
              SELECT 1 FROM audit_log
              WHERE audit_log.action_type = 'auto_user_create'
                AND audit_log.entity_type = 'user'
                AND audit_log.entity_id = users.id
          )
        """
    )


def _seed_builtin_moderation_rules(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO moderation_rules (name, rule_type, pattern, severity, auto_enforce_action, enabled)
        VALUES (?, 'egregious_term', '*', 'high', 'timeout', 1)
        ON CONFLICT(name) DO NOTHING
        """,
        (BUILTIN_EGREGIOUS_RULE_NAME,),
    )
    connection.execute(
        """
        INSERT INTO moderation_rules (name, rule_type, pattern, severity, auto_enforce_action, enabled)
        VALUES (?, 'streamboo_viewer_spam', 'streamboo + solicitation', 'high', 'timeout', 1)
        ON CONFLICT(name) DO NOTHING
        """,
        (BUILTIN_STREAMBOO_RULE_NAME,),
    )


def _backfill_social_scores(connection: sqlite3.Connection) -> None:
    from .intelligence.scoring import SOCIAL_SCORE_MODEL_VERSION, calculate_social_score
    from .intelligence.signals import refresh_user_derived_signals

    user_ids = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT id FROM users
            WHERE score_model_version IS NULL OR score_model_version != ?
            ORDER BY id
            """,
            (SOCIAL_SCORE_MODEL_VERSION,),
        ).fetchall()
    ]
    for user_id in user_ids:
        refresh_user_derived_signals(connection, user_id)
        calculate_social_score(connection, user_id)


def _seed_default_command_definitions(connection: sqlite3.Connection) -> None:
    for definition in DEFAULT_COMMAND_DEFINITIONS:
        connection.execute(
            """
            INSERT INTO command_definitions (
                command_name,
                title,
                description_template,
                footer_template,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_name) DO NOTHING
            """,
            (
                definition["command_name"],
                definition["title"],
                definition["description_template"],
                definition["footer_template"],
                definition["enabled"],
            ),
        )


def list_simple_command_definitions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT command_name, response_template, enabled, created_at, updated_at
        FROM simple_command_definitions
        WHERE command_name NOT IN ('addcom', 'delcom', 'editcom', 'alias')
        ORDER BY command_name
        """
    ).fetchall()
    return list(rows)


def get_simple_command_definition(connection: sqlite3.Connection, command_name: str) -> sqlite3.Row | None:
    command_key = command_name.strip().casefold()
    if command_key in RESERVED_COMMAND_NAMES:
        return None
    return connection.execute(
        """
        SELECT command_name, response_template, enabled, created_at, updated_at
        FROM simple_command_definitions
        WHERE command_name = ?
        """,
        (command_key,),
    ).fetchone()


def upsert_simple_command_definition(
    connection: sqlite3.Connection,
    *,
    command_name: str,
    response_template: str,
    enabled: bool,
) -> None:
    command_key = command_name.strip().casefold()
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    if not response_template.strip():
        raise ValueError("response_template must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO simple_command_definitions (
                command_name,
                response_template,
                enabled
            ) VALUES (?, ?, ?)
            ON CONFLICT(command_name)
            DO UPDATE SET
                response_template = excluded.response_template,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                command_key,
                response_template.strip(),
                int(enabled),
            ),
        )


def delete_simple_command_definition(connection: sqlite3.Connection, command_name: str) -> bool:
    command_key = command_name.strip().casefold().lstrip("!")
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    with connection:
        result = connection.execute(
            "DELETE FROM simple_command_definitions WHERE command_name = ?",
            (command_key,),
        )
    return result.rowcount > 0


def list_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def reset_database(connection: sqlite3.Connection) -> dict[str, int]:
    """Delete every application row, reset sequences, and restore seed data.

    The database file and schema remain in place so live processes do not keep
    writing to an unlinked SQLite inode. An exclusive transaction prevents
    concurrent writers from observing a partially cleared database.
    """
    connection.commit()
    tables = [
        name for name in list_tables(connection)
        if not name.startswith("observation_fts_")
    ]
    deleted_rows = 0
    foreign_keys_enabled = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN EXCLUSIVE")
        for table_name in tables:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            row_count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_name}").fetchone()[0])
            connection.execute(f"DELETE FROM {quoted_name}")
            deleted_rows += row_count
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone() is not None:
            connection.execute("DELETE FROM sqlite_sequence")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}")

    initialize_database(connection)
    return {"tables_cleared": len(tables), "rows_deleted": deleted_rows}


def database_health(database_path: Path) -> dict[str, object]:
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(database_path)
        initialize_database(connection)
        table_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        pragma_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "status": "ready" if integrity == "ok" else "degraded",
            "path": str(database_path),
            "table_count": table_count,
            "journal_mode": pragma_mode,
            "integrity": integrity,
        }
    except sqlite3.Error as exc:
        return {
            "status": "degraded",
            "path": str(database_path),
            "error": str(exc),
        }
    finally:
        if connection is not None:
            connection.close()


def upsert_service_reliability_bucket(
    connection: sqlite3.Connection,
    *,
    service_name: str,
    bucket_start: str,
    is_up: bool,
    status: str,
) -> None:
    cleaned_service_name = service_name.strip().casefold()
    cleaned_bucket_start = bucket_start.strip()
    cleaned_status = status.strip().casefold() or "down"
    if not cleaned_service_name:
        raise ValueError("service_name must not be empty")
    if not cleaned_bucket_start:
        raise ValueError("bucket_start must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO service_reliability_buckets (
                service_name,
                bucket_start,
                is_up,
                status
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(service_name, bucket_start)
            DO UPDATE SET
                is_up = MIN(service_reliability_buckets.is_up, excluded.is_up),
                status = CASE
                    WHEN service_reliability_buckets.is_up = 1 AND excluded.is_up = 0
                    THEN excluded.status
                    ELSE service_reliability_buckets.status
                END,
                recorded_at = CURRENT_TIMESTAMP
            """,
            (
                cleaned_service_name,
                cleaned_bucket_start,
                1 if is_up else 0,
                cleaned_status,
            ),
        )


def list_service_reliability_buckets(
    connection: sqlite3.Connection,
    *,
    service_name: str,
    limit: int = 1440,
) -> list[sqlite3.Row]:
    cleaned_service_name = service_name.strip().casefold()
    if not cleaned_service_name:
        return []
    if limit <= 0:
        return []

    rows = connection.execute(
        """
        SELECT bucket_start, is_up, status
        FROM (
            SELECT bucket_start, is_up, status
            FROM service_reliability_buckets
            WHERE service_name = ?
            ORDER BY bucket_start DESC
            LIMIT ?
        )
        ORDER BY bucket_start ASC
        """,
        (cleaned_service_name, int(limit)),
    ).fetchall()
    return list(rows)


def record_operational_metric(
    connection: sqlite3.Connection,
    metric_name: str,
    value: float,
    *,
    dimension_key: str = "",
    observed_at: str | None = None,
) -> None:
    cleaned_name = metric_name.strip().casefold()
    if not cleaned_name:
        raise ValueError("metric_name must not be empty")
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    with connection:
        connection.execute(
            """INSERT INTO operational_metrics(metric_name,dimension_key,value,observed_at)
               VALUES (?,?,?,?) ON CONFLICT(metric_name,dimension_key,observed_at)
               DO UPDATE SET value=excluded.value""",
            (cleaned_name, dimension_key.strip().casefold(), float(value), timestamp),
        )


def operational_readiness_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    queue_rows = connection.execute(
        """SELECT stage,status,COUNT(*) count FROM processing_jobs
           GROUP BY stage,status ORDER BY stage,status"""
    ).fetchall()
    latest_metrics = connection.execute(
        """SELECT m.metric_name,m.dimension_key,m.value,m.observed_at
           FROM operational_metrics m
           JOIN (SELECT metric_name,dimension_key,MAX(observed_at) latest
                 FROM operational_metrics GROUP BY metric_name,dimension_key) x
             ON x.metric_name=m.metric_name AND x.dimension_key=m.dimension_key AND x.latest=m.observed_at
           ORDER BY m.metric_name,m.dimension_key"""
    ).fetchall()
    counters = {
        "observations_5m": int(connection.execute(
            "SELECT COUNT(*) FROM observations WHERE occurred_at>=datetime('now','-5 minutes')"
        ).fetchone()[0]),
        "observations_1h": int(connection.execute(
            "SELECT COUNT(*) FROM observations WHERE occurred_at>=datetime('now','-1 hour')"
        ).fetchone()[0]),
        "open_reviews": int(connection.execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]),
        "pending_actions": int(connection.execute("SELECT COUNT(*) FROM moderation_actions WHERE status='pending'").fetchone()[0]),
        "failed_actions_24h": int(connection.execute(
            "SELECT COUNT(*) FROM moderation_actions WHERE status='failed' AND created_at>=datetime('now','-1 day')"
        ).fetchone()[0]),
        "open_alerts": int(connection.execute("SELECT COUNT(*) FROM intelligence_alerts WHERE status='open'").fetchone()[0]),
        "open_cases": int(connection.execute("SELECT COUNT(*) FROM investigation_cases WHERE status!='closed'").fetchone()[0]),
    }
    return {
        "counters": counters,
        "queues": [dict(row) for row in queue_rows],
        "latest_metrics": [dict(row) for row in latest_metrics],
        "schema_version": int(connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]),
        "latest_evaluation_at": connection.execute(
            "SELECT MAX(calculated_at) FROM model_evaluation_runs"
        ).fetchone()[0],
    }


def ensure_platform_account(
    connection: sqlite3.Connection,
    *,
    platform: str,
    platform_user_id: str,
    username: str,
    guild_or_channel_context: str | None,
) -> int:
    with connection:
        connection.execute(
            """
            INSERT INTO platform_accounts (
                platform,
                platform_user_id,
                username,
                guild_or_channel_context
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_user_id)
            DO UPDATE SET
                username = excluded.username,
                guild_or_channel_context = COALESCE(
                    excluded.guild_or_channel_context,
                    platform_accounts.guild_or_channel_context
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, platform_user_id, username, guild_or_channel_context),
        )
        row = connection.execute(
            """
            SELECT id
            FROM platform_accounts
            WHERE platform = ? AND platform_user_id = ?
            """,
            (platform, platform_user_id),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve platform account after upsert")

    return int(row[0])


def upsert_operator_account(
    connection: sqlite3.Connection,
    *,
    discord_user_id: str,
    discord_username: str,
    role: str,
) -> int:
    with connection:
        connection.execute(
            """
            INSERT INTO operator_accounts (
                discord_user_id,
                discord_username,
                role
            ) VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id)
            DO UPDATE SET
                discord_username = excluded.discord_username,
                role = excluded.role,
                updated_at = CURRENT_TIMESTAMP
            """,
            (discord_user_id, discord_username, role),
        )
        row = connection.execute(
            """
            SELECT id
            FROM operator_accounts
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve operator account after upsert")

    return int(row[0])


def get_operator_account_by_discord_user_id(
    connection: sqlite3.Connection,
    discord_user_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, discord_user_id, discord_username, role
        FROM operator_accounts
        WHERE discord_user_id = ?
        """,
        (discord_user_id,),
    ).fetchone()


def list_command_definitions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT command_name, title, description_template, footer_template, enabled, created_at, updated_at
        FROM command_definitions
        WHERE command_name NOT IN ('addcom', 'delcom', 'editcom')
        ORDER BY command_name
        """
    ).fetchall()
    return list(rows)


def get_command_definition(connection: sqlite3.Connection, command_name: str) -> sqlite3.Row | None:
    command_key = command_name.strip().casefold()
    if command_key in RESERVED_COMMAND_NAMES:
        return None
    return connection.execute(
        """
        SELECT command_name, title, description_template, footer_template, enabled, created_at, updated_at
        FROM command_definitions
        WHERE command_name = ?
        """,
        (command_key,),
    ).fetchone()


def upsert_command_definition(
    connection: sqlite3.Connection,
    *,
    command_name: str,
    title: str,
    description_template: str,
    footer_template: str | None,
    enabled: bool,
) -> None:
    command_key = command_name.strip().casefold()
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    if not title.strip():
        raise ValueError("title must not be empty")
    if not description_template.strip():
        raise ValueError("description_template must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO command_definitions (
                command_name,
                title,
                description_template,
                footer_template,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_name)
            DO UPDATE SET
                title = excluded.title,
                description_template = excluded.description_template,
                footer_template = excluded.footer_template,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                command_key,
                title.strip(),
                description_template.strip(),
                footer_template.strip() if footer_template and footer_template.strip() else None,
                int(enabled),
            ),
        )


def persist_normalized_message(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
    *,
    observation_id: int | None = None,
    moderation_shadow_mode: bool = False,
) -> IngestionResult:
    platform_account_id = ensure_platform_account(
        connection,
        platform=message.platform,
        platform_user_id=message.platform_user_id,
        username=message.username,
        guild_or_channel_context=message.guild_or_channel_context,
    )

    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    platform,
                    platform_message_id,
                    platform_account_id,
                    channel_id,
                    content_raw,
                    content_normalized,
                    sent_at,
                    observation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.platform,
                    message.platform_message_id,
                    platform_account_id,
                    message.channel_id,
                    message.content_raw,
                    message.content_normalized,
                    message.sent_at,
                    observation_id,
                ),
            )
            message_id = int(cursor.lastrowid)
            _persist_message_attachments(
                connection,
                message_id=message_id,
                attachments=message.metadata.get("attachment_urls"),
            )
            canonical_user_id = ensure_canonical_user_for_platform_account(
                connection,
                platform_account_id=platform_account_id,
                preferred_display_name=message.username,
            )
            connection.execute(
                "UPDATE messages SET user_id = ? WHERE id = ?",
                (canonical_user_id, message_id),
            )

            message_delta = score_delta_for_message(message.content_raw)
            if message_delta is not None:
                delta, reason_code = message_delta
                # Reward constructive human-to-human replies with a higher baseline bonus.
                if delta > 0 and message.metadata.get("reply_to_author_is_bot") is False:
                    delta = 2
                    reason_code = "reply_to_non_bot"
                record_reputation_evidence(
                    connection,
                    user_id=canonical_user_id,
                    delta=delta,
                    reason_code=reason_code,
                    source_type="message",
                    source_id=message_id,
                )

            welcome_delta = _score_delta_for_welcome_message(
                connection,
                message=message,
                platform_account_id=platform_account_id,
                message_id=message_id,
            )
            if welcome_delta is not None:
                delta, reason_code = welcome_delta
                record_reputation_evidence(
                    connection,
                    user_id=canonical_user_id,
                    delta=delta,
                    reason_code=reason_code,
                    source_type="message",
                    source_id=message_id,
                )

            moderation_rules = load_enabled_moderation_rules(connection)
            builtin_egregious_rule = next(
                (r for r in moderation_rules if r.name == BUILTIN_EGREGIOUS_RULE_NAME),
                None,
            )
            if moderation_rules:
                findings = evaluate_message_moderation(message, moderation_rules)
                record_moderation_findings(
                    connection,
                    message_id=message_id,
                    platform=message.platform,
                    findings=findings,
                    force_shadow=moderation_shadow_mode,
                )

                for finding in findings:
                    penalty_delta, penalty_reason = score_delta_for_moderation(
                        severity=finding.severity,
                        action_type=finding.auto_enforce_action,
                        reason_code=finding.reason_code,
                    )
                    record_reputation_evidence(
                        connection,
                        user_id=canonical_user_id,
                        delta=penalty_delta,
                        reason_code=penalty_reason,
                        source_type="moderation",
                        source_id=message_id,
                    )

            if builtin_egregious_rule is not None:
                egregious_findings = evaluate_egregious_content(message, builtin_egregious_rule)
                if egregious_findings:
                    record_moderation_findings(
                        connection,
                        message_id=message_id,
                        platform=message.platform,
                        findings=egregious_findings,
                        force_shadow=moderation_shadow_mode,
                    )
                    for finding in egregious_findings:
                        penalty_delta, penalty_reason = score_delta_for_moderation(
                            severity=finding.severity,
                            action_type=finding.auto_enforce_action,
                            reason_code=finding.reason_code,
                        )
                        record_reputation_evidence(
                            connection,
                            user_id=canonical_user_id,
                            delta=penalty_delta,
                            reason_code=penalty_reason,
                            source_type="moderation",
                            source_id=message_id,
                        )
        return IngestionResult(
            status="persisted",
            platform=message.platform,
            platform_account_id=platform_account_id,
            message_id=message_id,
        )
    except sqlite3.IntegrityError:
        if message.platform_message_id is None:
            raise

        row = connection.execute(
            """
            SELECT id
            FROM messages
            WHERE platform = ? AND platform_message_id = ?
            """,
            (message.platform, message.platform_message_id),
        ).fetchone()
        if row is None:
            raise

        return IngestionResult(
            status="duplicate",
            platform=message.platform,
            platform_account_id=platform_account_id,
            message_id=int(row[0]),
            reason="message_already_ingested",
        )


def _persist_message_attachments(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    attachments: object,
) -> None:
    if not isinstance(attachments, (list, tuple)):
        return

    attachment_rows: list[tuple[int, int, str]] = []
    for index, raw_attachment in enumerate(attachments, start=1):
        url = str(raw_attachment).strip()
        if not url:
            continue
        attachment_rows.append((message_id, index, url))

    if not attachment_rows:
        return

    connection.executemany(
        """
        INSERT INTO message_attachments (
            message_id,
            attachment_index,
            attachment_url
        ) VALUES (?, ?, ?)
        """,
        attachment_rows,
    )


def _score_delta_for_welcome_message(
    connection: sqlite3.Connection,
    *,
    message: NormalizedMessage,
    platform_account_id: int,
    message_id: int,
) -> tuple[int, str] | None:
    target_identifier = _extract_welcome_target_identifier(message)
    if target_identifier is None:
        return None

    message_time = datetime.fromisoformat(message.sent_at).astimezone(timezone.utc)
    duplicate_cutoff = (message_time - timedelta(minutes=WELCOME_DUPLICATE_WINDOW_MINUTES)).isoformat()
    duplicate_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM welcome_events
            WHERE sender_platform_account_id = ?
              AND target_identifier = ?
              AND created_at >= ?
            """,
            (platform_account_id, target_identifier, duplicate_cutoff),
        ).fetchone()[0]
    )

    connection.execute(
        """
        INSERT INTO welcome_events (
            sender_platform_account_id,
            target_identifier,
            message_id,
            created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (platform_account_id, target_identifier, message_id, message.sent_at),
    )

    if duplicate_count > 0:
        return (WELCOME_DUPLICATE_PENALTY_DELTA, "welcome_spam_duplicate")
    return (WELCOME_BONUS_DELTA, "welcome_new_user")


def _extract_welcome_target_identifier(message: NormalizedMessage) -> str | None:
    normalized = message.content_normalized
    if "welcome" not in normalized:
        return None

    mentioned_user_ids = message.metadata.get("mentioned_user_ids")
    if isinstance(mentioned_user_ids, (list, tuple)):
        for raw_id in mentioned_user_ids:
            target = str(raw_id).strip()
            if not target:
                continue
            if target == message.platform_user_id:
                continue
            return f"id:{target}"

    mention_match = _WELCOME_TARGET_MENTION_PATTERN.search(message.content_raw)
    if mention_match is not None:
        target_id = mention_match.group(1).strip()
        if target_id and target_id != message.platform_user_id:
            return f"id:{target_id}"

    handle_match = _WELCOME_TARGET_HANDLE_PATTERN.search(message.content_raw)
    if handle_match is not None:
        handle = handle_match.group(1).strip().casefold()
        if handle and handle != message.username.casefold():
            return f"handle:{handle}"

    return None


def ensure_canonical_user_for_platform_account(
    connection: sqlite3.Connection,
    *,
    platform_account_id: int,
    preferred_display_name: str,
) -> int:
    row = connection.execute(
        """
        SELECT user_id
        FROM platform_accounts
        WHERE id = ?
        """,
        (platform_account_id,),
    ).fetchone()
    if row is None:
        raise ValueError("platform account not found")

    existing_user_id = row[0]
    if existing_user_id is not None:
        return int(existing_user_id)

    display_name = preferred_display_name.strip() or f"user_{platform_account_id}"
    initial_score = default_social_score_for_name(display_name)
    connection.execute(
        """
        INSERT INTO users (
            primary_display_name,
            current_reputation_score,
            candidate_flag
        ) VALUES (?, ?, ?)
        """,
        (display_name, initial_score, int(initial_score >= POWERUSER_THRESHOLD)),
    )
    created_user = connection.execute(
        "SELECT id FROM users WHERE rowid = last_insert_rowid()"
    ).fetchone()
    if created_user is None:
        raise sqlite3.IntegrityError("Failed to resolve canonical user after insert")

    created_user_id = int(created_user[0])
    connection.execute(
        """
        UPDATE platform_accounts
        SET user_id = ?, detached_from_user_id = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (created_user_id, platform_account_id),
    )
    connection.execute(
        """
        INSERT INTO audit_log (
            actor_type,
            actor_id,
            action_type,
            entity_type,
            entity_id,
            payload_json
        ) VALUES (
            'system',
            NULL,
            'auto_user_create',
            'user',
            ?,
            json_object('platform_account_id', ?)
        )
        """,
        (created_user_id, platform_account_id),
    )
    return created_user_id


def upsert_twitch_channel(
    connection: sqlite3.Connection,
    *,
    channel_name: str,
    requested_by_platform_account_id: int | None,
    request_source_message_id: int | None,
    join_source: str,
    status: str = "requested",
) -> int:
    normalized_channel_name = channel_name.strip().casefold()
    if not normalized_channel_name:
        raise ValueError("channel_name must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO twitch_channels (
                channel_name,
                requested_by_platform_account_id,
                request_source_message_id,
                join_source,
                status
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_name)
            DO UPDATE SET
                requested_by_platform_account_id = excluded.requested_by_platform_account_id,
                request_source_message_id = excluded.request_source_message_id,
                join_source = excluded.join_source,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                normalized_channel_name,
                requested_by_platform_account_id,
                request_source_message_id,
                join_source,
                status,
            ),
        )
        row = connection.execute(
            "SELECT id FROM twitch_channels WHERE channel_name = ?",
            (normalized_channel_name,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve twitch channel after upsert")

    return int(row[0])


def list_twitch_channels(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[sqlite3.Row]:
    if status is None:
        rows = connection.execute(
            "SELECT * FROM twitch_channels ORDER BY channel_name"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM twitch_channels WHERE status = ? ORDER BY channel_name",
            (status,),
        ).fetchall()
    return list(rows)


def update_twitch_channel_status(
    connection: sqlite3.Connection,
    *,
    channel_name: str,
    status: str,
) -> None:
    normalized_channel_name = channel_name.strip().casefold()
    if not normalized_channel_name:
        raise ValueError("channel_name must not be empty")

    with connection:
        connection.execute(
            """
            UPDATE twitch_channels
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE channel_name = ?
            """,
            (status, normalized_channel_name),
        )


def upsert_moderation_rule(
    connection: sqlite3.Connection,
    *,
    name: str,
    rule_type: str,
    pattern: str,
    severity: str,
    auto_enforce_action: str | None = None,
    enabled: bool = True,
    enforcement_mode: str = "enforce",
    action_duration_seconds: int = 600,
) -> int:
    cleaned_name = name.strip()
    cleaned_type = rule_type.strip().casefold()
    cleaned_severity = severity.strip().casefold()
    cleaned_action = auto_enforce_action.strip().casefold() if auto_enforce_action else None
    if not cleaned_name:
        raise ValueError("moderation rule name must not be empty")
    if cleaned_type not in {"exact_term", "banned_phrase", "streamboo_viewer_spam", "link_restriction", "duplicate_message", "egregious_term"}:
        raise ValueError("unsupported moderation rule type")
    if cleaned_severity not in {"low", "medium", "high", "critical"}:
        raise ValueError("unsupported moderation severity")
    if cleaned_action not in {None, "warn", "timeout", "ban"}:
        raise ValueError("unsupported moderation action")
    normalized_mode = enforcement_mode.strip().casefold()
    if normalized_mode not in {"enforce", "review", "shadow", "disabled"}:
        raise ValueError("enforcement_mode must be enforce, review, shadow, or disabled")
    duration = max(1, min(int(action_duration_seconds), 2_419_200))
    with connection:
        connection.execute(
            """
            INSERT INTO moderation_rules (
                name,
                rule_type,
                pattern,
                severity,
                auto_enforce_action,
                enabled,
                enforcement_mode,
                action_duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name)
            DO UPDATE SET
                rule_type = excluded.rule_type,
                pattern = excluded.pattern,
                severity = excluded.severity,
                auto_enforce_action = excluded.auto_enforce_action,
                enabled = excluded.enabled,
                enforcement_mode = excluded.enforcement_mode,
                action_duration_seconds = excluded.action_duration_seconds,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                cleaned_name,
                cleaned_type,
                pattern,
                cleaned_severity,
                cleaned_action,
                int(enabled),
                normalized_mode,
                duration,
            ),
        )
        row = connection.execute(
            "SELECT id FROM moderation_rules WHERE name = ?",
            (cleaned_name,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve moderation rule after upsert")

    return int(row[0])


def load_enabled_moderation_rules(connection: sqlite3.Connection) -> list[ModerationRule]:
    rows = connection.execute(
        """
        SELECT id, name, rule_type, pattern, severity, auto_enforce_action, enabled,
               enforcement_mode, action_duration_seconds
        FROM moderation_rules
        WHERE enabled = 1
        ORDER BY id
        """
    ).fetchall()
    return [
        ModerationRule(
            id=int(row[0]),
            name=str(row[1]),
            rule_type=str(row[2]),
            pattern=str(row[3]),
            severity=str(row[4]),
            auto_enforce_action=str(row[5]) if row[5] is not None else None,
            enabled=bool(row[6]),
            enforcement_mode=str(row[7]),
            action_duration_seconds=int(row[8]),
        )
        for row in rows
    ]


def record_moderation_findings(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    platform: str,
    findings: list[ModerationFinding],
    force_shadow: bool = False,
) -> None:
    if not findings:
        return

    message_row = connection.execute(
        "SELECT user_id, observation_id FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    user_id = (
        int(message_row[0])
        if message_row is not None and message_row[0] is not None
        else None
    )
    observation_id = (
        int(message_row[1])
        if message_row is not None and message_row[1] is not None
        else None
    )

    for finding in findings:
        effective_mode = "shadow" if force_shadow else finding.enforcement_mode
        connection.execute(
            """
            INSERT INTO rule_matches (
                message_id,
                moderation_rule_id,
                severity,
                reason_code,
                confidence,
                recommended_action
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                finding.rule_id,
                finding.severity,
                finding.reason_code,
                1.0,
                finding.auto_enforce_action,
            ),
        )
        if finding.severity.strip().casefold() in {"high", "critical"}:
            title = finding.rule_name.removeprefix("builtin:").replace("_", " ").title()
            connection.execute(
                """
                INSERT INTO intelligence_alerts(
                    user_id, observation_id, alert_type, severity, title, summary,
                    confidence, dedupe_key
                ) VALUES (?, ?, 'moderation_finding', ?, ?, ?, 1.0, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    user_id,
                    observation_id,
                    finding.severity,
                    title,
                    f"{finding.reason_code} matched message {message_id} on {platform} ({effective_mode})",
                    f"moderation:{message_id}:{finding.rule_id}",
                ),
            )
        should_enforce = bool(
            finding.auto_enforce_action
            and effective_mode == "enforce"
        )
        if should_enforce:
            connection.execute(
                """
                INSERT INTO moderation_actions (
                    platform,
                    message_id,
                    target_platform_account_id,
                    user_id,
                    action_type,
                    actor_type,
                    actor_id,
                    reason,
                    duration_seconds,
                    status
                ) VALUES (
                    ?,
                    ?,
                    (SELECT platform_account_id FROM messages WHERE id = ?),
                    (SELECT user_id FROM messages WHERE id = ?),
                    ?,
                    'system',
                    NULL,
                    ?,
                    ?,
                    'pending'
                )
                """,
                (
                    platform,
                    message_id,
                    message_id,
                    message_id,
                    finding.auto_enforce_action,
                    finding.reason_code,
                    finding.action_duration_seconds,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO review_queue (
                    message_id,
                    status,
                    severity,
                    queue_reason_code,
                    assigned_operator_id,
                    created_at,
                    resolved_at
                ) VALUES (?, 'open', ?, ?, NULL, CURRENT_TIMESTAMP, NULL)
                """,
                (
                    message_id,
                    finding.severity,
                    finding.reason_code,
                ),
            )


def record_moderation_action(
    connection: sqlite3.Connection,
    *,
    platform: str,
    message_id: int | None,
    target_platform_account_id: int,
    action_type: str,
    reason: str,
    status: str = "pending",
    actor_type: str = "system",
    actor_id: int | None = None,
) -> int:
    attribution = connection.execute(
        """
        SELECT COALESCE(
            (SELECT user_id FROM messages WHERE id = ?),
            user_id,
            detached_from_user_id
        )
        FROM platform_accounts
        WHERE id = ?
        """,
        (message_id, target_platform_account_id),
    ).fetchone()
    attributed_user_id = attribution[0] if attribution is not None else None
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO moderation_actions (
                platform,
                message_id,
                target_platform_account_id,
                user_id,
                action_type,
                actor_type,
                actor_id,
                reason,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                message_id,
                target_platform_account_id,
                attributed_user_id,
                action_type,
                actor_type,
                actor_id,
                reason,
                status,
            ),
        )
    return int(cursor.lastrowid)


def list_pending_moderation_actions_for_message(
    connection: sqlite3.Connection,
    message_id: int,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            moderation_actions.id,
            moderation_actions.platform,
            moderation_actions.target_platform_account_id,
            moderation_actions.action_type,
            moderation_actions.reason,
            platform_accounts.username,
            moderation_actions.duration_seconds
        FROM moderation_actions
        INNER JOIN platform_accounts ON platform_accounts.id = moderation_actions.target_platform_account_id
        WHERE moderation_actions.message_id = ?
          AND moderation_actions.status = 'pending'
        ORDER BY moderation_actions.id
        """,
        (message_id,),
    ).fetchall()
    return list(rows)


def mark_moderation_action_completed(
    connection: sqlite3.Connection,
    action_id: int,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE moderation_actions
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE id = ?
            """,
            (action_id,),
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_server_boost_request(
    connection: sqlite3.Connection,
    *,
    platform: str,
    channel_id: str,
    requester_platform_account_id: int,
    command_name: str,
    requested_at: str | None = None,
    expires_in_minutes: int = 30,
) -> int:
    account = connection.execute(
        """
        SELECT user_id
        FROM platform_accounts
        WHERE id = ?
        """,
        (requester_platform_account_id,),
    ).fetchone()
    if account is None or account[0] is None:
        raise ValueError("requesting platform account is not linked to a canonical user")

    now = requested_at or _utcnow_iso()
    expires_at = (datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(minutes=expires_in_minutes)).isoformat()
    requester_user_id = int(account[0])
    with connection:
        row = connection.execute(
            """
            SELECT id
            FROM server_boost_requests
            WHERE platform = ?
              AND channel_id = ?
              AND requester_user_id = ?
              AND command_name = ?
              AND status = 'pending'
            ORDER BY requested_at DESC, id DESC
            LIMIT 1
            """,
            (platform, channel_id, requester_user_id, command_name),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO server_boost_requests (
                    platform,
                    channel_id,
                    requester_user_id,
                    requester_platform_account_id,
                    command_name,
                    status,
                    requested_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    platform,
                    channel_id,
                    requester_user_id,
                    requester_platform_account_id,
                    command_name,
                    now,
                    expires_at,
                ),
            )
            request_id = int(cursor.lastrowid)
        else:
            request_id = int(row[0])
            connection.execute(
                """
                UPDATE server_boost_requests
                SET requested_at = ?,
                    expires_at = ?,
                    status = 'pending',
                    fulfilled_at = NULL
                WHERE id = ?
                """,
                (now, expires_at, request_id),
            )

    return request_id


def reward_server_boost_request(
    connection: sqlite3.Connection,
    *,
    platform: str,
    channel_id: str,
    command_names: tuple[str, ...],
    reward_delta: int = 2,
    reason_code: str = "server_boost_success",
) -> int | None:
    command_names = tuple(command_name.strip().casefold() for command_name in command_names if command_name.strip())
    if not command_names:
        return None

    now = _utcnow_iso()
    row = connection.execute(
        f"""
        SELECT id, requester_user_id, command_name
        FROM server_boost_requests
        WHERE platform = ?
          AND channel_id = ?
          AND status = 'pending'
          AND expires_at > ?
          AND command_name IN ({','.join('?' for _ in command_names)})
        ORDER BY requested_at ASC, id ASC
        LIMIT 1
        """,
        (platform, channel_id, now, *command_names),
    ).fetchone()
    if row is None:
        return None

    request_id = int(row[0])
    requester_user_id = int(row[1])
    command_name = str(row[2])
    record_reputation_evidence(
        connection,
        user_id=requester_user_id,
        delta=reward_delta,
        reason_code=reason_code,
        source_type="server_boost",
        source_id=request_id,
    )
    from .intelligence.scoring import calculate_social_score
    from .intelligence.signals import refresh_user_derived_signals

    refresh_user_derived_signals(connection, requester_user_id)
    calculate_social_score(connection, requester_user_id)
    with connection:
        connection.execute(
            """
            UPDATE server_boost_requests
            SET status = 'fulfilled',
                fulfilled_at = ?
            WHERE id = ?
            """,
            (now, request_id),
        )

    return request_id


def upsert_discord_channel(
    connection: sqlite3.Connection,
    *,
    guild_id: str,
    channel_id: str,
    channel_name: str,
    channel_type: int,
) -> None:
    guild_key = guild_id.strip()
    channel_key = channel_id.strip()
    channel_label = channel_name.strip()
    if not guild_key or not channel_key or not channel_label:
        raise ValueError("guild_id, channel_id, and channel_name must not be empty")
    with connection:
        connection.execute(
            """
            INSERT INTO discord_channels (
                channel_id,
                guild_id,
                channel_name,
                channel_type
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id)
            DO UPDATE SET
                guild_id = excluded.guild_id,
                channel_name = excluded.channel_name,
                channel_type = excluded.channel_type,
                updated_at = CURRENT_TIMESTAMP
            """,
            (channel_key, guild_key, channel_label, int(channel_type)),
        )


def get_discord_channel_name(connection: sqlite3.Connection, channel_id: str) -> str | None:
    channel_key = channel_id.strip()
    if not channel_key:
        return None
    row = connection.execute(
        """
        SELECT channel_name
        FROM discord_channels
        WHERE channel_id = ?
        """,
        (channel_key,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def has_twitch_live_announcement(
    connection: sqlite3.Connection,
    *,
    twitch_channel_name: str,
    twitch_stream_id: str,
    discord_guild_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM twitch_live_announcements
        WHERE twitch_channel_name = ?
          AND twitch_stream_id = ?
          AND discord_guild_id = ?
        LIMIT 1
        """,
        (
            twitch_channel_name.strip().casefold(),
            twitch_stream_id.strip(),
            discord_guild_id.strip(),
        ),
    ).fetchone()
    return row is not None


def record_twitch_live_announcement(
    connection: sqlite3.Connection,
    *,
    twitch_channel_name: str,
    twitch_stream_id: str,
    discord_guild_id: str,
    discord_channel_id: str,
    announced_at: str | None = None,
) -> None:
    timestamp = announced_at or _utcnow_iso()
    with connection:
        connection.execute(
            """
            INSERT INTO twitch_live_announcements (
                twitch_channel_name,
                twitch_stream_id,
                discord_guild_id,
                discord_channel_id,
                announced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(twitch_channel_name, twitch_stream_id, discord_guild_id)
            DO NOTHING
            """,
            (
                twitch_channel_name.strip().casefold(),
                twitch_stream_id.strip(),
                discord_guild_id.strip(),
                discord_channel_id.strip(),
                timestamp,
            ),
        )

def persist_observation(
    connection: sqlite3.Connection,
    observation: Observation,
) -> ObservationResult:
    actor_account_id = None
    target_account_id = None

    if observation.actor_platform_user_id:
        actor_account_id = ensure_platform_account(
            connection,
            platform=observation.platform,
            platform_user_id=observation.actor_platform_user_id,
            username=(
                observation.actor_username
                or observation.actor_platform_user_id
            ),
            guild_or_channel_context=observation.context_id,
        )

    if observation.target_platform_user_id:
        target_account_id = ensure_platform_account(
            connection,
            platform=observation.platform,
            platform_user_id=observation.target_platform_user_id,
            username=observation.target_platform_user_id,
            guild_or_channel_context=observation.context_id,
        )

    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO observations (
                    platform,
                    event_type,
                    external_event_id,
                    actor_platform_account_id,
                    target_platform_account_id,
                    container_id,
                    context_id,
                    text_raw,
                    attributes_json,
                    occurred_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.platform,
                    observation.event_type,
                    observation.external_event_id,
                    actor_account_id,
                    target_account_id,
                    observation.container_id,
                    observation.context_id,
                    observation.text,
                    json.dumps(
                        observation.attributes,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    observation.occurred_at,
                    observation.schema_version,
                ),
            )
    except sqlite3.IntegrityError:
        if observation.external_event_id is None:
            raise
        return ObservationResult(
            status="duplicate",
            observation_id=None,
            actor_platform_account_id=actor_account_id,
            target_platform_account_id=target_account_id,
        )

    return ObservationResult(
        status="persisted",
        observation_id=int(cursor.lastrowid),
        actor_platform_account_id=actor_account_id,
        target_platform_account_id=target_account_id,
    )

def enqueue_processing_job(
    connection: sqlite3.Connection,
    *,
    stage: str,
    job_type: str,
    idempotency_key: str,
    observation_id: int | None = None,
    payload: Mapping[str, object] | None = None,
    priority: int = 100,
    max_attempts: int = 5,
) -> int | None:
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO processing_jobs (
                stage,
                job_type,
                observation_id,
                payload_json,
                priority,
                max_attempts,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                stage,
                job_type,
                observation_id,
                json.dumps(payload or {}, sort_keys=True),
                priority,
                max_attempts,
                idempotency_key,
            ),
        )

    if cursor.rowcount == 0:
        return None

    return int(cursor.lastrowid)

def claim_processing_job(
    connection: sqlite3.Connection,
    *,
    stage: str,
    worker_id: str,
) -> sqlite3.Row | None:
    connection.execute("BEGIN IMMEDIATE")

    try:
        row = connection.execute(
            """
            SELECT *
            FROM processing_jobs
            WHERE stage = ?
              AND status IN ('pending', 'retry')
              AND available_at <= CURRENT_TIMESTAMP
              AND attempts < max_attempts
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (stage,),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'running',
                claimed_at = CURRENT_TIMESTAMP,
                claimed_by = ?,
                attempts = attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (worker_id, int(row["id"])),
        )
        connection.commit()

        return connection.execute(
            "SELECT * FROM processing_jobs WHERE id = ?",
            (int(row["id"]),),
        ).fetchone()
    except Exception:
        connection.rollback()
        raise

def complete_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP,
                last_error = NULL
            WHERE id = ?
            """,
            (job_id,),
        )

def fail_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    retry_delay_seconds: int,
) -> None:
    row = connection.execute(
        """
        SELECT attempts, max_attempts
        FROM processing_jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()

    if row is None:
        return

    exhausted = int(row["attempts"]) >= int(row["max_attempts"])
    status = "failed" if exhausted else "retry"

    with connection:
        connection.execute(
            """
            UPDATE processing_jobs
            SET status = ?,
                available_at = datetime(
                    CURRENT_TIMESTAMP,
                    '+' || ? || ' seconds'
                ),
                last_error = ?,
                claimed_at = NULL,
                claimed_by = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                retry_delay_seconds,
                error[:2000],
                job_id,
            ),
        )

def collect_observation(
    connection: sqlite3.Connection,
    observation: Observation,
) -> CollectedObservation:
    with connection:
        result = persist_observation(
            connection,
            observation
        )

        if result.status == "duplicate":
            return CollectedObservation(
                observation_id=result.observation_id,
                status="duplicate",
                analysis_job_id=None,
            )

        observation_id = result.observation_id
        assert observation_id is not None

        job_id = enqueue_processing_job(
            connection,
            stage="analysis",
            job_type=f"analyze.{observation.event_type}",
            observation_id=observation_id,
            idempotency_key=(
                f"observation:{observation_id}:"
                f"{observation.event_type}:v1"
            ),
        )

    return CollectedObservation(
        observation_id=observation_id,
        status="persisted",
        analysis_job_id=job_id,
    )

def normalized_message_from_observation(
    row: sqlite3.Row,
) -> NormalizedMessage:
    event_type = str(row["event_type"])

    if event_type != "message.created":
        raise ValueError(
            f"Cannot construct a message from {event_type}"
        )

    raw_attributes = json.loads(
        str(row["attributes_json"] or "{}")
    )

    if not isinstance(raw_attributes, dict):
        raise ValueError(
            "Observation attributes must be a JSON object"
        )

    role_names_value = raw_attributes.pop(
        "role_names",
        (),
    )
    if isinstance(role_names_value, list):
        role_names = tuple(
            str(role)
            for role in role_names_value
        )
    else:
        role_names = ()

    is_moderator = bool(
        raw_attributes.pop("is_moderator", False)
    )

    return NormalizedMessage(
        platform=str(row["platform"]),
        platform_message_id=(
            str(row["external_event_id"])
            if row["external_event_id"] is not None
            else None
        ),
        platform_user_id=str(
            row["actor_platform_user_id"]
        ),
        username=str(row["actor_username"]),
        channel_id=str(row["container_id"]),
        guild_or_channel_context=(
            str(row["context_id"])
            if row["context_id"] is not None
            else None
        ),
        content_raw=str(row["text_raw"] or ""),
        sent_at=str(row["occurred_at"]),
        role_names=role_names,
        is_moderator=is_moderator,
        metadata=raw_attributes,
    )

def get_observation(
    connection: sqlite3.Connection,
    observation_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            observations.*,
            actor.platform_user_id
                AS actor_platform_user_id,
            actor.username
                AS actor_username,
            target.platform_user_id
                AS target_platform_user_id
        FROM observations
        LEFT JOIN platform_accounts AS actor
            ON actor.id =
               observations.actor_platform_account_id
        LEFT JOIN platform_accounts AS target
            ON target.id =
               observations.target_platform_account_id
        WHERE observations.id = ?
        """,
        (observation_id,),
    ).fetchone()
