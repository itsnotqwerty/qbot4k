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
from src.commands import CommandContext, _format_command_template, build_default_command_registry, render_command_reply
from src.twitch import TwitchConnector, TwitchConnectionError
from src.intelligence.userprofiles import create_canonical_user, link_platform_account


class _FakeResponse:
	def __enter__(self) -> _FakeResponse:
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		return None

	def read(self) -> bytes:
		return b"{}"


class _FakeHttpTemplateResponse:
	def __init__(self, body: bytes) -> None:
		self._body = body

	def __enter__(self) -> _FakeHttpTemplateResponse:
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		return None

	def read(self, _size: int = -1) -> bytes:
		return self._body


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

	def test_template_http_macros_support_all_methods(self) -> None:
		captured_methods: list[str] = []

		def _fake_urlopen(request, timeout=5):
			captured_methods.append(str(getattr(request, "method", "")))
			return _FakeHttpTemplateResponse(f"ok-{request.method}".encode("utf-8"))

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="httpcheck",
				response_template=(
					"{GET}(https://example.test/get) "
					"{POST}(https://example.test/post) "
					"{PUT}(https://example.test/put) "
					"{DELETE}(https://example.test/delete)"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-1",
				content="!httpcheck",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!httpcheck", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(
			render_command_reply(reply, "twitch"),
			"ok-GET ok-POST ok-PUT ok-DELETE",
		)
		self.assertEqual(captured_methods, ["GET", "POST", "PUT", "DELETE"])

	def test_template_http_macro_failure_returns_empty_string(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="httpfallback",
				response_template="prefix {GET}(https://example.test/fail) suffix",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-2",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-2",
				content="!httpfallback",
			)
			with mock.patch("src.commands.urlopen", side_effect=ValueError("boom")):
				reply = registry.dispatch("!httpfallback", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "prefix  suffix")

	def test_template_http_macro_failure_initializes_alias_variables(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="httpaliasfail",
				response_template=(
					"{GET}(https://example.test/fail)[threads:data.threads,posts:data.posts] "
					"{threads} threads and {posts} posts"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-2b",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-2b",
				content="!httpaliasfail",
			)
			with mock.patch("src.commands.urlopen", side_effect=ValueError("boom")):
				reply = registry.dispatch("!httpaliasfail", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), " {threads} threads and {posts} posts")

	def test_template_http_macro_extracts_json_path_value(self) -> None:
		def _fake_urlopen(_request, timeout=5):
			return _FakeHttpTemplateResponse(
				b'{"data":{"profile":{"name":"sam","scores":[5,7,9]}}}'
			)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsonpath",
				response_template=(
					"name={GET}(https://example.test/user)[data.profile.name] "
					"score={GET}(https://example.test/user)[data.profile.scores.1]"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-3",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-3",
				content="!jsonpath",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsonpath", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "name=sam score=7")

	def test_template_http_macro_extracts_multiple_variables_from_single_request(self) -> None:
		captured_urls: list[str] = []

		def _fake_urlopen(request, timeout=5):
			captured_urls.append(str(request.full_url))
			return _FakeHttpTemplateResponse(
				b'{"data":{"profile":{"name":"sam","scores":[5,7,9]}}}'
			)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsonmap",
				response_template=(
					"{GET}(https://example.test/user)"
					"[name:data.profile.name,score:data.profile.scores.1]"
					"name={name} score={score}"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-3b",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-3b",
				content="!jsonmap",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsonmap", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "name=sam score=7")
		self.assertEqual(captured_urls, ["https://example.test/user"])

	def test_template_http_macro_selector_map_supports_equals_separator(self) -> None:
		def _fake_urlopen(_request, timeout=5):
			return _FakeHttpTemplateResponse(
				b'{"data":{"profile":{"name":"sam","scores":[5,7,9]}}}'
			)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsonmapeq",
				response_template=(
					"{GET}(https://example.test/user)"
					"[name=data.profile.name,score=data.profile.scores.1]"
					"name={name} score={score}"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-3d",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-3d",
				content="!jsonmapeq",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsonmapeq", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "name=sam score=7")

	def test_template_http_macro_selector_map_supports_semicolon_separator(self) -> None:
		def _fake_urlopen(_request, timeout=5):
			return _FakeHttpTemplateResponse(
				b'{"data":{"profile":{"name":"sam","scores":[5,7,9]}}}'
			)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsonmapsemi",
				response_template=(
					"{GET}(https://example.test/user)"
					"[name:data.profile.name;score:data.profile.scores.1]"
					"name={name} score={score}"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-3e",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-3e",
				content="!jsonmapsemi",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsonmapsemi", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "name=sam score=7")

	def test_template_http_macro_caches_duplicate_request_in_single_render(self) -> None:
		call_count = 0

		def _fake_urlopen(_request, timeout=5):
			nonlocal call_count
			call_count += 1
			return _FakeHttpTemplateResponse(b'{"data":{"profile":{"name":"sam","scores":[5,7,9]}}}')

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsoncache",
				response_template=(
					"name={GET}(https://example.test/user)[data.profile.name] "
					"score={GET}(https://example.test/user)[data.profile.scores.1]"
				),
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-3c",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-3c",
				content="!jsoncache",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsoncache", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "name=sam score=7")
		self.assertEqual(call_count, 1)

	def test_template_http_macro_missing_json_path_returns_empty_string(self) -> None:
		def _fake_urlopen(_request, timeout=5):
			return _FakeHttpTemplateResponse(b'{"data":{"profile":{"name":"sam"}}}')

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="jsonmissing",
				response_template="value={GET}(https://example.test/user)[data.profile.missing]",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-4",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-4",
				content="!jsonmissing",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!jsonmissing", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "value=")

	def test_template_http_macro_substitutes_query_in_url(self) -> None:
		captured_urls: list[str] = []

		def _fake_urlopen(request, timeout=5):
			captured_urls.append(str(request.full_url))
			return _FakeHttpTemplateResponse(b'{"data":{"ok":true}}')

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="lookup",
				response_template="{GET}(https://example.test/search?q={query})[data.ok]",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-5",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-5",
				content="!lookup who is sam?",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!lookup who is sam?", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "true")
		self.assertEqual(captured_urls, ["https://example.test/search?q=who+is+sam%3F"])

	def test_template_http_macro_sends_user_agent_header(self) -> None:
		captured_user_agent: list[str] = []

		def _fake_urlopen(request, timeout=5):
			captured_user_agent.append(str(request.get_header("User-agent") or ""))
			return _FakeHttpTemplateResponse(b'{"data":{"ok":true}}')

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="useragent",
				response_template="{GET}(https://example.test/ok)[data.ok]",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-6",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-6",
				content="!useragent",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!useragent", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "true")
		self.assertEqual(captured_user_agent, ["qbot4k/1.0 (+https://example.invalid/qbot4k)"])

	def test_template_http_macro_extracts_json_path_from_large_payload(self) -> None:
		payload_items = [{"q": "filler"} for _ in range(120)]
		payload_items[119]["q"] = "tail-value"
		large_payload = json.dumps(payload_items, separators=(",", ":")).encode("utf-8")

		def _fake_urlopen(_request, timeout=5):
			return _FakeHttpTemplateResponse(large_payload)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="largejson",
				response_template="{GET}(https://example.test/quotes)[119.q]",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-http-7",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-http-7",
				content="!largejson",
			)
			with mock.patch("src.commands.urlopen", side_effect=_fake_urlopen):
				reply = registry.dispatch("!largejson", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "tail-value")

	def test_template_random_range_supports_inclusive_bounds(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="rng",
				response_template="roll={0..49}",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-rng-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-rng-1",
				content="!rng",
			)
			with mock.patch("src.commands.random.randint", return_value=49) as randint_mock:
				reply = registry.dispatch("!rng", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "roll=49")
		randint_mock.assert_called_once_with(0, 49)

	def test_template_random_range_handles_reversed_bounds(self) -> None:
		with mock.patch("src.commands.random.randint", return_value=3) as randint_mock:
			rendered = _format_command_template("value={5..1}")

		self.assertEqual(rendered, "value=3")
		randint_mock.assert_called_once_with(1, 5)

	def test_template_random_range_supports_query_bound(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_simple_command_definition(
				connection,
				command_name="randq",
				response_template="idx={0..{query}}",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-rng-2",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-rng-2",
				content="!randq 49",
			)
			with mock.patch("src.commands.random.randint", return_value=17) as randint_mock:
				reply = registry.dispatch("!randq 49", context)
		finally:
			connection.close()

		self.assertIsNotNone(reply)
		assert reply is not None
		self.assertEqual(render_command_reply(reply, "twitch"), "idx=17")
		randint_mock.assert_called_once_with(0, 49)

	def test_template_random_range_query_bound_sanitizes_non_numeric(self) -> None:
		with mock.patch("src.commands.random.randint", return_value=0) as randint_mock:
			rendered = _format_command_template("idx={0..{query}}", query="oops")

		self.assertEqual(rendered, "idx=0")
		randint_mock.assert_called_once_with(0, 0)

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