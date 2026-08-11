from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from unittest import mock

from src.config import AppSettings, ConfigError
from src.__main__ import (
    _install_shutdown_handlers,
    _restore_shutdown_handlers,
)
from src.db import connect_database, initialize_database, list_tables
from src.health import create_health_server
from src.token_store import persist_refreshed_twitch_tokens


class FoundationTests(unittest.TestCase):
    def build_env(self, tmpdir: str, **overrides: str) -> dict[str, str]:
        env = {
            "QBOT_DATABASE_PATH": str(Path(tmpdir) / "qbot4k.sqlite3"),
            "QBOT_ENABLED_SERVICES": "web,jobs",
            "QBOT_DASHBOARD_SESSION_SECRET": "session-secret",
            "QBOT_DISCORD_OAUTH_CLIENT_ID": "oauth-client-id",
            "QBOT_DISCORD_OAUTH_CLIENT_SECRET": "oauth-client-secret",
            "QBOT_DISCORD_OAUTH_REDIRECT_URI": "http://127.0.0.1/callback",
            "QBOT_OPERATOR_GUILD_IDS": "guild-1",
        }
        env.update(overrides)
        return env

    def test_identity_attribution_migration_preserves_detached_account_history(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "legacy-identity.sqlite3"
            connection = connect_database(database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE users (
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
                    CREATE TABLE platform_accounts (
                        id INTEGER PRIMARY KEY,
                        platform TEXT NOT NULL,
                        platform_user_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        guild_or_channel_context TEXT,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(platform, platform_user_id)
                    );
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY,
                        observation_id INTEGER,
                        platform TEXT NOT NULL,
                        platform_message_id TEXT,
                        platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
                        channel_id TEXT NOT NULL,
                        content_raw TEXT NOT NULL,
                        content_normalized TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(platform, platform_message_id)
                    );
                    CREATE TABLE moderation_actions (
                        id INTEGER PRIMARY KEY,
                        platform TEXT NOT NULL,
                        message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                        target_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
                        action_type TEXT NOT NULL,
                        actor_type TEXT NOT NULL,
                        actor_id INTEGER,
                        reason TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE reputation_events (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        source_type TEXT NOT NULL,
                        source_id INTEGER,
                        delta INTEGER NOT NULL,
                        reason_code TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE audit_log (
                        id INTEGER PRIMARY KEY,
                        actor_type TEXT NOT NULL,
                        actor_id INTEGER,
                        action_type TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id INTEGER,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO users (id, primary_display_name) VALUES (1, 'legacy_user');
                    INSERT INTO users (id, primary_display_name) VALUES (2, 'merge_target');
                    INSERT INTO users (id, primary_display_name) VALUES (3, 'merge_ghost');
                    INSERT INTO platform_accounts (
                        id, platform, platform_user_id, username, user_id
                    ) VALUES (10, 'discord', 'legacy-account', 'legacy_user', NULL);
                    INSERT INTO messages (
                        id, platform, platform_message_id, platform_account_id,
                        channel_id, content_raw, content_normalized, sent_at
                    ) VALUES (
                        20, 'discord', 'legacy-message', 10,
                        'legacy-channel', 'hello', 'hello', '2026-08-10T00:00:00+00:00'
                    );
                    INSERT INTO reputation_events (
                        user_id, source_type, source_id, delta, reason_code
                    ) VALUES (1, 'message', 20, 1, 'message_sent');
                    INSERT INTO platform_accounts (
                        id, platform, platform_user_id, username, user_id
                    ) VALUES (11, 'twitch', 'merged-account', 'merged_user', 2);
                    INSERT INTO messages (
                        id, platform, platform_message_id, platform_account_id,
                        channel_id, content_raw, content_normalized, sent_at
                    ) VALUES (
                        21, 'twitch', 'merged-message', 11,
                        'merged-channel', 'hello', 'hello', '2026-08-10T00:01:00+00:00'
                    );
                    INSERT INTO reputation_events (
                        user_id, source_type, source_id, delta, reason_code
                    ) VALUES (3, 'message', 21, 1, 'message_sent');
                    INSERT INTO audit_log (
                        actor_type, action_type, entity_type, entity_id, payload_json
                    ) VALUES (
                        'system', 'user_account_link', 'platform_account', 10,
                        '{"platform":"discord","platform_user_id":"legacy-account","user_id":1}'
                    );
                    INSERT INTO audit_log (
                        actor_type, action_type, entity_type, entity_id, payload_json
                    ) VALUES (
                        'system', 'user_account_unlink', 'platform_account', 10,
                        '{"platform":"discord","platform_user_id":"legacy-account"}'
                    );
                    INSERT INTO audit_log (
                        actor_type, action_type, entity_type, entity_id, payload_json
                    ) VALUES ('system', 'auto_user_create', 'user', 3, '{}');
                    """
                )

                initialize_database(connection)
                account = connection.execute(
                    "SELECT user_id, detached_from_user_id FROM platform_accounts WHERE id = 10"
                ).fetchone()
                message = connection.execute("SELECT user_id FROM messages WHERE id = 20").fetchone()
                ghost_count = int(connection.execute("SELECT COUNT(*) FROM users WHERE id = 3").fetchone()[0])
                merged_event_owner = int(
                    connection.execute(
                        "SELECT user_id FROM reputation_events WHERE source_id = 21 AND source_type = 'message'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

        self.assertIsNone(account[0])
        self.assertEqual(int(account[1]), 1)
        self.assertEqual(int(message[0]), 1)
        self.assertEqual(ghost_count, 0)
        self.assertEqual(merged_event_owner, 2)

    def test_settings_validate_with_web_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(self.build_env(tmpdir))
            self.assertEqual(settings.dashboard_port, 8080)
            self.assertEqual(settings.enabled_services, ("web", "jobs"))
            self.assertEqual(settings.twitch_channels, ("its_not_qwerty",))
            self.assertFalse(settings.discord_allow_bot_messages)

    def test_settings_can_enable_discord_bot_message_ingestion(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(
                self.build_env(tmpdir, QBOT_DISCORD_ALLOW_BOT_MESSAGES="true")
            )

        self.assertTrue(settings.discord_allow_bot_messages)

    def test_explicit_environment_file_is_read_and_authoritative(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "from-explicit-file.sqlite3"
            backup_path = Path(tmpdir) / "backups"
            env_file = Path(tmpdir) / "qbot4k.env"
            env_file.write_text(
                f"QBOT_DATABASE_PATH={database_path}\n"
                f"QBOT_BACKUP_DIR={backup_path}\n"
                "QBOT_ENABLED_SERVICES=jobs,analysis\n",
                encoding="utf-8",
            )
            inherited = {
                "QBOT_DATABASE_PATH": str(Path(tmpdir) / "wrong.sqlite3"),
                "QBOT_ENABLED_SERVICES": "web",
            }

            with mock.patch.dict(os.environ, inherited, clear=True):
                settings = AppSettings.from_env(env_file=env_file)

        self.assertEqual(settings.database_path, database_path)
        self.assertEqual(settings.backup_dir, backup_path)
        self.assertEqual(settings.enabled_services, ("jobs", "analysis"))

    def test_explicit_environment_file_must_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            missing_path = Path(tmpdir) / "missing.env"
            with self.assertRaisesRegex(ConfigError, "does not exist"):
                AppSettings.from_env(env_file=missing_path)

    def test_absolute_entrypoint_works_outside_project_directory(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        entrypoint = project_root / "src" / "__main__.py"
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "outside-cwd.sqlite3"
            env_file = Path(tmpdir) / "qbot4k.env"
            env_file.write_text(
                f"QBOT_DATABASE_PATH={database_path}\n"
                "QBOT_ENABLED_SERVICES=jobs,analysis\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(entrypoint),
                    "--env-file",
                    str(env_file),
                    "check-config",
                ],
                cwd=tmpdir,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["database_path"], str(database_path))

    def test_systemd_templates_reset_legacy_paths(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        service = (project_root / "deploy" / "qbot4k.service").read_text(
            encoding="utf-8"
        )
        override = (
            project_root / "deploy" / "zz-qbot4k-installer.conf"
        ).read_text(encoding="utf-8")

        expected_entrypoint = (
            "/opt/qbot4k/current/.venv/bin/python "
            "/opt/qbot4k/current/src/__main__.py "
            "--env-file /etc/qbot4k/qbot4k.env run"
        )
        self.assertIn(f"ExecStart={expected_entrypoint}", service)
        self.assertIn("ExecStart=\n", override)
        self.assertIn(f"ExecStart={expected_entrypoint}", override)
        self.assertIn("ReadWritePaths=\n", override)
        self.assertIn("/opt/qbot4k/data", override)

    def test_settings_fail_fast_when_web_auth_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            env = self.build_env(tmpdir)
            env.pop("QBOT_DISCORD_OAUTH_CLIENT_SECRET")

            with self.assertRaises(ConfigError):
                AppSettings.from_env(env)

    def test_settings_fail_fast_when_operator_allowlist_missing_for_web(self) -> None:
        with TemporaryDirectory() as tmpdir:
            env = self.build_env(tmpdir)
            env.pop("QBOT_OPERATOR_GUILD_IDS")

            with self.assertRaises(ConfigError):
                AppSettings.from_env(env)

    def test_settings_require_discord_guild_ids_for_live_announcement_stack(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(ConfigError):
                AppSettings.from_env(
                    self.build_env(
                        tmpdir,
                        QBOT_ENABLED_SERVICES="jobs,twitch,discord",
                        QBOT_TWITCH_BOT_TOKEN="token",
                        QBOT_DISCORD_BOT_TOKEN="discord-token",
                    )
                )

    def test_database_initialization_creates_expected_tables(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(self.build_env(tmpdir))
            connection = connect_database(settings.database_path)
            try:
                initialize_database(connection)
                tables = set(list_tables(connection))
            finally:
                connection.close()

            self.assertIn("users", tables)
            self.assertIn("messages", tables)
            self.assertIn("audit_log", tables)
            self.assertIn("twitch_channels", tables)

    def test_twitch_service_does_not_require_bootstrap_channels(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(
                self.build_env(
                    tmpdir,
                    QBOT_ENABLED_SERVICES="twitch,analysis",
                    QBOT_TWITCH_BOT_TOKEN="token",
                )
            )

        self.assertEqual(settings.twitch_channels, ("its_not_qwerty",))
        self.assertEqual(settings.twitch_join_command_channel, "its_not_qwerty")

    def test_twitch_refresh_token_requires_client_credentials(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(ConfigError):
                AppSettings.from_env(
                    self.build_env(
                        tmpdir,
                        QBOT_ENABLED_SERVICES="twitch,analysis",
                        QBOT_TWITCH_BOT_TOKEN="token",
                        QBOT_TWITCH_REFRESH_TOKEN="refresh-token",
                    )
                )

            settings = AppSettings.from_env(
                self.build_env(
                    tmpdir,
                    QBOT_ENABLED_SERVICES="twitch,analysis",
                    QBOT_TWITCH_BOT_TOKEN="token",
                    QBOT_TWITCH_REFRESH_TOKEN="refresh-token",
                    QBOT_TWITCH_CLIENT_ID="client-id",
                    QBOT_TWITCH_CLIENT_SECRET="client-secret",
                )
            )

        self.assertEqual(settings.twitch_client_id, "client-id")
        self.assertEqual(settings.twitch_client_secret, "client-secret")

    def test_health_endpoint_reports_ready(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(self.build_env(tmpdir))
            settings = replace(settings, dashboard_port=0)
            server = create_health_server(settings, {"jobs": "ready"})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                address = f"http://{server.server_address[0]}:{server.server_address[1]}/health"
                with urlopen(address, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["database"]["journal_mode"].lower(), "wal")

    def test_shutdown_handler_sets_event_and_can_be_restored(self) -> None:
        shutdown_event = threading.Event()
        handler_state = _install_shutdown_handlers(shutdown_event, logging.getLogger("test"))

        self.assertIsNotNone(handler_state)
        assert handler_state is not None
        _registered_handlers, handler = handler_state

        handler(signal.SIGTERM, None)
        self.assertTrue(shutdown_event.is_set())

        _restore_shutdown_handlers(handler_state)

    def test_refresh_persistence_updates_dotenv_tokens(self) -> None:
        with TemporaryDirectory() as tmpdir:
            dotenv_path = Path(tmpdir) / ".env"
            dotenv_path.write_text(
                "QBOT_TWITCH_BOT_TOKEN=oauth:old-token\n"
                "QBOT_TWITCH_REFRESH_TOKEN=old-refresh\n"
                "QBOT_DATABASE_PATH=./var/qbot4k.sqlite3\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=False):
                with mock.patch("src.token_store._DOTENV_PATH", dotenv_path):
                    persist_refreshed_twitch_tokens("refreshed-token", "rotated-refresh")

                contents = dotenv_path.read_text(encoding="utf-8")

        self.assertIn("QBOT_TWITCH_BOT_TOKEN=refreshed-token", contents)
        self.assertIn("QBOT_TWITCH_REFRESH_TOKEN=rotated-refresh", contents)
        self.assertIn("QBOT_DATABASE_PATH=./var/qbot4k.sqlite3", contents)


if __name__ == "__main__":
    unittest.main()
