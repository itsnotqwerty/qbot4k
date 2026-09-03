from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.config import AppSettings
from src.__main__ import run_application
from src.db import connect_database, initialize_database
from src.discord import DiscordConnector
from src.jobs import run_maintenance_jobs
from src.intelligence.community import register_installation
from src.pipeline.actions import ActionRegistry, ModerationActionHandler
from src.pipeline.workers import DiscordWorker
from src.twitch import TwitchConnectionError, TwitchConnector
from tests.pipeline_support import drain_analysis, ingest_and_analyze


class ImmediateBlockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "qbot4k.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def register_provider(self, platform: str, external_community_id: str) -> None:
        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            register_installation(
                connection,
                community_id=1,
                platform=platform,
                external_community_id=external_community_id,
                display_name=external_community_id,
                status="active",
            )
        finally:
            connection.close()

    def test_discord_moderation_findings_execute_through_action_worker(self) -> None:
        self.register_provider("discord", "guild-1")
        connector = DiscordConnector(self.database_path, bot_token="test-token")
        result = connector.ingest_message({
            "id": "discord-egregious-1",
            "timestamp": "2026-08-11T01:00:00Z",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "content": "you are a faggot",
            "author": {"id": "user-1", "username": "user_one", "bot": False},
        })
        self.assertEqual(result.status, "persisted")
        drain_analysis(self.database_path)

        registry = ActionRegistry()
        registry.register(
            "discord.moderation.execute",
            ModerationActionHandler(connector),
        )
        worker = DiscordWorker(self.database_path, registry, poll_interval=0)
        with (
            mock.patch.object(connector, "_delete_discord_message"),
            mock.patch.object(connector, "_timeout_discord_member") as timeout_member,
            mock.patch.object(connector, "_log_moderation_event_to_modlogs"),
        ):
            self.assertTrue(worker.process_next_job())

        connection = connect_database(self.database_path)
        try:
            action_status = str(connection.execute(
                "SELECT status FROM moderation_actions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
            job_status = str(connection.execute(
                """
                SELECT status FROM processing_jobs
                WHERE job_type = 'discord.moderation.execute'
                """
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(action_status, "completed")
        self.assertEqual(job_status, "completed")
        timeout_member.assert_called_once()

    def test_runtime_shuts_down_cleanly_without_connector_action_worker(self) -> None:
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_ENABLED_SERVICES": "analysis",
        })
        with (
            mock.patch("src.__main__.load_settings", return_value=settings),
            mock.patch("src.__main__.AnalysisWorker.run_forever", return_value=None),
        ):
            self.assertEqual(run_application(once=False), 0)

    def test_twitch_only_runtime_initializes_action_registry(self) -> None:
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_ENABLED_SERVICES": "twitch,analysis",
            "QBOT_TWITCH_BOT_TOKEN": "oauth:test-token",
            "QBOT_TWITCH_CHANNELS": "sample_channel",
        })
        with (
            mock.patch("src.__main__.load_settings", return_value=settings),
            mock.patch("src.__main__.AnalysisWorker.run_forever", return_value=None),
            mock.patch("src.__main__.DiscordWorker.run_forever", return_value=None),
            mock.patch("src.__main__.TwitchConnector.run_twitch_safely", return_value=None),
        ):
            self.assertEqual(run_application(once=False), 0)

    def test_twitch_moderation_findings_execute_through_action_worker(self) -> None:
        self.register_provider("twitch", "sample_channel")
        connector = TwitchConnector(self.database_path)
        result = connector.ingest_message({
            "message_id": "twitch-egregious-1",
            "timestamp": "2026-08-11T01:00:00Z",
            "channel": "sample_channel",
            "content": "you are a faggot",
            "user_id": "user-1",
            "username": "user_one",
        })
        self.assertEqual(result.status, "persisted")
        drain_analysis(self.database_path)

        registry = ActionRegistry()
        registry.register(
            "twitch.moderation.execute",
            ModerationActionHandler(connector),
        )
        worker = DiscordWorker(self.database_path, registry, poll_interval=0)
        with mock.patch.object(
            connector,
            "_execute_helix_moderation_action",
            return_value=("200", {"data": [{"moderation_id": "provider-1"}]}),
        ) as helix_action:
            self.assertTrue(worker.process_next_job())

        connection = connect_database(self.database_path)
        try:
            status = str(connection.execute(
                "SELECT status FROM moderation_actions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(status, "completed")
        helix_action.assert_called_once()
        self.assertEqual(helix_action.call_args.kwargs["target_user_id"], "user-1")
        self.assertEqual(helix_action.call_args.kwargs["action_type"], "timeout")

    def test_twitch_supervisor_reconnects_after_transient_failure(self) -> None:
        connector = TwitchConnector(self.database_path)
        service_states: dict[str, str] = {}
        calls = 0

        def run_once(_token: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TwitchConnectionError("temporary disconnect")
            connector.stop()

        with (
            mock.patch.object(connector, "run_forever", side_effect=run_once),
            mock.patch.object(connector._stop_event, "wait", return_value=False),
        ):
            connector.run_twitch_safely("token", service_states)

        self.assertEqual(calls, 2)
        self.assertEqual(service_states["twitch"], "reconnecting")

    def test_temporal_history_references_only_trigger_evidence(self) -> None:
        connector = DiscordConnector(self.database_path)
        base = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
        for index in range(20):
            actor = "u1" if index % 2 == 0 else "u2"
            target = "u2" if actor == "u1" else "u1"
            result = ingest_and_analyze(
                connector,
                {
                    "id": f"message-{index}",
                    "timestamp": (base + timedelta(seconds=index)).isoformat(),
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "content": f"hello <@{target}>",
                    "mentions": [{"id": target}],
                    "author": {"id": actor, "username": actor, "bot": False},
                },
            )
            self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            history_count = int(connection.execute(
                "SELECT COUNT(*) FROM derived_signal_history"
            ).fetchone()[0])
            evidence_count = int(connection.execute(
                "SELECT COUNT(*) FROM derived_signal_evidence"
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(history_count, 500)
        self.assertEqual(evidence_count, 500)

    def test_maintenance_compacts_expired_intelligence_state(self) -> None:
        connector = DiscordConnector(self.database_path)
        for index in range(2):
            result = ingest_and_analyze(
                connector,
                {
                    "id": f"old-message-{index}",
                    "timestamp": f"2026-08-0{index + 1}T00:00:00Z",
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "content": "hello everyone",
                    "author": {"id": "old-user", "username": "old_user", "bot": False},
                },
            )
            self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            with connection:
                connection.execute(
                    "UPDATE community_policy_settings SET message_retention_days=1 WHERE community_id=1"
                )
                connection.execute(
                    """
                    INSERT INTO processing_jobs (
                        stage, job_type, payload_json, status, completed_at,
                        idempotency_key, created_at, updated_at
                    ) VALUES (
                        'action', 'test.completed', '{}', 'completed',
                        '2026-08-01T00:00:00+00:00', 'test:completed',
                        '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'
                    )
                    """
                )
        finally:
            connection.close()

        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_ENABLED_SERVICES": "jobs",
            "QBOT_AUDIT_RETENTION_DAYS": "1",
        })
        report = run_maintenance_jobs(
            settings,
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )

        connection = connect_database(self.database_path)
        try:
            counts = {
                table: int(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in (
                    "messages",
                    "observations",
                    "signal_calculation_runs",
                    "derived_signal_history",
                    "derived_signal_evidence",
                    "social_score_runs",
                )
            }
            completed_test_jobs = int(connection.execute(
                """
                SELECT COUNT(*) FROM processing_jobs
                WHERE idempotency_key = 'test:completed'
                """
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(counts["messages"], 0)
        self.assertEqual(counts["observations"], 0)
        self.assertEqual(counts["signal_calculation_runs"], 0)
        self.assertEqual(counts["derived_signal_history"], 0)
        self.assertEqual(counts["derived_signal_evidence"], 0)
        self.assertEqual(counts["social_score_runs"], 1)
        self.assertEqual(completed_test_jobs, 0)
        self.assertEqual(report.deleted_messages, 2)
        self.assertEqual(report.deleted_observations, 2)
        self.assertEqual(report.deleted_signal_runs, 2)
        self.assertEqual(report.deleted_score_runs, 1)
        self.assertEqual(report.deleted_processing_jobs, 1)


if __name__ == "__main__":
    unittest.main()
