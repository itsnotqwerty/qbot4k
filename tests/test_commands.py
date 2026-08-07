from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.db import (
	connect_database,
	ensure_platform_account,
	initialize_database,
	upsert_command_definition,
	upsert_operator_account,
	upsert_simple_command_definition,
)
from src.discord import DiscordConnector
from src.commands import CommandContext, build_default_command_registry, render_command_reply
from src.twitch import TwitchConnector, TwitchConnectionError
from src.intelligence.userprofiles import create_canonical_user, link_platform_account


class _FakeResponse:
	def __enter__(self) -> _FakeResponse:
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		return None

	def read(self) -> bytes:
		return b"{}"


class DiscordCommandTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tempdir = TemporaryDirectory()
		self.database_path = Path(self.tempdir.name) / "commands.sqlite3"

	def tearDown(self) -> None:
		self.tempdir.cleanup()

	def test_credit_command_renders_profile_embed(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam", current_reputation_score=735)
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				username="sam",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				user_id=user_id,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="discord",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="discord-user-1",
				author_username="sam",
				channel_id="channel-1",
				guild_id="guild-1",
				message_id="message-1",
				content="!credit",
			)
			response = registry.dispatch("!credit", context)
		finally:
			connection.close()

		self.assertIsNotNone(response)
		assert response is not None
		embed = render_command_reply(response, "discord")["embeds"][0]
		self.assertEqual(embed["title"], "Social Credit Profile")
		self.assertEqual(embed["fields"][0]["name"], "Social Credit")
		self.assertEqual(embed["fields"][0]["value"], "735")
		self.assertEqual(embed["fields"][1]["value"], "Yes")
		self.assertIn("discord: sam", embed["fields"][2]["value"])

	def test_credit_command_renders_plaintext_for_twitch(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam", current_reputation_score=735)
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				username="sam",
				guild_or_channel_context="its_not_qwerty",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				user_id=user_id,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-1",
				content="!credit",
			)
			reply = registry.dispatch("!credit", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		text = render_command_reply(reply, "twitch")
		self.assertIsInstance(text, str)
		self.assertIn("Social Credit Profile", text)
		self.assertIn("Power User: Yes", text)
		self.assertIn("twitch: sam", text)

	def test_discord_connector_posts_credit_command_response(self) -> None:
		captured_request = None

		def _fake_urlopen(request, timeout=15):
			nonlocal captured_request
			captured_request = request
			return _FakeResponse()

		connector = DiscordConnector(self.database_path, bot_token="discord-bot-token")
		with mock.patch("src.discord.urlopen", side_effect=_fake_urlopen):
			connector.ingest_message(
				{
					"id": "discord-msg-credit-1",
					"timestamp": "2026-08-06T05:00:00Z",
					"channel_id": "channel-1",
					"guild_id": "guild-1",
					"content": "!credit",
					"author": {
						"id": "discord-user-1",
						"username": "sam",
						"bot": False,
					},
				}
			)

		self.assertIsNotNone(captured_request)
		assert captured_request is not None
		self.assertEqual(captured_request.method, "POST")
		self.assertIn("/channels/channel-1/messages", captured_request.full_url)
		payload = json.loads(captured_request.data.decode("utf-8"))
		self.assertEqual(payload["embeds"][0]["title"], "Social Credit Profile")
		self.assertEqual(payload["embeds"][0]["fields"][0]["name"], "Social Credit")

	def test_twitch_connector_sends_plaintext_credit_response(self) -> None:
		captured_messages: list[str] = []

		connector = TwitchConnector(self.database_path)
		result = connector.ingest_message(
			{
				"message_id": "twitch-msg-credit-1",
				"timestamp": "2026-08-06T05:10:00Z",
				"channel": "its_not_qwerty",
				"content": "!credit",
				"user_id": "twitch-user-1",
				"username": "sam",
			}
			,
			reply_sink=captured_messages.append,
		)

		self.assertEqual(result.status, "persisted")
		self.assertEqual(len(captured_messages), 1)
		self.assertIn("Social Credit Profile", captured_messages[0])
		self.assertIn("Power User: No", captured_messages[0])

	def test_twitch_run_forever_sends_plaintext_credit_response(self) -> None:
		captured_lines: list[str] = []
		irc_reader = mock.Mock()
		irc_reader.readline.side_effect = [
			"@badge-info=;badges=;color=#1E90FF;display-name=sam;user-id=twitch-user-1;tmi-sent-ts=1722921000000 :sam!sam@sam.tmi.twitch.tv PRIVMSG #its_not_qwerty :!credit\r\n",
			"",
		]

		irc_socket = mock.MagicMock()
		irc_socket.makefile.return_value = irc_reader
		irc_socket.sendall.side_effect = lambda data: captured_lines.append(data.decode("utf-8").rstrip("\r\n"))
		irc_socket.__enter__.return_value = irc_socket
		irc_socket.__exit__.return_value = None

		raw_socket = mock.MagicMock()
		raw_socket.__enter__.return_value = raw_socket
		raw_socket.__exit__.return_value = None

		ssl_context = mock.Mock()
		ssl_context.wrap_socket.return_value = irc_socket

		connector = TwitchConnector(self.database_path)
		with mock.patch.object(connector, "_validate_token_and_get_login", return_value="qbot4k"):
			with mock.patch.object(connector, "configured_channels", return_value=("its_not_qwerty",)):
				with mock.patch("src.twitch.socket.create_connection", return_value=raw_socket):
					with mock.patch("src.twitch.ssl.create_default_context", return_value=ssl_context):
						with self.assertRaises(TwitchConnectionError):
							connector.run_forever("oauth:test-token")

		self.assertTrue(any(line.startswith("PRIVMSG #its_not_qwerty :") for line in captured_lines))
		self.assertTrue(any("Social Credit Profile" in line for line in captured_lines))

	def test_simple_command_renders_plaintext_and_embed(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="wave",
				response_template="Hello {author_username} from {platform}",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-2",
				content="!wave",
			)
			reply = registry.dispatch("!wave", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "Hello sam from twitch")
		discord_payload = render_command_reply(reply, "discord")
		self.assertIsInstance(discord_payload, dict)
		self.assertEqual(discord_payload["content"], "Hello sam from twitch")
		self.assertNotIn("embeds", discord_payload)

	def test_reserved_command_names_are_rejected(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with self.assertRaises(ValueError):
				upsert_command_definition(
					connection,
					command_name="addcom",
					title="Add Command",
					description_template="ignored",
					footer_template=None,
					enabled=True,
				)
			with self.assertRaises(ValueError):
				upsert_simple_command_definition(
					connection,
					command_name="delcom",
					response_template="ignored",
					enabled=True,
				)
		finally:
			connection.close()

	def test_command_editing_builtins_manage_custom_commands(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				username="sam",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				user_id=user_id,
			)
			upsert_operator_account(
				connection,
				discord_user_id="discord-user-1",
				discord_username="sam",
				role="admin",
			)
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				username="sam",
				guild_or_channel_context="its_not_qwerty",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				user_id=user_id,
			)
			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-3",
				content="!addcom !website https://gatewaycorporate.org/",
			)
			reply = registry.dispatch("!addcom !website https://gatewaycorporate.org/", context)
			added_reply = render_command_reply(reply, "twitch")
			self.assertIsInstance(added_reply, str)
			self.assertIn("Added !website", added_reply)

			reply = registry.dispatch("!website", context)
			self.assertIsNotNone(reply)
			assert reply is not None
			self.assertEqual(render_command_reply(reply, "twitch"), "https://gatewaycorporate.org/")

			reply = registry.dispatch("!editcom !website https://gatewaycorporate.org/new", context)
			edited_reply = render_command_reply(reply, "twitch")
			self.assertIsInstance(edited_reply, str)
			self.assertIn("Updated !website", edited_reply)

			reply = registry.dispatch("!website", context)
			self.assertIsNotNone(reply)
			assert reply is not None
			self.assertEqual(render_command_reply(reply, "twitch"), "https://gatewaycorporate.org/new")

			reply = registry.dispatch("!delcom !website", context)
			deleted_reply = render_command_reply(reply, "twitch")
			self.assertIsInstance(deleted_reply, str)
			self.assertIn("Deleted !website", deleted_reply)

			reply = registry.dispatch("!website", context)
			self.assertIsNone(reply)
		finally:
			connection.close()


if __name__ == "__main__":
	unittest.main()