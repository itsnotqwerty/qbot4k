from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from src.config import AppSettings, ConfigError
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


if __name__ == "__main__":
    unittest.main()