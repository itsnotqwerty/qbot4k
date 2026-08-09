from __future__ import annotations

import hashlib
import json
import logging
import shutil
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
from .db import (
	connect_database,
	has_twitch_live_announcement,
	initialize_database,
	record_twitch_live_announcement,
	upsert_discord_channel,
)
from .permissions import _everyone_cannot_view
from .token_store import persist_refreshed_twitch_tokens
from .twitch_auth import TwitchAuthError, TwitchTokenManager


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
	deleted_audit_log_rows: int
	backup_path: str
	backup_metadata_path: str
	backup_sha256: str
	rollup_rows: int


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
) -> MaintenanceReport:
	current_time = now or datetime.now(UTC)
	connection = connection_factory(settings.database_path)
	try:
		initialize_database(connection)
		deleted_messages = purge_expired_messages(connection, current_time, settings.message_retention_days)
		deleted_audit_rows = purge_expired_audit_log(connection, current_time, settings.audit_retention_days)
		rollup_rows = refresh_metrics_rollups(connection, current_time)
	finally:
		connection.close()

	backup_path, backup_metadata_path, backup_sha256 = create_database_backup(
		settings.database_path,
		settings.backup_dir,
		current_time,
	)
	return MaintenanceReport(
		deleted_messages=deleted_messages,
		deleted_audit_log_rows=deleted_audit_rows,
		backup_path=str(backup_path),
		backup_metadata_path=str(backup_metadata_path),
		backup_sha256=backup_sha256,
		rollup_rows=rollup_rows,
	)


def run_twitch_live_announcement_job(settings: AppSettings) -> int:
	if not settings.discord_bot_token or not settings.twitch_bot_token:
		JOBS_LOGGER.warning(
			"skipping twitch live announcements: missing bot token discord=%s twitch=%s",
			bool(settings.discord_bot_token),
			bool(settings.twitch_bot_token),
		)
		return 0

	guild_ids = _resolve_target_discord_guild_ids(settings.discord_bot_token, settings.discord_guild_ids)
	if not guild_ids:
		JOBS_LOGGER.warning("skipping twitch live announcements: no connected Discord guilds discovered")
		return 0

	twitch_token_manager = _build_twitch_token_manager(settings)
	stream = _fetch_twitch_live_stream("its_not_qwerty", twitch_token_manager)
	if stream is None:
		JOBS_LOGGER.info("no active twitch stream detected for channel=%s", "its_not_qwerty")
		return 0

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

			if has_twitch_live_announcement(
				connection,
				twitch_channel_name="its_not_qwerty",
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
		)

		connection = connect_database(settings.database_path)
		try:
			initialize_database(connection)
			record_twitch_live_announcement(
				connection,
				twitch_channel_name="its_not_qwerty",
				twitch_stream_id=stream.stream_id,
				discord_guild_id=guild_id,
				discord_channel_id=target_channel_id,
			)
		finally:
			connection.close()
		announcements_sent += 1

	return announcements_sent


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
	stream = _fetch_twitch_live_stream("its_not_qwerty", twitch_token_manager)
	if stream is None:
		JOBS_LOGGER.info("manual go-live skipped: no active twitch stream for channel=%s", "its_not_qwerty")
		return 0

	announcements_sent = 0
	for guild_id in guild_ids:
		guild_channels = _fetch_discord_guild_channels(guild_id, settings.discord_bot_token)
		if not guild_channels:
			continue

		target_channel_id = _pick_best_discord_channel_for_stream(guild_channels, stream.title, stream.game_name, guild_id=guild_id)
		if not target_channel_id:
			continue

		_send_discord_here_announcement(
			bot_token=settings.discord_bot_token,
			channel_id=target_channel_id,
			stream=stream,
		)
		announcements_sent += 1

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
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"DELETE FROM messages WHERE created_at < ?",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)

def purge_expired_observations(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)

	with connection:
		cursor = connection.execute(
			"""
			DELETE FROM observations WHERE occurred_at < ?
				AND id NOT IN (
					SELECT observation_id
					FROM messages
					WHERE observation_id IS NOT NULL
				)
			""",
			(cutoff,),
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


def create_database_backup(database_path: Path, backup_dir: Path, now: datetime) -> tuple[Path, Path, str]:
	backup_dir.mkdir(parents=True, exist_ok=True)
	timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
	backup_path = backup_dir / f"{database_path.stem}-{timestamp}{database_path.suffix}"
	shutil.copy2(database_path, backup_path)
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
	return backup_path, backup_metadata_path, backup_sha256


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


def _send_discord_here_announcement(*, bot_token: str, channel_id: str, stream: TwitchLiveStream) -> None:
	token = bot_token.removeprefix("Bot ").strip()
	title_suffix = f" - {stream.title}" if stream.title else ""
	payload = {
		"allowed_mentions": {"parse": ["everyone"]},
		"content": f"@here its_not_qwerty is live: {stream.url}{title_suffix}",
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
