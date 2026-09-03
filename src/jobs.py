from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import time
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AppSettings
from .contexts import TenantContext
from .db import (
	connect_database,
	has_twitch_live_announcement,
	initialize_database,
	record_twitch_live_announcement,
	upsert_discord_channel,
	record_operational_metric,
)
from .permissions import _everyone_cannot_view
from .token_store import persist_refreshed_twitch_tokens
from .twitch_auth import TwitchAuthError, TwitchTokenManager
from .intelligence.signals import refresh_all_derived_signals
from .intelligence.announcements import dispatch_due_announcements, queue_system_announcement
from .intelligence.onboarding import dispatch_newcomer_roles, queue_due_checkpoint_reminders
from .intelligence.scoring import SOCIAL_SCORE_MODEL_VERSION, calculate_social_score
from .models import Observation
from .db import collect_observation
from .db_protocol import DatabaseTarget
from .intelligence.analytics import (
	refresh_community_cohort_baselines,
	refresh_emerging_topics,
	refresh_graph_analytics,
	refresh_identity_suggestions,
	run_model_evaluation,
	emit_analytics_alerts,
)
from .pipeline.handlers import recover_expired_processing_jobs
from .intelligence.recovery import flush_raw_event_archive
from .surface_policy import require_non_http_surface
from .intelligence.professional_ops import (
    dispatch_pending_notifications,
    refresh_stream_cohorts,
)


JobConnectionFactory = Callable[[Path], sqlite3.Connection]
JOBS_LOGGER = logging.getLogger("qbot4k.jobs")
_CHANNEL_MATCH_STOPWORDS = {
	"and",
	"for",
	"the",
	"this",
	"that",
	"with",
	"from",
	"into",
	"your",
	"our",
	"are",
	"was",
	"were",
	"live",
	"stream",
	"working",
	"giving",
}

@dataclass(frozen=True)
class MaintenanceReport:
	deleted_messages: int
	deleted_observations: int
	deleted_audit_log_rows: int
	deleted_signal_runs: int
	deleted_score_runs: int
	deleted_processing_jobs: int
	backup_path: str
	backup_metadata_path: str
	backup_sha256: str
	rollup_rows: int
	topic_count: int
	graph_node_count: int
	identity_suggestion_count: int
	cohort_baseline_count: int
	evaluation_run_id: int
	raw_events_archived: int = 0


@dataclass(frozen=True)
class TwitchLiveStream:
	stream_id: str
	title: str
	url: str
	game_name: str


def run_maintenance_jobs(
	settings: AppSettings,
	*,
	connection_factory: JobConnectionFactory = connect_database,
	now: datetime | None = None,
	perform_maintenance: bool = True,
	perform_analytics: bool = True,
	perform_backup: bool = True,
) -> MaintenanceReport:
	require_non_http_surface("job:maintenance", guard="system")
	current_time = now or datetime.now(UTC)
	connection = connection_factory(settings.database_path)
	try:
		initialize_database(connection)
		deleted_messages = deleted_observations = deleted_audit_rows = 0
		deleted_score_runs = deleted_signal_runs = deleted_processing_jobs = 0
		rollup_rows = 0
		topic_count = graph_node_count = identity_suggestion_count = 0
		cohort_baseline_count = evaluation_run_id = 0
		raw_events_archived = 0
		if perform_maintenance:
			recover_expired_processing_jobs(connection)
			raw_events_archived = flush_raw_event_archive(connection, settings.raw_archive_dir)
			retention_policies = connection.execute(
				"""SELECT community_id,message_retention_days
				   FROM community_policy_settings ORDER BY community_id"""
			).fetchall()
			deleted_messages = sum(
				purge_expired_messages(
					connection, current_time, int(policy[1]), community_id=int(policy[0])
				)
				for policy in retention_policies
			)
			deleted_observations = sum(
				purge_expired_observations(
					connection, current_time, int(policy[1]), community_id=int(policy[0])
				)
				for policy in retention_policies
			)
			deleted_audit_rows = purge_expired_audit_log(connection, current_time, settings.audit_retention_days)
			deleted_score_runs = purge_expired_score_runs(connection, current_time, settings.audit_retention_days)
			deleted_signal_runs = purge_expired_signal_runs(connection, current_time, settings.audit_retention_days)
			deleted_processing_jobs = purge_expired_processing_jobs(connection, current_time, settings.audit_retention_days)
			rollup_rows = refresh_metrics_rollups(connection, current_time)
		if perform_analytics:
			refresh_all_derived_signals(connection)
			for user_row in connection.execute(
				"SELECT id FROM users WHERE score_model_version IS NULL OR score_model_version<>? ORDER BY id",
				(SOCIAL_SCORE_MODEL_VERSION,),
			).fetchall():
				calculate_social_score(connection, int(user_row[0]), calculated_at=current_time.isoformat())
			with connection:
				for active_session in connection.execute(
					"SELECT id FROM stream_sessions WHERE status='live'"
				).fetchall():
					refresh_stream_cohorts(connection, int(active_session[0]))
				topic_count = sum(
					refresh_emerging_topics(
						connection, now=current_time, community_id=int(community[0])
					)
					for community in connection.execute(
						"SELECT id FROM communities ORDER BY id"
					).fetchall()
				)
				graph_node_count = sum(
					refresh_graph_analytics(
						connection, calculated_at=current_time.isoformat(),
						community_id=int(community[0]),
					)
					for community in connection.execute(
						"SELECT id FROM communities ORDER BY id"
					).fetchall()
				)
				identity_suggestion_count = sum(
					refresh_identity_suggestions(
						connection, community_id=int(community[0])
					)
					for community in connection.execute(
						"SELECT id FROM communities ORDER BY id"
					).fetchall()
				)
				cohort_baseline_count = sum(
					refresh_community_cohort_baselines(
						connection, community_id=int(community[0]),
						calculated_at=current_time.isoformat(),
					)[0]
					for community in connection.execute(
						"SELECT id FROM communities ORDER BY id"
					).fetchall()
				)
				evaluation_run_id = run_model_evaluation(connection)
				for community in connection.execute(
					"SELECT id FROM communities WHERE status='active' ORDER BY id"
				).fetchall():
					emit_analytics_alerts(
						connection, community_id=int(community[0]),
						calculated_at=current_time.isoformat(),
					)
			for community in connection.execute(
				"SELECT id FROM communities WHERE status='active' ORDER BY id"
			).fetchall():
				dispatch_pending_notifications(
					connection, tenant=TenantContext(int(community[0]))
				)
			record_operational_metric(connection, "analytics.refresh.success", 1.0, observed_at=current_time.isoformat())
		if perform_maintenance:
			record_operational_metric(connection, "maintenance.success", 1.0, observed_at=current_time.isoformat())
	finally:
		connection.close()

	backup_path = backup_metadata_path = ""
	backup_sha256 = ""
	if perform_backup:
		created_path, created_metadata_path, backup_sha256 = create_database_backup(
			settings.database_path, settings.backup_dir, current_time,
			retention_count=settings.backup_retention_count,
		)
		backup_path, backup_metadata_path = str(created_path), str(created_metadata_path)
		metric_connection = connection_factory(settings.database_path)
		try:
			initialize_database(metric_connection)
			record_operational_metric(metric_connection, "backup.success", float(created_path.stat().st_size),
				observed_at=current_time.isoformat())
		finally:
			metric_connection.close()
	return MaintenanceReport(
		deleted_messages=deleted_messages,
		deleted_observations=deleted_observations,
		deleted_audit_log_rows=deleted_audit_rows,
		deleted_signal_runs=deleted_signal_runs,
		deleted_score_runs=deleted_score_runs,
		deleted_processing_jobs=deleted_processing_jobs,
		backup_path=backup_path,
		backup_metadata_path=backup_metadata_path,
		backup_sha256=backup_sha256,
		rollup_rows=rollup_rows,
		topic_count=topic_count,
		graph_node_count=graph_node_count,
		identity_suggestion_count=identity_suggestion_count,
		cohort_baseline_count=cohort_baseline_count,
		evaluation_run_id=evaluation_run_id,
		raw_events_archived=raw_events_archived,
	)


def run_twitch_live_announcement_job(
	settings: AppSettings,
	twitch_token_manager: TwitchTokenManager | None = None,
) -> int:
	require_non_http_surface("job:twitch_live_announcements", guard="system")
	if not settings.discord_bot_token or not settings.twitch_bot_token:
		JOBS_LOGGER.warning(
			"skipping twitch live announcements: missing bot token discord=%s twitch=%s",
			bool(settings.discord_bot_token),
			bool(settings.twitch_bot_token),
		)
		return 0

	twitch_token_manager = twitch_token_manager or _build_twitch_token_manager(settings)
	tenant_targets = _tenant_live_announcement_targets(settings.database_path)
	if not tenant_targets:
		JOBS_LOGGER.warning(
			"skipping twitch live announcements: no active paired tenant installations"
		)
		return 0
	return _run_tenant_twitch_live_announcements(
		settings, twitch_token_manager, tenant_targets
	)


def _tenant_live_announcement_targets(database_path: Path) -> list[sqlite3.Row]:
	connection = connect_database(database_path)
	try:
		initialize_database(connection)
		return list(connection.execute(
			"""SELECT t.community_id,t.id AS twitch_installation_id,
			          json_extract(t.metadata_json,'$.channel_login') AS channel_login,
			          d.id AS discord_installation_id,d.external_community_id AS discord_guild_id
			   FROM community_installations t
			   JOIN community_installations d
			     ON d.community_id=t.community_id AND d.platform='discord' AND d.status='active'
			   WHERE t.platform='twitch' AND t.status='active'
			     AND TRIM(COALESCE(json_extract(t.metadata_json,'$.channel_login'),''))<>''
			   ORDER BY t.community_id,t.id,d.id"""
		).fetchall())
	finally:
		connection.close()


def _run_tenant_twitch_live_announcements(
	settings: AppSettings,
	token_manager: TwitchTokenManager,
	targets: list[sqlite3.Row],
) -> int:
	stream_cache: dict[tuple[int, int], TwitchLiveStream | None] = {}
	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
		for target in targets:
			community_id = int(target["community_id"])
			twitch_installation_id = int(target["twitch_installation_id"])
			channel_login = str(target["channel_login"])
			cache_key = (community_id, twitch_installation_id)
			if cache_key not in stream_cache:
				stream_cache[cache_key] = _fetch_twitch_live_stream(channel_login, token_manager)
				_record_twitch_stream_lifecycle(
					settings.database_path, channel_login, stream_cache[cache_key],
					community_id=community_id,
				)
			stream = stream_cache[cache_key]
			if stream is None:
				continue
			guild_id = str(target["discord_guild_id"])
			guild_channels = _fetch_discord_guild_channels(guild_id, settings.discord_bot_token)
			target_channel_id = _pick_best_discord_channel_for_stream(
				guild_channels, stream.title, stream.game_name, guild_id=guild_id
			)
			if not target_channel_id:
				continue
			for channel in guild_channels:
				upsert_discord_channel(
					connection, guild_id=guild_id,
					channel_id=str(channel.get("id") or "").strip(),
					channel_name=str(channel.get("name") or "").strip(),
					channel_type=int(channel.get("type") or 0),
				)
			title_suffix = f" - {stream.title}" if stream.title else ""
			queue_system_announcement(
				connection, community_id=community_id,
				target_installation_id=int(target["discord_installation_id"]),
				target_external_id=target_channel_id,
				body=f"@here {channel_login} is live: {stream.url}{title_suffix}",
				dedupe_key=f"twitch-live:{twitch_installation_id}:{stream.stream_id}:{target['discord_installation_id']}",
				source={
					"type": "twitch_live", "stream_id": stream.stream_id,
					"twitch_installation_id": twitch_installation_id,
				},
				scheduled_at=datetime.now(UTC).isoformat(),
			)
		return dispatch_due_announcements(
			connection,
			lambda platform, guild_id, channel_id, body, source: _send_scheduled_announcement(
				settings.discord_bot_token, platform, guild_id, channel_id, body, source
			),
		)
	finally:
		connection.close()


def run_scheduled_announcement_job(
	settings: AppSettings,
	*,
	now: datetime | None = None,
) -> int:
	require_non_http_surface("job:scheduled_announcements", guard="system")
	if not settings.discord_bot_token:
		JOBS_LOGGER.warning("scheduled announcements skipped: missing Discord bot token")
		return 0
	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
		return dispatch_due_announcements(
			connection,
			lambda platform, guild_id, channel_id, body, source: _send_scheduled_announcement(
				settings.discord_bot_token, platform, guild_id, channel_id, body, source
			),
			now=now,
		)
	finally:
		connection.close()


def run_onboarding_role_job(settings: AppSettings) -> int:
	require_non_http_surface("job:onboarding_roles", guard="system")
	if not settings.discord_bot_token:
		JOBS_LOGGER.warning("newcomer role assignments skipped: missing Discord bot token")
		return 0
	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
		return dispatch_newcomer_roles(
			connection,
			lambda guild_id, user_id, role_id: _assign_discord_member_role(
				settings.discord_bot_token, guild_id, user_id, role_id
			),
		)
	finally:
		connection.close()


def run_onboarding_checkpoint_job(
	settings: AppSettings, *, now: datetime | None = None
) -> int:
	require_non_http_surface("job:onboarding_checkpoints", guard="system")
	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
		return queue_due_checkpoint_reminders(connection, now=now)
	finally:
		connection.close()


def _announce_stream_to_guilds(
	settings: AppSettings,
	guild_ids: tuple[str, ...],
	twitch_channel_name: str,
	stream: TwitchLiveStream,
	*,
	deduplicate: bool,
) -> int:
	announcements_sent = 0
	for guild_id in guild_ids:
		guild_channels = _fetch_discord_guild_channels(guild_id, settings.discord_bot_token)
		if not guild_channels:
			continue

		connection = connect_database(settings.database_path)
		try:
			initialize_database(connection)
			for channel in guild_channels:
				upsert_discord_channel(
					connection,
					guild_id=guild_id,
					channel_id=str(channel.get("id") or "").strip(),
					channel_name=str(channel.get("name") or "").strip(),
					channel_type=int(channel.get("type") or 0),
				)

			if deduplicate and has_twitch_live_announcement(
				connection,
				twitch_channel_name=twitch_channel_name,
				twitch_stream_id=stream.stream_id,
				discord_guild_id=guild_id,
			):
				continue
		finally:
			connection.close()

		target_channel_id = _pick_best_discord_channel_for_stream(guild_channels, stream.title, stream.game_name, guild_id=guild_id)
		if not target_channel_id:
			continue

		_send_discord_here_announcement(
			bot_token=settings.discord_bot_token,
			channel_id=target_channel_id,
			stream=stream,
			twitch_channel_name=twitch_channel_name,
		)

		if deduplicate:
			connection = connect_database(settings.database_path)
			try:
				initialize_database(connection)
				record_twitch_live_announcement(
					connection,
					twitch_channel_name=twitch_channel_name,
					twitch_stream_id=stream.stream_id,
					discord_guild_id=guild_id,
					discord_channel_id=target_channel_id,
				)
			finally:
				connection.close()
		announcements_sent += 1

	return announcements_sent


def _record_twitch_stream_lifecycle(
	database_path: Path,
	channel_name: str,
	stream: TwitchLiveStream | None,
	*,
	community_id: int,
) -> None:
	"""Project polling changes into the common observation/analysis pipeline."""
	connection = connect_database(database_path)
	try:
		initialize_database(connection)
		latest = connection.execute(
			"""SELECT event_type,external_event_id,attributes_json FROM observations
			   WHERE platform='twitch' AND context_id=? AND community_id=?
			     AND event_type LIKE 'stream.%'
			   ORDER BY occurred_at DESC,id DESC LIMIT 1""",
			(channel_name, int(community_id)),
		).fetchone()
		if stream is None:
			if latest is None or str(latest[0]) == "stream.ended":
				return
			previous = json.loads(str(latest[2] or "{}"))
			event_type = "stream.ended"
			stream_id = str(previous.get("stream_id") or "unknown")
			attributes = {"stream_id": stream_id, "channel_name": channel_name}
		else:
			previous = json.loads(str(latest[2] or "{}")) if latest is not None else {}
			if latest is None or str(latest[0]) == "stream.ended" or previous.get("stream_id") != stream.stream_id:
				event_type = "stream.started"
			elif previous.get("title") != stream.title or previous.get("game_name") != stream.game_name:
				event_type = "stream.updated"
			else:
				return
			stream_id = stream.stream_id
			attributes = {"stream_id": stream.stream_id, "channel_name": channel_name,
				"title": stream.title, "game_name": stream.game_name, "url": stream.url}
		collect_observation(connection, Observation(
			platform="twitch", event_type=event_type,
			external_event_id=f"poll:{channel_name}:{stream_id}:{event_type}:{hashlib.sha256(json.dumps(attributes, sort_keys=True).encode()).hexdigest()[:12]}",
			actor_platform_user_id=channel_name, actor_username=channel_name,
			container_id=channel_name, context_id=channel_name,
			text=stream.title if stream is not None else None,
			occurred_at=datetime.now(UTC).isoformat(), attributes=attributes,
			community_id=int(community_id),
		))
	finally:
		connection.close()


def send_manual_twitch_live_announcements(settings: AppSettings) -> int:
	if not settings.discord_bot_token or not settings.twitch_bot_token:
		JOBS_LOGGER.warning(
			"manual go-live skipped: missing bot token discord=%s twitch=%s",
			bool(settings.discord_bot_token),
			bool(settings.twitch_bot_token),
		)
		return 0

	guild_ids = _resolve_target_discord_guild_ids(settings.discord_bot_token, settings.discord_guild_ids)
	if not guild_ids:
		JOBS_LOGGER.warning("manual go-live skipped: no connected Discord guilds discovered")
		return 0

	twitch_token_manager = _build_twitch_token_manager(settings)
	announcements_sent = 0
	for twitch_channel_name in settings.twitch_channels:
		stream = _fetch_twitch_live_stream(twitch_channel_name, twitch_token_manager)
		if stream is None:
			JOBS_LOGGER.info("manual go-live skipped: no active twitch stream for channel=%s", twitch_channel_name)
			continue
		announcements_sent += _announce_stream_to_guilds(
			settings, guild_ids, twitch_channel_name, stream, deduplicate=False
		)

	return announcements_sent


def _resolve_target_discord_guild_ids(discord_bot_token: str, configured_guild_ids: tuple[str, ...]) -> tuple[str, ...]:
	resolved_ids: list[str] = []
	seen_ids: set[str] = set()

	for guild_id in configured_guild_ids:
		cleaned_id = guild_id.strip()
		if not cleaned_id or cleaned_id in seen_ids:
			continue
		resolved_ids.append(cleaned_id)
		seen_ids.add(cleaned_id)

	discovered_ids = _fetch_discord_bot_guild_ids(discord_bot_token)
	for guild_id in discovered_ids:
		cleaned_id = guild_id.strip()
		if not cleaned_id or cleaned_id in seen_ids:
			continue
		resolved_ids.append(cleaned_id)
		seen_ids.add(cleaned_id)

	return tuple(resolved_ids)


def _fetch_discord_bot_guild_ids(discord_bot_token: str) -> tuple[str, ...]:
	token = discord_bot_token.removeprefix("Bot ").strip()
	request = Request(
		"https://discord.com/api/v10/users/@me/guilds",
		headers={
			"Authorization": f"Bot {token}",
			"Accept": "application/json",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		},
	)
	try:
		with urlopen(request, timeout=15) as response:
			payload = json.loads(response.read().decode("utf-8"))
	except (HTTPError, URLError):
		return ()
	if not isinstance(payload, list):
		return ()
	return tuple(str(item.get("id") or "").strip() for item in payload if isinstance(item, dict) and str(item.get("id") or "").strip())


def purge_expired_messages(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
	*,
	community_id: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"""DELETE FROM messages WHERE community_id=? AND datetime(sent_at) < datetime(?)
			   AND NOT EXISTS (
			       SELECT 1 FROM legal_holds
			       WHERE legal_holds.community_id=messages.community_id
			         AND legal_holds.status='active'
			   )""",
			(int(community_id), cutoff),
		)
	return int(cursor.rowcount or 0)

def purge_expired_observations(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
	*,
	community_id: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)

	with connection:
		cursor = connection.execute(
			"""
			DELETE FROM observations WHERE community_id=? AND occurred_at < ?
				AND NOT EXISTS (
					SELECT 1 FROM legal_holds
					WHERE legal_holds.community_id=observations.community_id
					  AND legal_holds.status='active'
				)
				AND id NOT IN (
					SELECT observation_id
					FROM messages
					WHERE observation_id IS NOT NULL
				)
			""",
			(int(community_id), cutoff),
		)

	return int(cursor.rowcount or 0)


def purge_expired_audit_log(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"DELETE FROM audit_log WHERE created_at < ?",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def purge_expired_signal_runs(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"""
			DELETE FROM signal_calculation_runs
			WHERE datetime(calculated_at) < datetime(?)
			""",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def purge_expired_score_runs(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"""
			DELETE FROM social_score_runs
			WHERE datetime(calculated_at) < datetime(?)
			  AND id NOT IN (
				SELECT MAX(id)
				FROM social_score_runs
				GROUP BY user_id
			  )
			""",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def purge_expired_processing_jobs(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"""
			DELETE FROM processing_jobs
			WHERE status IN ('completed', 'failed', 'cancelled')
			  AND datetime(COALESCE(completed_at, updated_at, created_at)) < datetime(?)
			""",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def refresh_metrics_rollups(connection: sqlite3.Connection, now: datetime) -> int:
	bucket_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
	rollups = {
		"messages_total": connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
		"open_reviews": connection.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0],
		"pending_actions": connection.execute("SELECT COUNT(*) FROM moderation_actions WHERE status = 'pending'").fetchone()[0],
		"observations_total": connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
		"observations_24h": connection.execute("SELECT COUNT(*) FROM observations WHERE occurred_at >= datetime(?, '-1 day')", (now.astimezone(UTC).isoformat(),),).fetchone()[0],
	}
	with connection:
		for metric_name, value in rollups.items():
			connection.execute(
				"""
				INSERT INTO metrics_rollups (
					metric_name,
					bucket_start,
					bucket_size,
					dimension_json,
					value
				) VALUES (?, ?, '1d', '{}', ?)
				ON CONFLICT(metric_name, bucket_start, bucket_size, dimension_json)
				DO UPDATE SET value = excluded.value, created_at = CURRENT_TIMESTAMP
				""",
				(metric_name, bucket_start, float(value)),
			)
	return len(rollups)


def create_database_backup(
	database_path: Path | str,
	backup_dir: Path,
	now: datetime,
	*,
	retention_count: int = 48,
) -> tuple[Path, Path, str]:
	"""Create a transactionally consistent SQLite backup and prune old generations."""
	target = DatabaseTarget.parse(database_path)
	if target.backend != "sqlite":
		raise NotImplementedError(
			"PostgreSQL backups require the native backup workflow from DF4-06"
		)
	database_path = Path(target.value)
	backup_dir.mkdir(parents=True, exist_ok=True)
	timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
	backup_path = backup_dir / f"{database_path.stem}-{timestamp}{database_path.suffix}"
	source = sqlite3.connect(database_path)
	destination = sqlite3.connect(backup_path)
	try:
		source.backup(destination)
		destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
		integrity = destination.execute("PRAGMA integrity_check").fetchone()
		if integrity is None or str(integrity[0]).casefold() != "ok":
			raise sqlite3.DatabaseError("backup integrity check failed")
	finally:
		destination.close()
		source.close()
	backup_sha256 = _sha256sum(backup_path)
	metadata = {
		"created_at": now.astimezone(UTC).isoformat(),
		"database_path": str(database_path),
		"backup_path": str(backup_path),
		"sha256": backup_sha256,
		"size_bytes": backup_path.stat().st_size,
	}
	backup_metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
	backup_metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
	_generations = sorted(
		backup_dir.glob(f"{database_path.stem}-*{database_path.suffix}"),
		key=lambda path: path.stat().st_mtime,
		reverse=True,
	)
	for expired in _generations[max(1, retention_count):]:
		expired.unlink(missing_ok=True)
		expired.with_suffix(expired.suffix + ".json").unlink(missing_ok=True)
	return backup_path, backup_metadata_path, backup_sha256


def restore_database_backup(
	backup_path: Path,
	restore_path: Path,
	*,
	expected_sha256: str,
) -> dict[str, object]:
	"""Restore a backup to a separate path and verify integrity and table counts."""
	if backup_path.resolve() == restore_path.resolve():
		raise ValueError("restore path must be isolated from the backup")
	if restore_path.exists():
		raise FileExistsError("restore path already exists")
	actual_sha256 = _sha256sum(backup_path)
	if not hmac.compare_digest(actual_sha256, expected_sha256.strip().casefold()):
		raise ValueError("backup checksum mismatch")
	restore_path.parent.mkdir(parents=True, exist_ok=True)
	source = sqlite3.connect(backup_path)
	destination = sqlite3.connect(restore_path)
	try:
		source.backup(destination)
		integrity = destination.execute("PRAGMA integrity_check").fetchone()
		if integrity is None or str(integrity[0]).casefold() != "ok":
			raise sqlite3.DatabaseError("restored database integrity check failed")
		tables = [
			str(row[0]) for row in source.execute(
				"""SELECT name FROM sqlite_master
				   WHERE type='table' AND name NOT LIKE 'sqlite_%'
				   ORDER BY name"""
			).fetchall()
		]
		source_counts = {
			table: int(source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
			for table in tables
		}
		restored_counts = {
			table: int(destination.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
			for table in tables
		}
		if source_counts != restored_counts:
			raise sqlite3.DatabaseError("restored database row counts do not match backup")
	finally:
		destination.close()
		source.close()
	return {
		"integrity": "ok",
		"sha256": actual_sha256,
		"table_counts": restored_counts,
		"restore_path": str(restore_path),
	}


def _cutoff_timestamp(now: datetime, retention_days: int) -> str:
	return (now.astimezone(UTC) - timedelta(days=retention_days)).isoformat()


def _sha256sum(path: Path) -> str:
	hasher = hashlib.sha256()
	with path.open("rb") as file_handle:
		for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
			hasher.update(chunk)
	return hasher.hexdigest()



def _build_twitch_token_manager(settings: AppSettings) -> TwitchTokenManager:
	return TwitchTokenManager(
		initial_access_token=settings.twitch_bot_token or "",
		refresh_token=settings.twitch_refresh_token,
		client_id=settings.twitch_client_id,
		client_secret=settings.twitch_client_secret,
		on_token_refresh=persist_refreshed_twitch_tokens,
		logger=JOBS_LOGGER,
	)


def _fetch_twitch_live_stream(channel_name: str, token_manager: TwitchTokenManager) -> TwitchLiveStream | None:
	try:
		validation = token_manager.validate_token()
	except TwitchAuthError:
		return None

	access_token = validation.access_token
	client_id = validation.client_id

	query = urlencode({"user_login": channel_name.strip().casefold()})

	def _build_streams_request(token: str, validated_client_id: str) -> Request:
		return Request(
			f"https://api.twitch.tv/helix/streams?{query}",
			headers={
				"Authorization": f"Bearer {token}",
				"Client-Id": validated_client_id,
			},
		)

	streams_request = _build_streams_request(access_token, client_id)
	try:
		with urlopen(streams_request, timeout=15) as response:
			streams_payload = json.loads(response.read().decode("utf-8"))
	except HTTPError as exc:
		if exc.code == 401 and token_manager.can_refresh():
			try:
				token_manager.refresh_access_token()
				validation = token_manager.validate_token()
			except TwitchAuthError:
				return None
			streams_request = _build_streams_request(validation.access_token, validation.client_id)
			try:
				with urlopen(streams_request, timeout=15) as response:
					streams_payload = json.loads(response.read().decode("utf-8"))
			except (HTTPError, URLError):
				return None
		else:
			return None
	except URLError:
		return None

	items = streams_payload.get("data")
	if not isinstance(items, list) or not items:
		return None
	item = items[0] if isinstance(items[0], dict) else {}
	stream_id = str(item.get("id") or "").strip()
	title = str(item.get("title") or "").strip()
	if not stream_id:
		return None
	return TwitchLiveStream(
		stream_id=stream_id,
		title=title,
		url=f"https://www.twitch.tv/{channel_name.strip().casefold()}",
		game_name=str(item.get("game_name") or "").strip(),
	)


def _fetch_discord_guild_channels(guild_id: str, discord_bot_token: str) -> list[dict[str, object]]:
	token = discord_bot_token.removeprefix("Bot ").strip()
	request = Request(
		f"https://discord.com/api/v10/guilds/{guild_id}/channels",
		headers={
			"Authorization": f"Bot {token}",
			"Accept": "application/json",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		},
	)
	try:
		with urlopen(request, timeout=15) as response:
			payload = json.loads(response.read().decode("utf-8"))
	except (HTTPError, URLError):
		return []
	if not isinstance(payload, list):
		return []
	channels: list[dict[str, object]] = []
	for item in payload:
		if not isinstance(item, dict):
			continue
		channel_type = int(item.get("type") or 0)
		if channel_type not in {0, 5}:
			continue
		channel_id = str(item.get("id") or "").strip()
		channel_name = str(item.get("name") or "").strip()
		if channel_id and channel_name:
			channels.append(item)
	return channels

def _pick_best_discord_channel_for_stream(
	channels: list[dict[str, object]],
	stream_title: str,
	game_name: str,
	guild_id: str,
) -> str | None:
	channels = [
		channel
		for channel in channels
		if not _everyone_cannot_view(channel, guild_id)
	]

	title_tokens = _tokenize(stream_title)
	game_tokens = _tokenize(game_name)

	best_channel_id: str | None = None
	best_score = -1
	for channel in channels:
		channel_id = str(channel.get("id") or "").strip()
		channel_name = str(channel.get("name") or "").strip().casefold()
		if not channel_id or not channel_name:
			continue
		name_tokens = _tokenize(channel_name)
		overlap_title = len(title_tokens & name_tokens)
		overlap_game = len(game_tokens & name_tokens)
		score = overlap_title * 3 + overlap_game * 2
		if score > best_score:
			best_score = score
			best_channel_id = channel_id

	if best_score > 0 and best_channel_id is not None:
		return best_channel_id

	for fallback_name in ("general", "lounge"):
		for channel in channels:
			channel_name = str(channel.get("name") or "").strip().casefold()
			if channel_name == fallback_name:
				return str(channel.get("id") or "").strip() or None

	for fallback_name in ("general", "lounge"):
		for channel in channels:
			channel_name = str(channel.get("name") or "").strip().casefold()
			if fallback_name in channel_name:
				return str(channel.get("id") or "").strip() or None

	if channels:
		return str(channels[0].get("id") or "").strip() or None
	return None


def _tokenize(text: str) -> set[str]:
	tokens = {
		token
		for token in re.split(r"[^a-z0-9]+", text.casefold())
		if len(token) >= 3 and token not in _CHANNEL_MATCH_STOPWORDS
	}
	return tokens


def _send_discord_here_announcement(
	*, bot_token: str, channel_id: str, stream: TwitchLiveStream, twitch_channel_name: str
) -> None:
	token = bot_token.removeprefix("Bot ").strip()
	title_suffix = f" - {stream.title}" if stream.title else ""
	payload = {
		"allowed_mentions": {"parse": ["everyone"]},
		"content": f"@here {twitch_channel_name} is live: {stream.url}{title_suffix}",
	}
	request = Request(
		f"https://discord.com/api/v10/channels/{channel_id}/messages",
		data=json.dumps(payload).encode("utf-8"),
		headers={
			"Authorization": f"Bot {token}",
			"Accept": "application/json",
			"Content-Type": "application/json",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		},
		method="POST",
	)
	with urlopen(request, timeout=15) as response:
		response.read()


def _send_scheduled_announcement(
	bot_token: str,
	platform: str,
	guild_id: str,
	channel_id: str,
	body: str,
	source: dict[str, object],
) -> str | None:
	if platform != "discord":
		raise ValueError("scheduled Twitch announcements are not configured")
	channels = _fetch_discord_guild_channels(guild_id, bot_token)
	if channel_id not in {str(channel.get("id") or "").strip() for channel in channels}:
		raise ValueError("announcement channel does not belong to the active installation")
	token = bot_token.removeprefix("Bot ").strip()
	allowed_mentions: dict[str, object] = {"parse": []}
	if source.get("type") == "twitch_live":
		allowed_mentions = {"parse": ["everyone"]}
	elif source.get("type") in {
		"member_welcome", "onboarding_checkpoint_reminder", "member_verification_resource",
	} and str(source.get("user_id") or "").strip():
		allowed_mentions = {"parse": [], "users": [str(source["user_id"]).strip()]}
	request = Request(
		f"https://discord.com/api/v10/channels/{channel_id}/messages",
		data=json.dumps({
			"content": body,
			"allowed_mentions": allowed_mentions,
		}).encode("utf-8"),
		headers={
			"Authorization": f"Bot {token}",
			"Accept": "application/json",
			"Content-Type": "application/json",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		},
		method="POST",
	)
	with urlopen(request, timeout=15) as response:
		payload = json.loads(response.read().decode("utf-8") or "{}")
	return str(payload.get("id") or "").strip() or None


def _assign_discord_member_role(
	bot_token: str, guild_id: str, user_id: str, role_id: str
) -> None:
	token = bot_token.removeprefix("Bot ").strip()
	request = Request(
		f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
		data=b"",
		headers={
			"Authorization": f"Bot {token}",
			"Content-Length": "0",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		},
		method="PUT",
	)
	with urlopen(request, timeout=15) as response:
		response.read()
