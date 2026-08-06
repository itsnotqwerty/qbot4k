from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import AppSettings
from src.db import connect_database, initialize_database
from src.jobs import run_maintenance_jobs


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
		self.assertEqual(report.rollup_rows, 3)
		self.assertEqual(message_count, 0)
		self.assertEqual(audit_count, 0)
		self.assertEqual([row[0] for row in rollups], ["messages_total", "open_reviews", "pending_actions"])
		self.assertEqual([row[1] for row in rollups], [0.0, 0.0, 0.0])

		backup_path = Path(report.backup_path)
		metadata_path = Path(report.backup_metadata_path)
		self.assertTrue(backup_path.exists())
		self.assertTrue(metadata_path.exists())
		metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
		self.assertEqual(metadata["backup_path"], report.backup_path)
		self.assertEqual(metadata["sha256"], report.backup_sha256)
