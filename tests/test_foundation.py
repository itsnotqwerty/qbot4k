from __future__ import annotations

import json
import logging
import signal
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from src.config import AppSettings, ConfigError
from src.__main__ import _install_shutdown_handlers, _restore_shutdown_handlers
from src.db import connect_database, initialize_database, list_tables
from src.health import create_health_server


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
                    QBOT_ENABLED_SERVICES="twitch",
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
                        QBOT_ENABLED_SERVICES="twitch",
                        QBOT_TWITCH_BOT_TOKEN="token",
                        QBOT_TWITCH_REFRESH_TOKEN="refresh-token",
                    )
                )

            settings = AppSettings.from_env(
                self.build_env(
                    tmpdir,
                    QBOT_ENABLED_SERVICES="twitch",
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


if __name__ == "__main__":
    unittest.main()