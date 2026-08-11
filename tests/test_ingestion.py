from __future__ import annotations

import json
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
from src.intelligence.powerusers import score_delta_for_message
from src.twitch import TwitchConnector, parse_twitch_irc_message
from tests.pipeline_support import ingest_and_analyze


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "ingestion.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_discord_message_ingestion_persists_account_and_message(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = ingest_and_analyze(connector, 
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
        self.assertEqual(user_row[1], 501)

    def test_scoring_avoids_substring_false_positives(self) -> None:
        self.assertEqual(
            score_delta_for_message("How many viewers are watching before stream?"),
            (1, "message_sent"),
        )

    def test_scoring_supports_conservative_fuzzy_negative_match(self) -> None:
        self.assertEqual(
            score_delta_for_message("you are an ashole"),
            (-10, "very_negative_content"),
        )

    def test_scoring_ignores_ambiguous_common_word_harder(self) -> None:
        self.assertEqual(
            score_delta_for_message("It is harder to grow on Twitch than YouTube."),
            (1, "message_sent"),
        )

    def test_scoring_ignores_ambiguous_neutral_terms(self) -> None:
        self.assertEqual(
            score_delta_for_message(
                "Our church group has asian and american members, and we discussed welfare policy."
            ),
            (1, "message_sent"),
        )

    def test_scoring_still_flags_clear_abusive_terms(self) -> None:
        self.assertEqual(
            score_delta_for_message("you are an asshole"),
            (-10, "very_negative_content"),
        )

    def test_positive_messages_give_small_social_score_increase(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
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

    def test_reply_to_non_bot_user_gives_larger_social_score_increase(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-reply-human",
                "timestamp": "2026-08-06T05:01:10Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "thanks for the tip",
                "author": {
                    "id": "user-reply-human",
                    "username": "sam",
                    "bot": False,
                },
                "referenced_message": {
                    "author": {
                        "id": "user-original",
                        "username": "alex",
                        "bot": False,
                    }
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-reply-human'
                """
            ).fetchone()[0]
            reason = connection.execute(
                """
                SELECT reputation_events.reason_code
                FROM reputation_events
                INNER JOIN users ON users.id = reputation_events.user_id
                INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-reply-human'
                ORDER BY reputation_events.id DESC
                LIMIT 1
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(score, 502)
        self.assertEqual(reason, "reply_to_non_bot")

    def test_reply_to_bot_user_keeps_standard_social_score_increase(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-reply-bot",
                "timestamp": "2026-08-06T05:01:20Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "thanks for the tip",
                "author": {
                    "id": "user-reply-bot",
                    "username": "sam",
                    "bot": False,
                },
                "referenced_message": {
                    "author": {
                        "id": "helper-bot",
                        "username": "helperbot",
                        "bot": True,
                    }
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-reply-bot'
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(score, 501)

    def test_welcoming_new_user_grants_small_bonus(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-welcome-1",
                "timestamp": "2026-08-06T05:01:30Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "welcome <@new-user-1>",
                "author": {
                    "id": "user-welcomer",
                    "username": "sam",
                    "bot": False,
                },
                "mentions": ["new-user-1"],
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-welcomer'
                """
            ).fetchone()[0]
            reasons = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT reason_code
                    FROM reputation_events
                    INNER JOIN users ON users.id = reputation_events.user_id
                    INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                    WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-welcomer'
                    ORDER BY reputation_events.id
                    """
                ).fetchall()
            ]
        finally:
            connection.close()

        self.assertEqual(score, 502)
        self.assertIn("welcome_new_user", reasons)

    def test_duplicate_welcome_same_target_is_penalized(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-welcome-dup-1",
                "timestamp": "2026-08-06T05:02:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "welcome <@new-user-2>",
                "author": {
                    "id": "user-welcomer-dup",
                    "username": "sam",
                    "bot": False,
                },
                "mentions": ["new-user-2"],
            }
        )
        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-welcome-dup-2",
                "timestamp": "2026-08-06T05:03:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "welcome again <@new-user-2>",
                "author": {
                    "id": "user-welcomer-dup",
                    "username": "sam",
                    "bot": False,
                },
                "mentions": ["new-user-2"],
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-welcomer-dup'
                """
            ).fetchone()[0]
            welcome_reasons = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT reputation_events.reason_code
                    FROM reputation_events
                    INNER JOIN users ON users.id = reputation_events.user_id
                    INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                    WHERE platform_accounts.platform = 'discord'
                      AND platform_accounts.platform_user_id = 'user-welcomer-dup'
                      AND reputation_events.reason_code LIKE 'welcome_%'
                    ORDER BY reputation_events.id
                    """
                ).fetchall()
            ]
        finally:
            connection.close()

        self.assertEqual(score, 500)
        self.assertEqual(welcome_reasons, ["welcome_new_user", "welcome_spam_duplicate"])

    def test_successful_server_bump_rewards_only_after_success_message(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-1",
                "timestamp": "2026-08-06T05:01:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "/bump",
                "author": {
                    "id": "user-bump",
                    "username": "sam",
                    "bot": False,
                },
            }
        )
        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-2",
                "timestamp": "2026-08-06T05:02:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "Bump done!",
                "author": {
                    "id": "bump-bot",
                    "username": "bumpbot",
                    "bot": True,
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-bump'
                """
            ).fetchone()[0]
            request_row = connection.execute(
                "SELECT status, command_name FROM server_boost_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(score, 502)
        self.assertEqual(request_row[0], "fulfilled")
        self.assertEqual(request_row[1], "/bump")

    def test_server_bump_reward_uses_interaction_user_when_command_not_literal(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-seed-user",
                "timestamp": "2026-08-06T05:01:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "thanks",
                "author": {
                    "id": "user-bump-interaction",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-success-interaction",
                "timestamp": "2026-08-06T05:02:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "Bump done!",
                "author": {
                    "id": "bump-bot",
                    "username": "bumpbot",
                    "bot": True,
                },
                "interaction_user_id": "user-bump-interaction",
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-bump-interaction'
                """
            ).fetchone()[0]
            request_row = connection.execute(
                "SELECT status, command_name FROM server_boost_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(score, 503)
        self.assertEqual(request_row[0], "fulfilled")
        self.assertEqual(request_row[1], "/bump")

    def test_server_bump_request_uses_interaction_command_name_when_content_is_empty(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-empty-content",
                "timestamp": "2026-08-06T05:03:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "</bump:1234567890>",
                "author": {
                    "id": "user-bump-empty",
                    "username": "sam",
                    "bot": False,
                },
                "interaction": {
                    "name": "bump",
                    "user": {"id": "user-bump-empty"},
                },
            }
        )

        self.assertEqual(result.status, "persisted")

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            request_row = connection.execute(
                "SELECT status, command_name FROM server_boost_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(request_row[0], "pending")
        self.assertEqual(request_row[1], "/bump")

    def test_server_bump_reward_accepts_broader_success_wording(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-success-wording-seed",
                "timestamp": "2026-08-06T05:04:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "thanks",
                "author": {
                    "id": "user-bump-success-wording",
                    "username": "sam",
                    "bot": False,
                },
            }
        )

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-bump-success-wording",
                "timestamp": "2026-08-06T05:05:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "Bump successful!",
                "author": {
                    "id": "bump-bot-alt",
                    "username": "bumpbot",
                    "bot": True,
                },
                "interaction_user_id": "user-bump-success-wording",
                "interaction_command_name": "bump",
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-bump-success-wording'
                """
            ).fetchone()[0]
            request_row = connection.execute(
                "SELECT status, command_name FROM server_boost_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(score, 503)
        self.assertEqual(request_row[0], "fulfilled")
        self.assertEqual(request_row[1], "/bump")

    def test_embed_only_boops_are_processed_from_live_gateway_payload(self) -> None:
        connector = DiscordConnector(self.database_path)
        gateway_payload = build_discord_message_payload(
            {
                "id": "discord-msg-boop-embed",
                "timestamp": "2026-08-10T05:05:00Z",
                "channel_id": "channel-boop",
                "guild_id": "guild-1",
                "content": "",
                "author": {
                    "id": "boop-bot",
                    "username": "BoopBot",
                    "bot": True,
                },
                "embeds": [
                    {
                        "title": "Server Boop",
                        "description": "Boop completed successfully!",
                    }
                ],
                "interaction_metadata": {
                    "name": "boop",
                    "user": {
                        "id": "user-boop-interaction",
                        "username": "boop_user",
                    },
                },
            }
        )

        result = ingest_and_analyze(connector, gateway_payload)

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            user = connection.execute(
                """
                SELECT users.current_reputation_score
                FROM users
                INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
                WHERE platform_accounts.platform = 'discord'
                  AND platform_accounts.platform_user_id = 'user-boop-interaction'
                """
            ).fetchone()
            request_row = connection.execute(
                "SELECT status, command_name FROM server_boost_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
            bot_account_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM platform_accounts WHERE platform_user_id = 'boop-bot'"
                ).fetchone()[0]
            )
            projected_bot_message_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE platform_message_id = 'discord-msg-boop-embed'"
                ).fetchone()[0]
            )
        finally:
            connection.close()

        self.assertEqual(result.status, "persisted")
        self.assertIsNotNone(user)
        self.assertEqual(int(user[0]), 502)
        self.assertEqual(tuple(request_row), ("fulfilled", "/boop"))
        self.assertEqual(bot_account_count, 0)
        self.assertEqual(projected_bot_message_count, 0)

    def test_very_negative_messages_cause_reputation_drop_without_moderation(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-negative",
                "timestamp": "2026-08-06T05:02:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "what an asshole",
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
            action_count = connection.execute(
                "SELECT COUNT(*) FROM moderation_actions"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertLess(score, 500)
        self.assertEqual(action_count, 0)

    def test_egregious_messages_trigger_automatic_moderation(self) -> None:
        connector = DiscordConnector(self.database_path)

        ingest_and_analyze(connector, 
            {
                "id": "discord-msg-egregious",
                "timestamp": "2026-08-06T05:03:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": "you are a nazi",
                "author": {
                    "id": "user-egregious",
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
                WHERE platform_accounts.platform = 'discord' AND platform_accounts.platform_user_id = 'user-egregious'
                """
            ).fetchone()[0]
            match_row = connection.execute(
                """
                SELECT rule_matches.reason_code, moderation_rules.rule_type
                FROM rule_matches
                INNER JOIN moderation_rules ON moderation_rules.id = rule_matches.moderation_rule_id
                ORDER BY rule_matches.id
                """
            ).fetchone()
            action_row = connection.execute(
                "SELECT action_type FROM moderation_actions ORDER BY id"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(score, 470)
        self.assertIsNotNone(match_row)
        self.assertEqual(match_row[0], "egregious_term")
        self.assertEqual(match_row[1], "egregious_term")
        self.assertIsNotNone(action_row)
        self.assertEqual(action_row[0], "timeout")

    def test_egregious_discord_messages_execute_pending_actions_immediately(self) -> None:
        connector = DiscordConnector(self.database_path, bot_token="discord-bot-token")

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        observed_requests: list[tuple[str, str]] = []

        def _fake_urlopen(request, timeout=15):
            observed_requests.append((request.method, request.full_url))
            return _FakeResponse()

        with mock.patch("src.discord.urlopen", side_effect=_fake_urlopen):
            ingest_and_analyze(connector, 
                {
                    "id": "discord-msg-egregious-exec",
                    "timestamp": "2026-08-06T05:03:30Z",
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "content": "you are a nazi",
                    "author": {
                        "id": "user-egregious-exec",
                        "username": "links776_",
                        "bot": False,
                    },
                }
            )

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            action_row = connection.execute(
                "SELECT action_type, status FROM moderation_actions ORDER BY id LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertIn(("DELETE", "https://discord.com/api/v10/channels/channel-1/messages/discord-msg-egregious-exec"), observed_requests)
        self.assertIn(("PATCH", "https://discord.com/api/v10/guilds/guild-1/members/user-egregious-exec"), observed_requests)
        self.assertEqual(action_row[0], "timeout")
        self.assertEqual(action_row[1], "completed")

    def test_egregious_discord_messages_log_embed_to_modlogs_when_available(self) -> None:
        connector = DiscordConnector(self.database_path, bot_token="discord-bot-token")

        class _FakeResponse:
            def __init__(self, body: str) -> None:
                self._body = body.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return self._body

        observed_requests: list[tuple[str, str, str | None]] = []

        def _fake_urlopen(request, timeout=15):
            body = request.data.decode("utf-8") if request.data is not None else None
            observed_requests.append((request.method, request.full_url, body))
            if request.method == "GET" and request.full_url.endswith("/guilds/guild-1/channels"):
                return _FakeResponse('[{"id":"modlogs-1","name":"modlogs","type":0}]')
            return _FakeResponse("{}")

        with mock.patch("src.discord.urlopen", side_effect=_fake_urlopen):
            ingest_and_analyze(connector, 
                {
                    "id": "discord-msg-egregious-modlogs",
                    "timestamp": "2026-08-06T05:03:45Z",
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "content": "you are a nazi",
                    "author": {
                        "id": "user-egregious-modlogs",
                        "username": "modlog_target",
                        "bot": False,
                    },
                }
            )

        self.assertIn(("GET", "https://discord.com/api/v10/guilds/guild-1/channels", None), observed_requests)

        modlog_posts = [
            req
            for req in observed_requests
            if req[0] == "POST" and req[1] == "https://discord.com/api/v10/channels/modlogs-1/messages"
        ]
        self.assertEqual(len(modlog_posts), 1)

        modlog_payload = json.loads(modlog_posts[0][2] or "{}")
        self.assertIn("embeds", modlog_payload)
        self.assertEqual(len(modlog_payload["embeds"]), 1)
        self.assertEqual(modlog_payload["embeds"][0]["title"], "Moderation Event")
        field_names = {field["name"] for field in modlog_payload["embeds"][0].get("fields", [])}
        self.assertIn("Action", field_names)
        self.assertIn("Reason", field_names)
        self.assertIn("Outcome", field_names)

    def test_egregious_discord_moderator_skips_timeout_but_deletes_message(self) -> None:
        connector = DiscordConnector(self.database_path, bot_token="discord-bot-token")

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        observed_requests: list[tuple[str, str]] = []

        def _fake_urlopen(request, timeout=15):
            observed_requests.append((request.method, request.full_url))
            return _FakeResponse()

        with mock.patch("src.discord.urlopen", side_effect=_fake_urlopen):
            result = ingest_and_analyze(connector, 
                {
                    "id": "discord-msg-egregious-mod",
                    "timestamp": "2026-08-06T05:03:40Z",
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "content": "hello there",
                    "author": {
                        "id": "user-egregious-mod",
                        "username": "mod_user",
                        "bot": False,
                    },
                    "role_names": ["Moderator"],
                }
            )

            connection = connect_database(self.database_path)
            try:
                initialize_database(connection)
                connection.execute(
                    """
                    INSERT INTO moderation_actions (
                        platform,
                        message_id,
                        target_platform_account_id,
                        action_type,
                        actor_type,
                        actor_id,
                        reason,
                        status
                    ) VALUES ('discord', ?, (SELECT id FROM platform_accounts WHERE platform = 'discord' AND platform_user_id = 'user-egregious-mod'), 'timeout', 'system', NULL, 'egregious_term', 'pending')
                    """,
                    (result.message_id,),
                )
                normalized = connector.__class__.__dict__["ingest_message"].__globals__["normalize_discord_message"](
                    {
                        "id": "discord-msg-egregious-mod",
                        "timestamp": "2026-08-06T05:03:40Z",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "content": "hello there",
                        "author": {
                            "id": "user-egregious-mod",
                            "username": "mod_user",
                            "bot": False,
                        },
                        "role_names": ["Moderator"],
                    }
                )
                connector._execute_pending_moderation_actions(connection, normalized, result)
                action_row = connection.execute(
                    "SELECT action_type, status FROM moderation_actions ORDER BY id LIMIT 1"
                ).fetchone()
            finally:
                connection.close()

        self.assertIn(("DELETE", "https://discord.com/api/v10/channels/channel-1/messages/discord-msg-egregious-mod"), observed_requests)
        self.assertNotIn(("PATCH", "https://discord.com/api/v10/guilds/guild-1/members/user-egregious-mod"), observed_requests)
        self.assertEqual(action_row[0], "timeout")
        self.assertEqual(action_row[1], "completed")

    def test_usernames_receive_the_same_evidence_based_default_on_ingestion(self) -> None:
        discord_connector = DiscordConnector(self.database_path)
        twitch_connector = TwitchConnector(self.database_path)

        ingest_and_analyze(discord_connector,
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
        ingest_and_analyze(twitch_connector,
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
        self.assertEqual(rows[0][1], 501)
        self.assertEqual(rows[0][2], 0)
        self.assertEqual(rows[1][0], "its_not_qwerty")
        self.assertEqual(rows[1][1], 501)
        self.assertEqual(rows[1][2], 0)

    def test_discord_message_ingestion_allows_empty_content(self) -> None:
        connector = DiscordConnector(self.database_path)

        result = ingest_and_analyze(connector, 
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

        result = ingest_and_analyze(connector, 
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
        ingest_and_analyze(connector, 
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
        ingest_and_analyze(connector, 
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

        self.assertLessEqual(score, 431)

    def test_twitch_message_ingestion_persists_normalized_fields(self) -> None:
        connector = TwitchConnector(self.database_path)

        result = ingest_and_analyze(connector, 
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

        first_result = ingest_and_analyze(connector, payload)
        second_result = ingest_and_analyze(connector, payload)

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

        result = ingest_and_analyze(connector, 
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

        ingest_and_analyze(connector, 
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
        ingest_and_analyze(connector, 
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

    def test_streamboo_viewers_detection_requires_both_terms(self) -> None:
        connector = TwitchConnector(self.database_path)

        self.assertTrue(connector._contains_streamboo_viewer_spam("Get viewers from streamboo now"))
        self.assertTrue(connector._contains_streamboo_viewer_spam("Get viewer from streamboo now"))
        self.assertTrue(connector._contains_streamboo_viewer_spam("BEST followers at STREAM-B00 now"))
        self.assertTrue(connector._contains_streamboo_viewer_spam("cheap viewers at s\u200bt\u200br\u200be\u200ba\u200bm\u200bb\u200bo\u200bo"))
        self.assertFalse(connector._contains_streamboo_viewer_spam("Get viewers now"))
        self.assertFalse(connector._contains_streamboo_viewer_spam("streamboo is bad"))

    def test_streamboo_rule_runs_through_live_analysis_and_action_pipeline(self) -> None:
        connector = TwitchConnector(self.database_path)

        result = ingest_and_analyze(connector,
            {
                "message_id": "twitch-msg-streamboo-1",
                "timestamp": "2026-08-06T05:30:00Z",
                "channel": "its_not_qwerty",
                "content": "Get the best viewers from streamboo.com now",
                "user_id": "viewer-streamboo-1",
                "username": "spam_viewer",
            }
        )
        self.assertEqual(result.status, "persisted")
        assert result.message_id is not None

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            rule_row = connection.execute(
                """SELECT moderation_rules.name, rule_matches.severity, rule_matches.reason_code,
                          rule_matches.recommended_action
                   FROM rule_matches JOIN moderation_rules ON moderation_rules.id=rule_matches.moderation_rule_id
                   WHERE rule_matches.message_id=?""",
                (result.message_id,),
            ).fetchone()
            action_row = connection.execute(
                """
                SELECT platform, message_id, action_type, reason, status
                FROM moderation_actions
                WHERE message_id=?
                """
                , (result.message_id,)
            ).fetchone()
            job_row = connection.execute(
                """SELECT stage, job_type, status FROM processing_jobs
                   WHERE observation_id=(SELECT observation_id FROM messages WHERE id=?)
                     AND stage='action'""", (result.message_id,),
            ).fetchone()
            alert_row = connection.execute(
                """SELECT alert_type, severity, title, confidence, status
                   FROM intelligence_alerts WHERE user_id=(SELECT user_id FROM messages WHERE id=?)""",
                (result.message_id,),
            ).fetchone()
            penalty_row = connection.execute(
                """SELECT delta, reason_code, source_type, source_id FROM reputation_events
                   WHERE user_id=(SELECT user_id FROM messages WHERE id=?) AND source_type='moderation'""",
                (result.message_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(tuple(rule_row), ("builtin:streamboo_viewer_spam", "high", "streamboo_viewer_spam", "timeout"))
        self.assertEqual(action_row[0], "twitch")
        self.assertEqual(action_row[1], result.message_id)
        self.assertEqual(action_row[2], "timeout")
        self.assertEqual(action_row[3], "streamboo_viewer_spam")
        self.assertEqual(action_row[4], "pending")
        self.assertEqual(tuple(job_row), ("action", "twitch.moderation.execute", "pending"))
        self.assertEqual(tuple(alert_row), ("moderation_finding", "high", "Streamboo Viewer Spam", 1.0, "open"))
        self.assertEqual(tuple(penalty_row), (-70, "moderation_penalty", "moderation", result.message_id))

    def test_streamboo_rule_exempts_moderators(self) -> None:
        connector = TwitchConnector(self.database_path)

        result = ingest_and_analyze(connector,
            {
                "message_id": "twitch-msg-streamboo-2",
                "timestamp": "2026-08-06T05:31:00Z",
                "channel": "its_not_qwerty",
                "content": "buy viewer from streamboo",
                "user_id": "viewer-streamboo-2",
                "username": "spam_viewer_two",
                "is_moderator": True,
            }
        )
        self.assertEqual(result.status, "persisted")
        assert result.message_id is not None

        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            match_count = int(connection.execute("SELECT COUNT(*) FROM rule_matches WHERE message_id=?", (result.message_id,)).fetchone()[0])
            action_count = int(connection.execute("SELECT COUNT(*) FROM moderation_actions WHERE message_id=?", (result.message_id,)).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(match_count, 0)
        self.assertEqual(action_count, 0)


if __name__ == "__main__":
    unittest.main()
