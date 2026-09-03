from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
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
            "QBOT_LEGAL_ORGANIZATION_NAME": "QBot4K Test Operations",
            "QBOT_LEGAL_CONTACT_EMAIL": "privacy@example.test",
            "QBOT_LEGAL_JURISDICTION": "Test Jurisdiction",
            "QBOT_LEGAL_EFFECTIVE_DATE": "2026-09-02",
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

    def test_schema_migrations_are_ordered_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "migrations.sqlite3"
            connection = connect_database(database_path)
            try:
                initialize_database(connection)
                initial = [tuple(row) for row in connection.execute(
                    "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
                )]

                initialize_database(connection, force=True)
                repeated = [tuple(row) for row in connection.execute(
                    "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
                )]
            finally:
                connection.close()

        self.assertEqual([row[0] for row in initial], list(range(1, 28)))
        self.assertEqual(repeated, initial)

    def test_tenant_job_migration_upgrades_pre_27_schema(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "pre-v27.sqlite3")
            try:
                initialize_database(connection)
                with connection:
                    connection.execute("DROP INDEX idx_processing_jobs_tenant_available")
                    connection.execute("ALTER TABLE processing_jobs DROP COLUMN community_id")
                    connection.execute("DELETE FROM schema_migrations WHERE version=27")

                initialize_database(connection, force=True)
                columns = {
                    str(row[1]) for row in connection.execute(
                        "PRAGMA table_info(processing_jobs)"
                    ).fetchall()
                }
                indexes = {
                    str(row[1]) for row in connection.execute(
                        "PRAGMA index_list(processing_jobs)"
                    ).fetchall()
                }
            finally:
                connection.close()

        self.assertIn("community_id", columns)
        self.assertIn("idx_processing_jobs_tenant_available", indexes)

    def test_api_client_ownership_migration_backfills_and_requires_community(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "api-client-v17.sqlite3")
            initialize_database(connection)
            with connection:
                connection.execute("DROP TABLE api_request_usage")
                connection.execute("DROP TABLE api_clients")
                connection.execute(
                    """CREATE TABLE api_clients (
                           id INTEGER PRIMARY KEY,
                           organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                           name TEXT NOT NULL,
                           key_hash TEXT NOT NULL UNIQUE,
                           scopes_json TEXT NOT NULL DEFAULT '[]',
                           status TEXT NOT NULL DEFAULT 'active',
                           rate_limit_per_minute INTEGER NOT NULL DEFAULT 120,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           revoked_at TEXT
                       )"""
                )
                connection.execute(
                    "INSERT INTO api_clients(organization_id,name,key_hash) VALUES (1,'legacy','legacy-key')"
                )
                connection.execute("DELETE FROM schema_migrations WHERE version=18")

            initialize_database(connection, force=True)

            self.assertEqual(
                connection.execute(
                    "SELECT community_id FROM api_clients WHERE key_hash='legacy-key'"
                ).fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO api_clients(organization_id,name,key_hash,community_id)
                       VALUES (1,'invalid','invalid-key',NULL)"""
                )
            connection.close()

    def test_data_subject_request_migration_backfills_strict_community_owner(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "subject-request-v21.sqlite3")
            initialize_database(connection)
            with connection:
                connection.execute("DROP TABLE data_subject_requests")
                connection.execute(
                    """CREATE TABLE data_subject_requests (
                           id INTEGER PRIMARY KEY,
                           organization_id INTEGER NOT NULL REFERENCES organizations(id),
                           request_type TEXT NOT NULL,
                           status TEXT NOT NULL DEFAULT 'open',
                           result_json TEXT NOT NULL DEFAULT '{}'
                       )"""
                )
                connection.execute(
                    """INSERT INTO data_subject_requests(organization_id,request_type)
                       VALUES (1,'export')"""
                )
                connection.execute("DELETE FROM schema_migrations WHERE version=22")

            initialize_database(connection, force=True)

            owner = connection.execute(
                "SELECT community_id FROM data_subject_requests WHERE id=1"
            ).fetchone()[0]
            columns = {
                str(row[1]): int(row[3])
                for row in connection.execute("PRAGMA table_info(data_subject_requests)")
            }
            self.assertEqual(owner, 1)
            self.assertEqual(columns["community_id"], 1)
            connection.close()

    def test_community_runtime_policy_migration_backfills_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "community-policy-v18.sqlite3")
            initialize_database(connection)
            with connection:
                connection.execute("ALTER TABLE community_policy_settings DROP COLUMN message_retention_days")
                connection.execute("ALTER TABLE community_policy_settings DROP COLUMN analytics_retention_days")
                connection.execute("ALTER TABLE community_policy_settings DROP COLUMN allow_bot_messages")
                connection.execute("DELETE FROM schema_migrations WHERE version=19")

            initialize_database(connection, force=True)

            policy = connection.execute(
                """SELECT message_retention_days,analytics_retention_days,allow_bot_messages
                   FROM community_policy_settings WHERE community_id=1"""
            ).fetchone()
            self.assertEqual(tuple(policy), (90, 90, 0))
            connection.close()

    def test_community_anti_abuse_policy_migration_backfills_shadow_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "anti-abuse-v19.sqlite3")
            initialize_database(connection)
            columns = (
                "anti_abuse_enabled", "anti_abuse_enforcement_mode", "message_burst_limit",
                "message_burst_window_seconds", "mention_limit", "join_raid_limit",
                "join_raid_window_seconds",
            )
            with connection:
                for column in columns:
                    connection.execute(f"ALTER TABLE community_policy_settings DROP COLUMN {column}")
                connection.execute("DELETE FROM schema_migrations WHERE version=20")

            initialize_database(connection, force=True)

            policy = connection.execute(
                """SELECT anti_abuse_enabled,anti_abuse_enforcement_mode,
                          message_burst_limit,message_burst_window_seconds,
                          mention_limit,join_raid_limit,join_raid_window_seconds
                   FROM community_policy_settings WHERE community_id=1"""
            ).fetchone()
            self.assertEqual(tuple(policy), (1, "shadow", 12, 10, 8, 25, 60))
            connection.close()

    def test_legacy_twitch_onboarding_migration_preserves_intent(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "twitch-onboarding-v20.sqlite3")
            initialize_database(connection)
            with connection:
                connection.execute(
                    """INSERT INTO operator_accounts(
                           id,discord_user_id,discord_username,role
                       ) VALUES (1,'legacy-operator','Legacy Operator','admin')"""
                )
                connection.execute(
                    """CREATE TABLE twitch_onboarding_states (
                           nonce TEXT PRIMARY KEY,
                           operator_id INTEGER NOT NULL REFERENCES operator_accounts(id),
                           community_id INTEGER NOT NULL REFERENCES communities(id),
                           broadcaster_login TEXT NOT NULL,
                           requested_scopes_json TEXT NOT NULL DEFAULT '[]',
                           status TEXT NOT NULL DEFAULT 'pending',
                           installation_id INTEGER,
                           expires_at TEXT NOT NULL,
                           completed_at TEXT,
                           last_error TEXT,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                connection.execute(
                    """INSERT INTO twitch_onboarding_states(
                           nonce,operator_id,community_id,broadcaster_login,
                           requested_scopes_json,status,expires_at
                       ) VALUES ('legacy-nonce',1,1,'streamer',?,
                                 'pending','2099-01-01T00:00:00+00:00')""",
                    ('["chat:read"]',),
                )
                connection.execute("DELETE FROM schema_migrations WHERE version=21")

            initialize_database(connection, force=True)

            intent = connection.execute(
                """SELECT operator_id,community_id,broadcaster_login,scopes_json,consumed_at
                   FROM twitch_install_intents WHERE nonce='legacy-nonce'"""
            ).fetchone()
            legacy_table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='twitch_onboarding_states'"""
            ).fetchone()
            self.assertEqual(tuple(intent), (1, 1, "streamer", '["chat:read"]', None))
            self.assertIsNone(legacy_table)
            connection.close()

    def test_legacy_schema_convergence_runs_once_at_version_seven(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "legacy-v6.sqlite3"
            connection = connect_database(database_path)
            initialize_database(connection)
            with connection:
                connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
                connection.execute("ALTER TABLE community_installations DROP COLUMN last_error")

            initialize_database(connection, force=True)
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(community_installations)"
                )
            }
            self.assertIn("last_error", columns)

            with mock.patch("src.db._migrate_legacy_schema") as convergence:
                initialize_database(connection, force=True)
            convergence.assert_not_called()
            connection.close()

    def test_provider_installation_references_backfill_from_observation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "installation-backfill.sqlite3")
            initialize_database(connection)
            with connection:
                installation_id = int(connection.execute(
                    """INSERT INTO community_installations(
                           community_id,platform,external_community_id,display_name,status
                       ) VALUES (1,'discord','guild-legacy','Legacy Guild','active')
                       RETURNING id"""
                ).fetchone()[0])
                account_id = int(connection.execute(
                    """INSERT INTO platform_accounts(platform,platform_user_id,username)
                       VALUES ('discord','legacy-user','Legacy User') RETURNING id"""
                ).fetchone()[0])
                observation_id = int(connection.execute(
                    """INSERT INTO observations(
                           platform,community_id,event_type,external_event_id,
                           actor_platform_account_id,container_id,context_id,occurred_at
                       ) VALUES (
                           'discord',1,'message.created','legacy-event',?,
                           'channel-1','guild-legacy','2026-09-02T00:00:00+00:00'
                       ) RETURNING id""",
                    (account_id,),
                ).fetchone()[0])
                message_id = int(connection.execute(
                    """INSERT INTO messages(
                           observation_id,platform,platform_message_id,
                           platform_account_id,community_id,channel_id,
                           content_raw,content_normalized,sent_at
                       ) VALUES (
                           ?,'discord','legacy-message',?,1,'channel-1',
                           'hello','hello','2026-09-02T00:00:00+00:00'
                       ) RETURNING id""",
                    (observation_id, account_id),
                ).fetchone()[0])
                action_id = int(connection.execute(
                    """INSERT INTO moderation_actions(
                           community_id,platform,message_id,target_platform_account_id,
                           action_type,actor_type,status
                       ) VALUES (1,'discord',?,?,'warn','system','completed')
                       RETURNING id""",
                    (message_id, account_id),
                ).fetchone()[0])
                archive_id = int(connection.execute(
                    """INSERT INTO raw_event_archive(
                           community_id,observation_id,platform,event_type,
                           external_event_id,payload_sha256,payload_json
                       ) VALUES (1,?,'discord','message.created','legacy-event','digest','{}')
                       RETURNING id""",
                    (observation_id,),
                ).fetchone()[0])
                connection.execute("DELETE FROM schema_migrations WHERE version=16")

            initialize_database(connection, force=True)
            for table_name, row_id in (
                ("observations", observation_id),
                ("messages", message_id),
                ("moderation_actions", action_id),
                ("raw_event_archive", archive_id),
            ):
                with self.subTest(table=table_name):
                    value = connection.execute(
                        f"SELECT installation_id FROM {table_name} WHERE id=?", (row_id,)
                    ).fetchone()[0]
                    self.assertEqual(int(value), installation_id)
            connection.close()

    def test_strict_tenant_migration_backfills_legacy_null_without_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "legacy-tenant.sqlite3"
            connection = connect_database(database_path)
            initialize_database(connection)
            with connection:
                connection.execute("DELETE FROM schema_migrations WHERE version=15")
                connection.execute("PRAGMA writable_schema=ON")
                connection.execute(
                    """UPDATE sqlite_schema
                       SET sql=replace(
                           sql,
                           'community_id INTEGER NOT NULL REFERENCES communities(id)',
                           'community_id INTEGER DEFAULT 1 REFERENCES communities(id)'
                       )
                       WHERE type='table' AND name='investigation_cases'"""
                )
                connection.execute("PRAGMA writable_schema=OFF")
                schema_version = int(
                    connection.execute("PRAGMA schema_version").fetchone()[0]
                )
                connection.execute(f"PRAGMA schema_version={schema_version + 1}")
            connection.close()

            connection = connect_database(database_path)
            try:
                with connection:
                    connection.execute(
                        """INSERT INTO investigation_cases(
                               community_id,title
                           ) VALUES (NULL,'Legacy case')"""
                    )
                initialize_database(connection, force=True)
                community_id = int(connection.execute(
                    "SELECT community_id FROM investigation_cases WHERE title='Legacy case'"
                ).fetchone()[0])
                community_column = next(
                    row for row in connection.execute(
                        "PRAGMA table_info(investigation_cases)"
                    ) if str(row[1]) == "community_id"
                )

                self.assertEqual(community_id, 1)
                self.assertEqual(int(community_column[3]), 1)
                self.assertIsNone(community_column[4])
                with self.assertRaises(Exception):
                    connection.execute(
                        "INSERT INTO investigation_cases(title) VALUES ('Missing tenant')"
                    )
            finally:
                connection.close()

    def test_strict_tenant_migration_rejects_unresolved_ownership(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "orphan-tenant.sqlite3"
            connection = connect_database(database_path)
            try:
                initialize_database(connection)
                with connection:
                    connection.execute("DELETE FROM schema_migrations WHERE version=15")
                connection.execute("PRAGMA foreign_keys=OFF")
                with connection:
                    connection.execute(
                        """INSERT INTO investigation_cases(
                               community_id,title
                           ) VALUES (999,'Orphan case')"""
                    )
                connection.execute("PRAGMA foreign_keys=ON")

                with self.assertRaisesRegex(
                    RuntimeError,
                    "unresolved tenant ownership: investigation_cases=1",
                ):
                    initialize_database(connection, force=True)
                applied = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=15"
                ).fetchone()[0]
                self.assertEqual(applied, 0)
            finally:
                connection.close()

    def test_tenant_owned_tables_are_not_null_without_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "strict-schema.sqlite3")
            try:
                initialize_database(connection)
                for table_name in (
                    "messages", "moderation_rules", "observations",
                    "intelligence_alerts", "investigation_cases",
                    "entity_relationships", "moderation_actions", "user_notes",
                    "emerging_topics", "topic_history", "topic_evidence",
                    "intelligence_reports",
                ):
                    with self.subTest(table=table_name):
                        column = next(
                            row for row in connection.execute(
                                f"PRAGMA table_info({table_name})"
                            ) if str(row[1]) == "community_id"
                        )
                        self.assertEqual(int(column[3]), 1)
                        self.assertIsNone(column[4])
            finally:
                connection.close()

    def test_settings_validate_with_web_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(self.build_env(tmpdir))
            self.assertEqual(settings.dashboard_port, 8080)
            self.assertEqual(settings.enabled_services, ("web", "jobs"))
            self.assertEqual(settings.twitch_channels, ("its_not_qwerty",))
            self.assertFalse(hasattr(settings, "discord_allow_bot_messages"))

    def test_tenant_policy_environment_is_not_application_configuration(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = AppSettings.from_env(
                self.build_env(tmpdir, QBOT_DISCORD_ALLOW_BOT_MESSAGES="true")
            )

        self.assertFalse(hasattr(settings, "discord_allow_bot_messages"))
        self.assertNotIn("discord_allow_bot_messages", settings.safe_summary())

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

    def test_release_installer_renders_python_systemd_template(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        service = (project_root / "deploy" / "systemd.service.template").read_text(
            encoding="utf-8"
        )
        installer = (project_root / "install.sh").read_text(encoding="utf-8")

        self.assertIn("ExecStart=__PYTHON__ -m src", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("-/var/lib/qbot4k", service)
        self.assertIn("-/var/backups/qbot4k", service)
        self.assertIn("systemd.service.template", installer)
        self.assertIn("/opt/qbot4k/current/.venv/bin/python", installer)
        self.assertIn("rm -f -- \"$UNIT_DROPIN_FILE\"", installer)

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
