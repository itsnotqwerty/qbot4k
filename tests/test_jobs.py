from __future__ import annotations

import json
import io
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlparse, parse_qs
from unittest import mock

from src.config import AppSettings
from src.db import connect_database, initialize_database
from src.jobs import run_maintenance_jobs, run_twitch_live_announcement_job


class JobTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tempdir = TemporaryDirectory()
		self.database_path = Path(self.tempdir.name) / "jobs.sqlite3"
		self.backup_dir = Path(self.tempdir.name) / "backups"
		self.settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs",
				"QBOT_MESSAGE_RETENTION_DAYS": "1",
				"QBOT_AUDIT_RETENTION_DAYS": "1",
			}
		)

	def tearDown(self) -> None:
		self.tempdir.cleanup()

	def test_maintenance_jobs_purge_old_rows_backup_and_rollups(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"""
					INSERT INTO users (primary_display_name) VALUES ('sam')
					"""
				)
				connection.execute(
					"""
					INSERT INTO platform_accounts (platform, platform_user_id, username, guild_or_channel_context)
					VALUES ('discord', 'user-1', 'sam', 'guild-1')
					"""
				)
				connection.execute(
					"""
					INSERT INTO messages (
						platform,
						platform_message_id,
						platform_account_id,
						channel_id,
						content_raw,
						content_normalized,
						sent_at,
						created_at
					) VALUES ('discord', 'msg-1', 1, 'channel-1', 'hello', 'hello', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')
					"""
				)
				connection.execute(
					"""
					INSERT INTO audit_log (actor_type, action_type, entity_type, payload_json, created_at)
					VALUES ('system', 'seed', 'test', '{}', '2026-08-01T00:00:00+00:00')
					"""
				)
		finally:
			connection.close()

		report = run_maintenance_jobs(
			self.settings,
			now=datetime(2026, 8, 6, tzinfo=UTC),
		)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
			audit_count = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
			rollups = connection.execute(
				"SELECT metric_name, value FROM metrics_rollups ORDER BY metric_name"
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(report.deleted_messages, 1)
		self.assertEqual(report.deleted_audit_log_rows, 1)
		self.assertEqual(report.rollup_rows, 5)
		self.assertEqual(message_count, 0)
		self.assertEqual(audit_count, 0)
		self.assertEqual([row[0] for row in rollups], ["messages_total", "observations_24h", "observations_total", "open_reviews", "pending_actions"])
		self.assertEqual([row[1] for row in rollups], [0.0, 0.0, 0.0, 0.0, 0.0])

		backup_path = Path(report.backup_path)
		metadata_path = Path(report.backup_metadata_path)
		self.assertTrue(backup_path.exists())
		self.assertTrue(metadata_path.exists())
		metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
		self.assertEqual(metadata["backup_path"], report.backup_path)
		self.assertEqual(metadata["sha256"], report.backup_sha256)

	def test_twitch_live_announcement_posts_here_and_dedupes(self) -> None:
		settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
				"QBOT_MESSAGE_RETENTION_DAYS": "1",
				"QBOT_AUDIT_RETENTION_DAYS": "1",
				"QBOT_TWITCH_BOT_TOKEN": "oauth:test-token",
				"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
				"QBOT_DISCORD_GUILD_IDS": "guild-1",
			}
		)

		class _FakeResponse:
			def __init__(self, payload: object) -> None:
				self._payload = payload

			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, tb) -> None:
				return None

			def read(self) -> bytes:
				return json.dumps(self._payload).encode("utf-8")

		post_calls: list[str] = []

		def _fake_urlopen(request, timeout=15):
			url = request.full_url
			parsed = urlparse(url)
			if parsed.netloc == "id.twitch.tv" and parsed.path == "/oauth2/validate":
				return _FakeResponse({"client_id": "twitch-client-id", "login": "its_not_qwerty"})
			if parsed.netloc == "api.twitch.tv" and parsed.path == "/helix/streams":
				query = parse_qs(parsed.query)
				self.assertEqual(query.get("user_login", [""])[0], "its_not_qwerty")
				return _FakeResponse(
					{
						"data": [
							{
								"id": "stream-1",
								"title": "Late Night Coding Session",
								"game_name": "Software and Game Development",
							}
						]
					}
				)
			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/users/@me/guilds":
				return _FakeResponse([])
			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/guilds/guild-1/channels":
				return _FakeResponse(
					[
						{"id": "c-1", "name": "general", "type": 0},
						{"id": "c-2", "name": "coding", "type": 0},
					]
				)
			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/channels/c-2/messages":
				post_calls.append(request.data.decode("utf-8"))
				return _FakeResponse({"id": "discord-message-1"})
			raise AssertionError(f"Unexpected URL in test: {url}")

		with mock.patch("src.jobs.urlopen", side_effect=_fake_urlopen):
			with mock.patch("src.twitch_auth.urlopen", side_effect=_fake_urlopen):
				first_count = run_twitch_live_announcement_job(settings)
				second_count = run_twitch_live_announcement_job(settings)

		self.assertEqual(first_count, 1)
		self.assertEqual(second_count, 0)
		self.assertEqual(len(post_calls), 1)
		self.assertIn("@here its_not_qwerty is live", post_calls[0])
		self.assertIn("https://www.twitch.tv/its_not_qwerty", post_calls[0])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			cache_rows = connection.execute(
				"SELECT channel_id, channel_name FROM discord_channels ORDER BY channel_id"
			).fetchall()
			announcement_rows = connection.execute(
				"SELECT twitch_stream_id, discord_guild_id, discord_channel_id FROM twitch_live_announcements"
			).fetchall()
		finally:
			connection.close()

		self.assertEqual([(row[0], row[1]) for row in cache_rows], [("c-1", "general"), ("c-2", "coding")])
		self.assertEqual([(row[0], row[1], row[2]) for row in announcement_rows], [("stream-1", "guild-1", "c-2")])

	def test_twitch_live_announcement_refreshes_expired_access_token(self) -> None:
		settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
				"QBOT_MESSAGE_RETENTION_DAYS": "1",
				"QBOT_AUDIT_RETENTION_DAYS": "1",
				"QBOT_TWITCH_BOT_TOKEN": "oauth:expired-token",
				"QBOT_TWITCH_REFRESH_TOKEN": "refresh-token",
				"QBOT_TWITCH_CLIENT_ID": "configured-client-id",
				"QBOT_TWITCH_CLIENT_SECRET": "configured-client-secret",
				"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
				"QBOT_DISCORD_GUILD_IDS": "guild-1",
			}
		)

		class _FakeResponse:
			def __init__(self, payload: object) -> None:
				self._payload = payload

			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, tb) -> None:
				return None

			def read(self) -> bytes:
				return json.dumps(self._payload).encode("utf-8")

		refresh_posts = 0
		helix_auth_headers: list[str] = []

		def _fake_urlopen(request, timeout=15):
			nonlocal refresh_posts
			url = request.full_url
			parsed = urlparse(url)
			headers = dict(request.header_items())

			if parsed.netloc == "id.twitch.tv" and parsed.path == "/oauth2/validate":
				authorization = headers.get("Authorization", "")
				if authorization == "OAuth expired-token":
					raise HTTPError(url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"{}"))
				if authorization == "OAuth refreshed-token":
					return _FakeResponse({"client_id": "resolved-client-id", "login": "its_not_qwerty"})
				raise AssertionError(f"Unexpected validate authorization header: {authorization}")

			if parsed.netloc == "id.twitch.tv" and parsed.path == "/oauth2/token":
				refresh_posts += 1
				request_form = parse_qs((request.data or b"").decode("utf-8"))
				self.assertEqual(request_form.get("grant_type", [""])[0], "refresh_token")
				self.assertEqual(request_form.get("refresh_token", [""])[0], "refresh-token")
				self.assertEqual(request_form.get("client_id", [""])[0], "configured-client-id")
				self.assertEqual(request_form.get("client_secret", [""])[0], "configured-client-secret")
				return _FakeResponse({"access_token": "refreshed-token", "refresh_token": "rotated-refresh-token"})

			if parsed.netloc == "api.twitch.tv" and parsed.path == "/helix/streams":
				helix_auth_headers.append(headers.get("Authorization", ""))
				return _FakeResponse(
					{
						"data": [
							{
								"id": "stream-2",
								"title": "Refreshed Session",
								"game_name": "Software and Game Development",
							}
						]
					}
				)

			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/users/@me/guilds":
				return _FakeResponse([])

			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/guilds/guild-1/channels":
				return _FakeResponse([{"id": "c-1", "name": "general", "type": 0}])

			if parsed.netloc == "discord.com" and parsed.path == "/api/v10/channels/c-1/messages":
				return _FakeResponse({"id": "discord-message-2"})

			raise AssertionError(f"Unexpected URL in test: {url}")

		with mock.patch("src.jobs.urlopen", side_effect=_fake_urlopen):
			with mock.patch("src.twitch_auth.urlopen", side_effect=_fake_urlopen):
				sent_count = run_twitch_live_announcement_job(settings)

		self.assertEqual(sent_count, 1)
		self.assertEqual(refresh_posts, 1)
		self.assertEqual(helix_auth_headers, ["Bearer refreshed-token"])
