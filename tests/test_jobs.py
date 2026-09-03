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
from src.intelligence.announcements import (
	approve_announcement,
	cancel_announcement,
	create_announcement,
	dispatch_due_announcements,
	preview_announcement,
	retry_announcement,
)
from src.intelligence.community import (
	create_community,
	create_organization,
	create_workspace,
	register_installation,
)
from src.jobs import (
	TwitchLiveStream,
	_send_scheduled_announcement,
	create_database_backup,
	restore_database_backup,
	run_maintenance_jobs,
	run_onboarding_role_job,
	run_scheduled_announcement_job,
	run_twitch_live_announcement_job,
)


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
					"UPDATE community_policy_settings SET message_retention_days=1 WHERE community_id=1"
				)
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
						community_id,
						platform,
						platform_message_id,
						platform_account_id,
						channel_id,
						content_raw,
						content_normalized,
						sent_at,
						created_at
					) VALUES (1, 'discord', 'msg-1', 1, 'channel-1', 'hello', 'hello', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')
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
			audit_actions = [str(row[0]) for row in connection.execute(
				"SELECT action_type FROM audit_log ORDER BY id"
			).fetchall()]
			rollups = connection.execute(
				"SELECT metric_name, value FROM metrics_rollups ORDER BY metric_name"
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(report.deleted_messages, 1)
		self.assertEqual(report.deleted_audit_log_rows, 1)
		self.assertEqual(report.rollup_rows, 5)
		self.assertEqual(message_count, 0)
		self.assertEqual(audit_count, 1)
		self.assertEqual(audit_actions, ["social_score.calculated"])
		self.assertEqual([row[0] for row in rollups], ["messages_total", "observations_24h", "observations_total", "open_reviews", "pending_actions"])
		self.assertEqual([row[1] for row in rollups], [0.0, 0.0, 0.0, 0.0, 0.0])

		backup_path = Path(report.backup_path)
		metadata_path = Path(report.backup_metadata_path)
		self.assertTrue(backup_path.exists())
		self.assertTrue(metadata_path.exists())
		metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
		self.assertEqual(metadata["backup_path"], report.backup_path)
		self.assertEqual(metadata["sha256"], report.backup_sha256)

	def test_isolated_backup_restore_drill_preserves_tenant_rows(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			organization_id = create_organization(
				connection, name="Backup Drill", slug="backup-drill",
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Backup Drill", slug="backup-drill",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Second Tenant", slug="second-tenant",
			)
			with connection:
				connection.execute(
					"INSERT INTO legal_holds(community_id,reason) VALUES (1,'first tenant')"
				)
				connection.execute(
					"INSERT INTO legal_holds(community_id,reason) VALUES (?,'second tenant')",
					(community_id,),
				)
		finally:
			connection.close()
		backup_path, _, digest = create_database_backup(
			self.database_path, self.backup_dir,
			datetime(2026, 9, 2, tzinfo=UTC),
		)
		restore_path = Path(self.tempdir.name) / "restore" / "drill.sqlite3"

		result = restore_database_backup(
			backup_path, restore_path, expected_sha256=digest,
		)

		self.assertEqual(result["integrity"], "ok")
		self.assertEqual(result["table_counts"]["legal_holds"], 2)
		restored = connect_database(restore_path)
		try:
			owners = [int(row[0]) for row in restored.execute(
				"SELECT community_id FROM legal_holds ORDER BY community_id"
			).fetchall()]
		finally:
			restored.close()
		self.assertEqual(owners, [1, community_id])
		with self.assertRaises(ValueError):
			restore_database_backup(
				backup_path, backup_path, expected_sha256=digest,
			)

	def test_maintenance_applies_message_retention_per_community(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			organization_id = create_organization(
				connection, name="Retention Organization", slug="retention-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Retention Workspace", slug="retention-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Long Retention", slug="long-retention",
			)
			with connection:
				platform_account_id = int(connection.execute(
					"""INSERT INTO platform_accounts(platform,platform_user_id,username)
					   VALUES ('discord','retention-user','Retention User')"""
				).lastrowid)
				connection.execute(
					"UPDATE community_policy_settings SET message_retention_days=1 WHERE community_id=1"
				)
				connection.execute(
					"UPDATE community_policy_settings SET message_retention_days=30 WHERE community_id=?",
					(community_id,),
				)
				for tenant_id, message_id in ((1, "short-lived"), (community_id, "retained")):
					connection.execute(
						"""INSERT INTO messages(
						       community_id,platform,platform_message_id,platform_account_id,channel_id,
						       content_raw,content_normalized,sent_at
						   ) VALUES (?,'discord',?,?,'channel','message','message','2026-08-01T00:00:00+00:00')""",
						(tenant_id, message_id, platform_account_id),
					)
		finally:
			connection.close()

		report = run_maintenance_jobs(
			self.settings, now=datetime(2026, 8, 6, tzinfo=UTC),
			perform_analytics=False, perform_backup=False,
		)

		connection = connect_database(self.database_path)
		try:
			remaining = connection.execute(
				"SELECT community_id,platform_message_id FROM messages ORDER BY community_id"
			).fetchall()
		finally:
			connection.close()
		self.assertEqual(report.deleted_messages, 1)
		self.assertEqual([tuple(row) for row in remaining], [(community_id, "retained")])

	def test_twitch_live_announcement_posts_here_and_dedupes(self) -> None:
		settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
				"QBOT_AUDIT_RETENTION_DAYS": "1",
				"QBOT_TWITCH_BOT_TOKEN": "oauth:test-token",
				"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
				"QBOT_DISCORD_GUILD_IDS": "guild-1",
			}
		)
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			register_installation(
				connection, community_id=1, platform="twitch",
				external_community_id="broadcaster-1", display_name="its_not_qwerty",
				metadata={"channel_login": "its_not_qwerty"},
			)
			register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="guild-1", display_name="Guild One",
			)
		finally:
			connection.close()

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
				"""SELECT community_id,target_external_id,status,source_json
				   FROM community_announcements WHERE community_id=1"""
			).fetchall()
		finally:
			connection.close()

		self.assertEqual([(row[0], row[1]) for row in cache_rows], [("c-1", "general"), ("c-2", "coding")])
		self.assertEqual(len(announcement_rows), 1)
		self.assertEqual(tuple(announcement_rows[0][:3]), (1, "c-2", "delivered"))
		self.assertEqual(json.loads(announcement_rows[0][3])["stream_id"], "stream-1")

	def test_scheduled_announcements_dispatch_through_same_community_installation(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			organization_id = create_organization(connection, name="Announcements", slug="announcements")
			workspace_id = create_workspace(
				connection, organization_id=organization_id, name="Operations", slug="operations"
			)
			community_ids = [
				create_community(connection, workspace_id=workspace_id, name=name, slug=slug)
				for name, slug in (("Alpha", "alpha"), ("Bravo", "bravo"))
			]
			with connection:
				operator_id = int(connection.execute(
					"""INSERT INTO operator_accounts(discord_user_id,discord_username,role)
					   VALUES ('operator-1','sam','admin')"""
				).lastrowid)
			for community_id, guild_id in zip(community_ids, ("guild-alpha", "guild-bravo"), strict=True):
				register_installation(
					connection, community_id=community_id, platform="discord",
					external_community_id=guild_id, display_name=guild_id,
				)
			announcement_id = create_announcement(
				connection, community_id=community_ids[1], platform="discord",
				target_external_id="channel-bravo", body="Scheduled update",
				created_by_operator_id=operator_id,
			)
			approve_announcement(
				connection, announcement_id=announcement_id, community_id=community_ids[1],
				approved_by_operator_id=operator_id, scheduled_at="2026-08-26T11:00:00+00:00",
			)
			calls: list[tuple[str, str, str, str]] = []
			delivered = dispatch_due_announcements(
				connection, lambda *args: calls.append(args[:4]) or "message-1",
				now=datetime(2026, 8, 26, 12, tzinfo=UTC),
			)
			row = connection.execute(
				"SELECT status,provider_message_id FROM community_announcement_deliveries"
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(delivered, 1)
		self.assertEqual(calls, [("discord", "guild-bravo", "channel-bravo", "Scheduled update")])
		self.assertEqual(tuple(row), ("delivered", "message-1"))

	def test_announcement_dispatch_requires_installation_capability(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			installation_id = register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="guild-limited", display_name="Limited",
				capabilities={"events"},
			)
			with connection:
				operator_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('limited-operator','sam','admin')"
				).lastrowid)
			announcement_id = create_announcement(
				connection, community_id=1, platform="discord", target_installation_id=installation_id,
				target_external_id="channel-1", body="Blocked", created_by_operator_id=operator_id,
			)
			approve_announcement(
				connection, announcement_id=announcement_id, community_id=1,
				approved_by_operator_id=operator_id, scheduled_at="2026-08-26T11:00:00+00:00",
			)
			calls: list[str] = []
			delivered = dispatch_due_announcements(
				connection, lambda *args: calls.append(str(args[3])) or "message-1",
				now=datetime(2026, 8, 26, 12, tzinfo=UTC),
			)
			row = connection.execute(
				"SELECT status,last_error FROM community_announcements WHERE id=?", (announcement_id,)
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(delivered, 0)
		self.assertEqual(calls, [])
		self.assertEqual(row[0], "failed")
		self.assertIn("does not support announcements", str(row[1]))

	def test_announcement_schedule_uses_tenant_timezone_and_previews_local_time(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"UPDATE communities SET timezone='America/Los_Angeles' WHERE id=1"
				)
				operator_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('timezone-operator','sam','admin')"
				).lastrowid)
			register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="timezone-guild", display_name="Timezone Guild",
			)
			announcement_id = create_announcement(
				connection, community_id=1, platform="discord", target_external_id="updates",
				body="Local update", created_by_operator_id=operator_id,
			)
			approve_announcement(
				connection, announcement_id=announcement_id, community_id=1,
				approved_by_operator_id=operator_id, scheduled_at="2026-08-26T18:00",
			)
			preview = preview_announcement(
				connection, announcement_id=announcement_id, community_id=1
			)
		finally:
			connection.close()

		self.assertEqual(preview["timezone"], "America/Los_Angeles")
		self.assertEqual(preview["scheduled_at"], "2026-08-27T01:00:00+00:00")
		self.assertEqual(preview["scheduled_local"], "2026-08-26T18:00:00-07:00")

	def test_scheduled_announcement_job_rejects_channel_outside_installation(self) -> None:
		settings = AppSettings.from_env({
			"QBOT_DATABASE_PATH": str(self.database_path),
			"QBOT_BACKUP_DIR": str(self.backup_dir),
			"QBOT_ENABLED_SERVICES": "jobs,discord,analysis",
			"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
		})
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				operator_id = int(connection.execute(
					"""INSERT INTO operator_accounts(discord_user_id,discord_username,role)
					   VALUES ('operator-1','sam','admin')"""
				).lastrowid)
			community_id = 1
			register_installation(
				connection, community_id=community_id, platform="discord",
				external_community_id="guild-1", display_name="Guild 1",
			)
			announcement_id = create_announcement(
				connection, community_id=community_id, platform="discord",
				target_external_id="foreign-channel", body="Private update",
				created_by_operator_id=operator_id,
			)
			approve_announcement(
				connection, announcement_id=announcement_id, community_id=community_id,
				approved_by_operator_id=operator_id, scheduled_at="2026-08-26T11:00:00+00:00",
			)
		finally:
			connection.close()

		with mock.patch("src.jobs._fetch_discord_guild_channels", return_value=[{"id": "owned-channel"}]):
			delivered = run_scheduled_announcement_job(
				settings, now=datetime(2026, 8, 26, 12, tzinfo=UTC)
			)
		connection = connect_database(self.database_path)
		try:
			row = connection.execute(
				"SELECT status,last_error FROM community_announcements WHERE id=?",
				(announcement_id,),
			).fetchone()
		finally:
			connection.close()
		self.assertEqual(delivered, 0)
		self.assertEqual(row["status"], "failed")
		self.assertIn("does not belong", row["last_error"])

	def test_welcome_delivery_mentions_only_joined_member(self) -> None:
		class FakeResponse:
			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, traceback) -> None:
				return None

			def read(self) -> bytes:
				return b'{"id":"welcome-message"}'

		captured_payload: dict[str, object] = {}
		def fake_urlopen(request, timeout=15):
			captured_payload.update(json.loads(request.data.decode("utf-8")))
			return FakeResponse()

		with mock.patch(
			"src.jobs._fetch_discord_guild_channels",
			return_value=[{"id": "welcome-channel", "name": "welcome", "type": 0}],
		):
			with mock.patch("src.jobs.urlopen", side_effect=fake_urlopen):
				message_id = _send_scheduled_announcement(
					"bot-token", "discord", "guild-1", "welcome-channel",
					"Welcome <@member-1>!",
					{"type": "member_welcome", "user_id": "member-1"},
				)
		self.assertEqual(message_id, "welcome-message")
		self.assertEqual(captured_payload["allowed_mentions"], {"parse": [], "users": ["member-1"]})
		captured_payload.clear()
		with mock.patch(
			"src.jobs._fetch_discord_guild_channels",
			return_value=[{"id": "welcome-channel", "name": "welcome", "type": 0}],
		):
			with mock.patch("src.jobs.urlopen", side_effect=fake_urlopen):
				_send_scheduled_announcement(
					"bot-token", "discord", "guild-1", "welcome-channel",
					"Verified <@member-1>. Read the guide.",
					{"type": "member_verification_resource", "user_id": "member-1"},
				)
		self.assertEqual(captured_payload["allowed_mentions"], {"parse": [], "users": ["member-1"]})
		captured_payload.clear()
		with mock.patch(
			"src.jobs._fetch_discord_guild_channels",
			return_value=[{"id": "welcome-channel", "name": "welcome", "type": 0}],
		):
			with mock.patch("src.jobs.urlopen", side_effect=fake_urlopen):
				_send_scheduled_announcement(
					"bot-token", "discord", "guild-1", "welcome-channel",
					"Reminder <@member-1>!",
					{"type": "onboarding_checkpoint_reminder", "user_id": "member-1"},
				)
		self.assertEqual(captured_payload["allowed_mentions"], {"parse": [], "users": ["member-1"]})

	def test_onboarding_role_job_uses_tenant_installation_guild(self) -> None:
		settings = AppSettings.from_env({
			"QBOT_DATABASE_PATH": str(self.database_path),
			"QBOT_BACKUP_DIR": str(self.backup_dir),
			"QBOT_ENABLED_SERVICES": "jobs,discord,analysis",
			"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
		})
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			installation_id = register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="role-guild", display_name="Role Guild",
			)
			with connection:
				connection.execute(
					"""INSERT INTO community_onboarding_members(
					       community_id,discord_installation_id,platform_user_id,username,
					       newcomer_role_id,role_assignment_status,joined_at
					   ) VALUES (1,?,'member-1','Member','role-1','pending',?)""",
					(installation_id, "2026-08-26T12:00:00+00:00"),
				)
		finally:
			connection.close()
		with mock.patch("src.jobs._assign_discord_member_role") as assign_role:
			assigned = run_onboarding_role_job(settings)
		assign_role.assert_called_once_with("discord-bot-token", "role-guild", "member-1", "role-1")
		self.assertEqual(assigned, 1)

	def test_failed_announcement_retry_is_bounded_cancellable_and_tenant_scoped(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				operator_id = int(connection.execute(
					"""INSERT INTO operator_accounts(discord_user_id,discord_username,role)
					   VALUES ('operator-1','sam','admin')"""
				).lastrowid)
			announcement_id = create_announcement(
				connection, community_id=1, platform="discord", target_external_id="channel-1",
				body="Retry me", created_by_operator_id=operator_id,
			)
			preview = preview_announcement(
				connection, announcement_id=announcement_id, community_id=1
			)
			with self.assertRaises(LookupError):
				preview_announcement(
					connection, announcement_id=announcement_id, community_id=2
				)
			with connection:
				connection.execute(
					"UPDATE community_announcements SET status='failed',last_error='provider error' WHERE id=?",
					(announcement_id,),
				)
				installation_id = int(connection.execute(
					"""INSERT INTO community_installations(
					       community_id,platform,external_community_id,display_name,status
					   ) VALUES (1,'discord','guild-1','Guild 1','active')"""
				).lastrowid)
				for attempt in range(1, 3):
					connection.execute(
						"""INSERT INTO community_announcement_deliveries(
						       announcement_id,installation_id,attempt_number,status
						   ) VALUES (?,?,?,'failed')""",
						(announcement_id, installation_id, attempt),
					)
			with self.assertRaises(LookupError):
				retry_announcement(
					connection, announcement_id=announcement_id, community_id=2,
					operator_id=operator_id, scheduled_at="2026-08-26T12:00:00+00:00",
				)
			retry_announcement(
				connection, announcement_id=announcement_id, community_id=1,
				operator_id=operator_id, scheduled_at="2026-08-26T12:00:00+00:00",
			)
			with connection:
				connection.execute(
					"UPDATE community_announcements SET status='failed' WHERE id=?", (announcement_id,)
				)
				connection.execute(
					"""INSERT INTO community_announcement_deliveries(
					       announcement_id,installation_id,attempt_number,status
					   ) VALUES (?,?,3,'failed')""",
					(announcement_id, installation_id),
				)
			with self.assertRaisesRegex(ValueError, "attempt limit"):
				retry_announcement(
					connection, announcement_id=announcement_id, community_id=1,
					operator_id=operator_id, scheduled_at="2026-08-26T13:00:00+00:00",
				)
			cancel_announcement(
				connection, announcement_id=announcement_id, community_id=1, operator_id=operator_id
			)
			row = connection.execute(
				"SELECT status FROM community_announcements WHERE id=?", (announcement_id,)
			).fetchone()
			actions = [str(item[0]) for item in connection.execute(
				"""SELECT action_type FROM audit_log WHERE entity_type='community_announcement'
				   AND entity_id=? ORDER BY id""",
				(announcement_id,),
			).fetchall()]
		finally:
			connection.close()
		self.assertEqual(row["status"], "cancelled")
		self.assertEqual(preview["body"], "Retry me")
		self.assertFalse(preview["ready"])
		self.assertEqual(preview["attempt_count"], 0)
		self.assertEqual(
			actions,
			["announcement.created", "announcement.retry_scheduled", "announcement.cancelled"],
		)

	def test_announcement_dispatch_applies_per_community_quota(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			organization_id = create_organization(connection, name="Quota", slug="quota")
			workspace_id = create_workspace(
				connection, organization_id=organization_id, name="Quota", slug="quota"
			)
			community_ids = [1, create_community(
				connection, workspace_id=workspace_id, name="Second", slug="second"
			)]
			with connection:
				operator_id = int(connection.execute(
					"""INSERT INTO operator_accounts(discord_user_id,discord_username,role)
					   VALUES ('operator-1','sam','admin')"""
				).lastrowid)
			for community_id in community_ids:
				register_installation(
					connection, community_id=community_id, platform="discord",
					external_community_id=f"guild-{community_id}", display_name=f"Guild {community_id}",
				)
			for community_id, count in ((community_ids[0], 4), (community_ids[1], 1)):
				for item in range(count):
					announcement_id = create_announcement(
						connection, community_id=community_id, platform="discord",
						target_external_id=f"channel-{community_id}", body=f"Message {community_id}-{item}",
						created_by_operator_id=operator_id,
					)
					approve_announcement(
						connection, announcement_id=announcement_id, community_id=community_id,
						approved_by_operator_id=operator_id, scheduled_at="2026-08-26T11:00:00+00:00",
					)
			calls: list[str] = []
			delivered = dispatch_due_announcements(
				connection, lambda platform, guild, channel, body, source: calls.append(guild) or body,
				now=datetime(2026, 8, 26, 12, tzinfo=UTC), limit=2, per_community_limit=2,
			)
		finally:
			connection.close()
		self.assertEqual(delivered, 2)
		self.assertEqual(calls.count("guild-1"), 1)
		self.assertEqual(calls.count(f"guild-{community_ids[1]}"), 1)

	def test_twitch_live_announcements_use_same_community_installations(self) -> None:
		settings = AppSettings.from_env({
			"QBOT_DATABASE_PATH": str(self.database_path),
			"QBOT_BACKUP_DIR": str(self.backup_dir),
			"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
			"QBOT_TWITCH_BOT_TOKEN": "oauth:test-token",
			"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
			"QBOT_DISCORD_GUILD_IDS": "foreign-static-guild",
		})
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			second_community_id = create_community(
				connection, workspace_id=1, name="Second", slug="second-live"
			)
			for community_id, login, guild_id in (
				(1, "alpha", "guild-alpha"),
				(second_community_id, "bravo", "guild-bravo"),
			):
				register_installation(
					connection, community_id=community_id, platform="twitch",
					external_community_id=f"broadcaster-{login}", display_name=login,
					metadata={"channel_login": login},
				)
				register_installation(
					connection, community_id=community_id, platform="discord",
					external_community_id=guild_id, display_name=guild_id,
				)
		finally:
			connection.close()

		def fake_stream(login: str, token_manager) -> TwitchLiveStream:
			return TwitchLiveStream(
				stream_id=f"stream-{login}", title=f"{login} coding",
				url=f"https://www.twitch.tv/{login}", game_name="Software Development",
			)

		def fake_channels(guild_id: str, bot_token: str) -> list[dict[str, object]]:
			return [{"id": f"channel-{guild_id}", "name": "general", "type": 0}]

		deliveries: list[tuple[str, str, str, dict[str, object]]] = []
		def fake_send(bot_token, platform, guild_id, channel_id, body, source):
			deliveries.append((guild_id, channel_id, body, source))
			return f"message-{guild_id}"

		with mock.patch("src.jobs._fetch_twitch_live_stream", side_effect=fake_stream):
			with mock.patch("src.jobs._fetch_discord_guild_channels", side_effect=fake_channels):
				with mock.patch("src.jobs._send_scheduled_announcement", side_effect=fake_send):
					first_count = run_twitch_live_announcement_job(settings, mock.Mock())
					second_count = run_twitch_live_announcement_job(settings, mock.Mock())

		connection = connect_database(self.database_path)
		try:
			announcements = connection.execute(
				"""SELECT community_id,status,source_json FROM community_announcements
				   ORDER BY community_id"""
			).fetchall()
			observations = connection.execute(
				"""SELECT community_id,context_id FROM observations
				   WHERE event_type='stream.started' ORDER BY community_id"""
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(first_count, 2)
		self.assertEqual(second_count, 0)
		self.assertEqual([item[0] for item in deliveries], ["guild-alpha", "guild-bravo"])
		self.assertNotIn("foreign-static-guild", [item[0] for item in deliveries])
		self.assertEqual([int(row["community_id"]) for row in announcements], [1, second_community_id])
		self.assertTrue(all(row["status"] == "delivered" for row in announcements))
		self.assertTrue(all(json.loads(row["source_json"])["type"] == "twitch_live" for row in announcements))
		self.assertEqual(
			[(int(row["community_id"]), row["context_id"]) for row in observations],
			[(1, "alpha"), (second_community_id, "bravo")],
		)

	def test_twitch_live_announcement_reuses_supplied_token_manager(self) -> None:
		settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
				"QBOT_TWITCH_BOT_TOKEN": "oauth:stale-token",
				"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
				"QBOT_DISCORD_GUILD_IDS": "guild-1",
			}
		)
		token_manager = mock.Mock()
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			register_installation(
				connection, community_id=1, platform="twitch",
				external_community_id="broadcaster-1", display_name="its_not_qwerty",
				metadata={"channel_login": "its_not_qwerty"},
			)
			register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="guild-1", display_name="Guild One",
			)
		finally:
			connection.close()

		with (
			mock.patch("src.jobs._build_twitch_token_manager") as build_token_manager,
			mock.patch("src.jobs._fetch_twitch_live_stream", return_value=None) as fetch_stream,
		):
			run_twitch_live_announcement_job(settings, token_manager)

		build_token_manager.assert_not_called()
		fetch_stream.assert_called_once_with("its_not_qwerty", token_manager)

	def test_twitch_live_announcement_refreshes_expired_access_token(self) -> None:
		settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_BACKUP_DIR": str(self.backup_dir),
				"QBOT_ENABLED_SERVICES": "jobs,twitch,discord,analysis",
				"QBOT_AUDIT_RETENTION_DAYS": "1",
				"QBOT_TWITCH_BOT_TOKEN": "oauth:expired-token",
				"QBOT_TWITCH_REFRESH_TOKEN": "refresh-token",
				"QBOT_TWITCH_CLIENT_ID": "configured-client-id",
				"QBOT_TWITCH_CLIENT_SECRET": "configured-client-secret",
				"QBOT_DISCORD_BOT_TOKEN": "discord-bot-token",
				"QBOT_DISCORD_GUILD_IDS": "guild-1",
			}
		)
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			register_installation(
				connection, community_id=1, platform="twitch",
				external_community_id="broadcaster-1", display_name="its_not_qwerty",
				metadata={"channel_login": "its_not_qwerty"},
			)
			register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="guild-1", display_name="Guild One",
			)
		finally:
			connection.close()

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
