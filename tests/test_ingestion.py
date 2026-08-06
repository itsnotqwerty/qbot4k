from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.db import (
    connect_database,
    initialize_database,
    list_twitch_channels,
    upsert_moderation_rule,
)
from src.discord import (
    DiscordAuthError,
    DiscordConnectionError,
    DiscordConnector,
    build_discord_message_payload,
)
from src.twitch import TwitchConnector, parse_twitch_irc_message


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "ingestion.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_discord_message_ingestion_persists_account_and_message(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = connector.ingest_message(
            {
                "id": "discord-msg-1",
                "timestamp": "2026-08-06T05:00:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": " Hello   World ",
                "author": {
                    "id": "user-1",
                    "username": "sam",
                    "bot": False,
                },
                "role_names": ["Moderator"],
            }
        )

        self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            account = connection.execute(
                "SELECT id, platform, platform_user_id, username, guild_or_channel_context, user_id FROM platform_accounts"
            ).fetchone()
            message = connection.execute(
                "SELECT platform, platform_message_id, channel_id, content_raw, content_normalized, sent_at FROM messages"
            ).fetchone()
            user_row = connection.execute(
                "SELECT id, current_reputation_score FROM users WHERE id = ?",
                (account[5],),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(account[1], "discord")
        self.assertEqual(account[2], "user-1")
        self.assertEqual(account[3], "sam")
        self.assertEqual(account[4], "guild-1")
        self.assertIsNotNone(account[5])
        self.assertEqual(message[0], "discord")
        self.assertEqual(message[1], "discord-msg-1")
        self.assertEqual(message[2], "channel-1")
        self.assertEqual(message[3], " Hello   World ")
        self.assertEqual(message[4], "hello world")
        self.assertTrue(message[5].endswith("+00:00"))
        self.assertEqual(user_row[1], 500)

    def test_positive_messages_give_small_social_score_increase(self) -> None:
        connector = DiscordConnector(self.database_path)

        connector.ingest_message(
            {
                "id": "discord-msg-positive",
                "timestamp": "2026-08-06T05:01:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "thanks, great stream",
                "author": {
                    "id": "user-positive",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            score = connection.execute(
                """
                SELECT users.current_reputation_score
                FROM users
                INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-positive'
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(score, 501)

    def test_very_negative_messages_cause_significant_social_score_drop(self) -> None:
        connector = DiscordConnector(self.database_path)

        connector.ingest_message(
            {
                "id": "discord-msg-negative",
                "timestamp": "2026-08-06T05:02:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "you are trash",
                "author": {
                    "id": "user-negative",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            score = connection.execute(
                """
                SELECT users.current_reputation_score
                FROM users
                INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-negative'
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertLessEqual(score, 470)

    def test_priority_usernames_get_max_default_social_score_on_ingestion(self) -> None:
        discord_connector = DiscordConnector(self.database_path)
        twitch_connector = TwitchConnector(self.database_path)

        discord_connector.ingest_message(
            {
                "id": "discord-priority-1",
                "timestamp": "2026-08-06T05:03:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "hello",
                "author": {
                    "id": "priority-discord",
                    "username": "apollyon",
                    "bot": False,
                },
            }
        )
        twitch_connector.ingest_message(
            {
                "message_id": "twitch-priority-1",
                "timestamp": "2026-08-06T05:04:00Z",
                "channel": "its_not_qwerty",
                "content": "hello",
                "user_id": "priority-twitch",
                "username": "its_not_qwerty",
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            rows = connection.execute(
                """
                SELECT platform_accounts.username, users.current_reputation_score, users.candidate_flag
                FROM platform_accounts
                INNER JOIN users ON users.id = platform_accounts.user_id
                WHERE platform_accounts.username IN ('apollyon', 'its_not_qwerty')
                ORDER BY platform_accounts.username
                """
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "apollyon")
        self.assertEqual(rows[0][1], 900)
        self.assertEqual(rows[0][2], 1)
        self.assertEqual(rows[1][0], "its_not_qwerty")
        self.assertEqual(rows[1][1], 900)
        self.assertEqual(rows[1][2], 1)

    def test_discord_message_ingestion_allows_empty_content(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = connector.ingest_message(
            {
                "id": "discord-msg-empty",
                "timestamp": "2026-08-06T05:05:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "",
                "author": {
                    "id": "user-1",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            row = connection.execute(
                "SELECT content_raw, content_normalized FROM messages WHERE platform_message_id = ?",
                ("discord-msg-empty",),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(row[0], "")
        self.assertEqual(row[1], "")

    def test_build_discord_message_payload_extracts_gateway_fields(self) -> None:
        payload = build_discord_message_payload(
            {
                "id": "discord-msg-3",
                "timestamp": "2026-08-06T05:15:00Z",
                "guild_id": "guild-1",
                "channel_id": "channel-1",
                "content": "hello",
                "author": {
                    "id": "user-2",
                    "username": "sam",
                    "global_name": "sammy",
                    "bot": False,
                },
                "member": {"roles": ["role-1", "role-2"]},
            }
        )

        self.assertEqual(payload["author"]["username"], "sam")
        self.assertEqual(payload["role_names"], ("role-1", "role-2"))
        self.assertEqual(payload["guild_id"], "guild-1")

    def test_discord_auth_error_stops_reconnect_loop(self) -> None:
        connector = DiscordConnector(self.database_path)

        with mock.patch.object(
            connector,
            "_connect_and_listen",
            side_effect=DiscordAuthError("Failed to fetch Discord gateway URL: HTTP 403"),
        ):
            connector.run_forever("discord-token")

        self.assertEqual(connector.health_snapshot().status, "auth_failed")

    def test_discord_identify_payload_uses_raw_token(self) -> None:
        connector = DiscordConnector(self.database_path)

        payload = connector._build_identify_payload("discord-token")

        self.assertEqual(payload["op"], 2)
        self.assertEqual(payload["d"]["token"], "discord-token")

    def test_discord_websocket_close_message_includes_code_and_reason(self) -> None:
        connector = DiscordConnector(self.database_path)

        fake_ws = mock.Mock()
        fake_ws.recv_frame.side_effect = [
            mock.Mock(opcode=1, data='{"op": 10, "d": {"heartbeat_interval": 1000}}'),
            mock.Mock(opcode=8, data=bytes([0x0F, 0xBE]) + b"Disallowed intents"),
        ]

        with mock.patch.object(connector, "_fetch_gateway_url", return_value="wss://gateway.discord.gg/?v=10&encoding=json"):
            with mock.patch("src.discord.create_connection", return_value=fake_ws):
                with self.assertRaises(DiscordConnectionError) as error:
                    connector._connect_and_listen("discord-token")

        self.assertIn("code=4030", str(error.exception))
        self.assertIn("reason=Disallowed intents", str(error.exception))

    def test_discord_bot_messages_are_ignored_by_default(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = connector.ingest_message(
            {
                "id": "discord-msg-2",
                "timestamp": "2026-08-06T05:00:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "bot post",
                "author": {
                    "id": "bot-1",
                    "username": "helper-bot",
                    "bot": True,
                },
            }
        )

        self.assertEqual(result.status, "ignored")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 0)

    def test_moderation_rules_create_review_items_and_actions(self) -> None:
        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            upsert_moderation_rule(
                connection,
                name="blacklisted_term",
                rule_type="exact_term",
                pattern="spoiler",
                severity="medium",
            )
            upsert_moderation_rule(
                connection,
                name="link_block",
                rule_type="link_restriction",
                pattern="",
                severity="high",
                auto_enforce_action="timeout",
            )
        finally:
            connection.close()

        connector = DiscordConnector(self.database_path)
        connector.ingest_message(
            {
                "id": "discord-msg-rule-1",
                "timestamp": "2026-08-06T05:20:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "This has a spoiler and http://example.com",
                "author": {
                    "id": "user-3",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            rule_match_rows = connection.execute(
                "SELECT severity, reason_code FROM rule_matches ORDER BY id"
            ).fetchall()
            review_rows = connection.execute(
                "SELECT severity, queue_reason_code FROM review_queue ORDER BY id"
            ).fetchall()
            action_rows = connection.execute(
                "SELECT action_type, reason FROM moderation_actions ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(len(rule_match_rows), 2)
        self.assertEqual(rule_match_rows[0][0], "medium")
        self.assertEqual(rule_match_rows[0][1], "exact_term")
        self.assertEqual(rule_match_rows[1][0], "high")
        self.assertEqual(rule_match_rows[1][1], "link_restriction")
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0][0], "medium")
        self.assertEqual(review_rows[0][1], "exact_term")
        self.assertEqual(len(action_rows), 1)
        self.assertEqual(action_rows[0][0], "timeout")
        self.assertEqual(action_rows[0][1], "link_restriction")

    def test_moderation_punishment_causes_significant_social_score_drop(self) -> None:
        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            upsert_moderation_rule(
                connection,
                name="link_block",
                rule_type="link_restriction",
                pattern="",
                severity="high",
                auto_enforce_action="timeout",
            )
        finally:
            connection.close()

        connector = DiscordConnector(self.database_path)
        connector.ingest_message(
            {
                "id": "discord-msg-rule-penalty",
                "timestamp": "2026-08-06T05:25:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "http://example.com",
                "author": {
                    "id": "user-penalty",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            score = connection.execute(
                """
                SELECT users.current_reputation_score
                FROM users
                INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-penalty'
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertLessEqual(score, 430)

    def test_twitch_message_ingestion_persists_normalized_fields(self) -> None:
        connector = TwitchConnector(self.database_path)

        result = connector.ingest_message(
            {
                "message_id": "twitch-msg-1",
                "timestamp": "2026-08-06T05:10:00Z",
                "channel": "sam_channel",
                "content": "Visit My Stream",
                "user_id": "viewer-1",
                "username": "viewer",
                "badges": ["vip"],
            }
        )

        self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            row = connection.execute(
                "SELECT platform, platform_message_id, channel_id, content_normalized FROM messages"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(row[0], "twitch")
        self.assertEqual(row[1], "twitch-msg-1")
        self.assertEqual(row[2], "sam_channel")
        self.assertEqual(row[3], "visit my stream")

    def test_duplicate_platform_message_is_reported_without_new_row(self) -> None:
        connector = TwitchConnector(self.database_path)
        payload = {
            "message_id": "dup-msg",
            "timestamp": "2026-08-06T05:10:00Z",
            "channel": "sam_channel",
            "content": "Repeat",
            "user_id": "viewer-1",
            "username": "viewer",
        }

        first_result = connector.ingest_message(payload)
        second_result = connector.ingest_message(payload)

        self.assertEqual(first_result.status, "persisted")
        self.assertEqual(second_result.status, "duplicate")
        self.assertEqual(first_result.message_id, second_result.message_id)

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 1)

    def test_join_command_in_its_not_qwerty_channel_registers_requesting_user_channel(self) -> None:
        connector = TwitchConnector(self.database_path)

        result = connector.ingest_message(
            {
                "message_id": "join-msg-1",
                "timestamp": "2026-08-06T05:12:00Z",
                "channel": "its_not_qwerty",
                "content": "!join",
                "user_id": "viewer-42",
                "username": "new_streamer",
            }
        )

        self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            channels = list_twitch_channels(connection)
        finally:
            connection.close()

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channel_name"], "new_streamer")
        self.assertEqual(channels[0]["join_source"], "command")
        self.assertEqual(channels[0]["status"], "requested")
        self.assertEqual(channels[0]["request_source_message_id"], result.message_id)

    def test_join_command_outside_qwerty_channel_does_not_register_channel(self) -> None:
        connector = TwitchConnector(self.database_path)

        connector.ingest_message(
            {
                "message_id": "join-msg-2",
                "timestamp": "2026-08-06T05:13:00Z",
                "channel": "somewhere_else",
                "content": "!join",
                "user_id": "viewer-42",
                "username": "new_streamer",
            }
        )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            channels = list_twitch_channels(connection)
        finally:
            connection.close()

        self.assertEqual(channels, [])

    def test_bootstrap_channels_are_stored_in_database(self) -> None:
        connector = TwitchConnector(
            self.database_path,
            bootstrap_channels=("sam_channel", "another_channel"),
        )

        channels = connector.configured_channels()

        self.assertEqual(channels, ("another_channel", "sam_channel"))

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            rows = list_twitch_channels(connection, status="active")
        finally:
            connection.close()

        self.assertEqual(len(rows), 2)

    def test_default_bootstrap_channel_is_its_not_qwerty(self) -> None:
        connector = TwitchConnector(
            self.database_path,
            bootstrap_channels=("its_not_qwerty",),
        )

        channels = connector.configured_channels()

        self.assertEqual(channels, ("its_not_qwerty",))

    def test_parse_twitch_irc_message_extracts_privmsg_fields(self) -> None:
        raw_line = (
            "@badges=vip/1;color=#1E90FF;display-name=NewStreamer;id=msg-1;mod=0;"
            "tmi-sent-ts=1754456400000;user-id=viewer-42 "
            ":newstreamer!newstreamer@newstreamer.tmi.twitch.tv PRIVMSG #its_not_qwerty :!join"
        )

        payload = parse_twitch_irc_message(raw_line)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["message_id"], "msg-1")
        self.assertEqual(payload["channel"], "its_not_qwerty")
        self.assertEqual(payload["content"], "!join")
        self.assertEqual(payload["user_id"], "viewer-42")
        self.assertEqual(payload["username"], "NewStreamer")
        self.assertEqual(payload["badges"], ("vip",))

    def test_mark_channel_active_updates_joined_channel_status(self) -> None:
        connector = TwitchConnector(self.database_path)
        connector.ingest_message(
            {
                "message_id": "join-msg-3",
                "timestamp": "2026-08-06T05:14:00Z",
                "channel": "its_not_qwerty",
                "content": "!join",
                "user_id": "viewer-42",
                "username": "new_streamer",
            }
        )

        connector._mark_channel_active("new_streamer")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            channels = list_twitch_channels(connection)
        finally:
            connection.close()

        self.assertEqual(channels[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()