from __future__ import annotations

import json
import unittest
from dataclasses import replace
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
from src.discord import DiscordConnector, normalize_discord_message
from src.commands import CommandContext, _format_command_template, build_default_command_registry, render_command_reply
from src.twitch import TwitchConnector, TwitchConnectionError, normalize_twitch_message
from tests.pipeline_support import ingest_and_analyze
from src.intelligence.community import (
	create_community,
	create_organization,
	create_workspace,
	register_installation,
	set_operator_permission_override,
)
from src.intelligence.onboarding import configure_welcome, queue_member_welcome, save_onboarding_resource
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

	def test_verify_command_enforces_tenant_self_service_gates(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			operator_id = upsert_operator_account(
				connection, discord_user_id="operator-1", discord_username="operator", role="admin"
			)
			organization_id = create_organization(connection, name="Org", slug="org")
			workspace_id = create_workspace(
				connection, organization_id=organization_id, name="Workspace", slug="workspace"
			)
			community_id = create_community(
				connection, workspace_id=workspace_id, name="Community", slug="community"
			)
			installation_id = register_installation(
				connection, community_id=community_id, platform="discord",
				external_community_id="guild-verify", display_name="Guild Verify",
			)
			configure_welcome(
				connection, community_id=community_id, discord_installation_id=installation_id,
				welcome_channel_id="welcome-1", welcome_template="Welcome {mention}", enabled=True,
				operator_id=operator_id, self_service_verification_enabled=False,
			)
			queue_member_welcome(
				connection, community_id=community_id, guild_id="guild-verify", user_id="member-1",
				username="member", event_id="join-1", occurred_at="2026-01-01T00:00:00+00:00",
			)
			context = CommandContext(
				platform="discord", database_path=self.database_path, connection=connection,
				author_platform_user_id="member-1", author_username="member", channel_id="welcome-1",
				guild_id="guild-verify", message_id="message-verify", content="!verify",
				community_id=community_id,
			)
			registry = build_default_command_registry()
			disabled_reply = registry.dispatch("!verify", context)
			self.assertIn("disabled", str(render_command_reply(disabled_reply, "discord")))

			configure_welcome(
				connection, community_id=community_id, discord_installation_id=installation_id,
				welcome_channel_id="welcome-1", welcome_template="Welcome {mention}", enabled=True,
				operator_id=operator_id, self_service_verification_enabled=True,
				verification_resource_enabled=True, verification_resource_url="https://example.com/start",
			)
			save_onboarding_resource(
				connection, community_id=community_id, operator_id=operator_id, title="Rules",
				resource_url="https://example.com/rules",
				message_template="{mention}: {title} {resource_url}",
			)
			success_reply = registry.dispatch("!verify", context)
			self.assertIn("Verification complete", str(render_command_reply(success_reply, "discord")))
			member = connection.execute(
				"SELECT status,verified_by_operator_id FROM community_onboarding_members WHERE community_id=? AND platform_user_id=?",
				(community_id, "member-1"),
			).fetchone()
			self.assertEqual((member[0], member[1]), ("verified", None))
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM audit_log WHERE action_type='onboarding.member_self_verified' AND actor_id='member-1'"
			).fetchone()[0], 1)
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM community_announcements WHERE community_id=? AND json_extract(source_json,'$.type') LIKE 'member_verification%'",
				(community_id,),
			).fetchone()[0], 2)
			repeated_reply = registry.dispatch("!verify", context)
			self.assertIn("No pending", str(render_command_reply(repeated_reply, "discord")))
			wrong_tenant = registry.dispatch("!verify", replace(context, guild_id="other-guild"))
			self.assertIn("No pending", str(render_command_reply(wrong_tenant, "discord")))
			queue_member_welcome(
				connection, community_id=community_id, guild_id="guild-verify", user_id="member-2",
				username="member-two", event_id="join-2", occurred_at="2026-01-02T00:00:00+00:00",
			)
			configure_welcome(
				connection, community_id=community_id, discord_installation_id=installation_id,
				welcome_channel_id="welcome-1", welcome_template="Welcome {mention}", enabled=True,
				operator_id=operator_id, self_service_verification_enabled=True,
				verification_evidence_required=True,
			)
			evidence_reply = registry.dispatch(
				"!verify", replace(context, author_platform_user_id="member-2")
			)
			self.assertIn("evidence is required", str(render_command_reply(evidence_reply, "discord")))
			self.assertEqual(connection.execute(
				"SELECT status FROM community_onboarding_members WHERE community_id=? AND platform_user_id=?",
				(community_id, "member-2"),
			).fetchone()[0], "newcomer")
		finally:
			connection.close()

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
			ingest_and_analyze(connector, 
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

	def test_connector_command_dispatch_propagates_installation_tenant(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			organization_id = create_organization(connection, name="Commands", slug="commands")
			workspace_id = create_workspace(
				connection, organization_id=organization_id, name="Commands", slug="commands"
			)
			community_id = create_community(
				connection, workspace_id=workspace_id, name="Second", slug="second"
			)
			register_installation(
				connection, community_id=community_id, platform="discord",
				external_community_id="guild-second", display_name="Guild Second",
			)
			register_installation(
				connection, community_id=community_id, platform="twitch",
				external_community_id="channel-second", display_name="Channel Second",
			)
			registry = mock.Mock(wraps=build_default_command_registry())
			registry.dispatch.return_value = None
			DiscordConnector(
				self.database_path, bot_token="token", command_registry=registry
			)._dispatch_registered_command(connection, normalize_discord_message({
				"id": "discord-command", "timestamp": "2026-08-06T05:00:00Z",
				"channel_id": "channel-1", "guild_id": "guild-second", "content": "!credit",
				"author": {"id": "member-1", "username": "member"},
			}))
			TwitchConnector(
				self.database_path, command_registry=registry
			)._dispatch_registered_command(connection, normalize_twitch_message({
				"message_id": "twitch-command", "timestamp": "2026-08-06T05:00:00Z",
				"channel": "channel-second", "content": "!credit",
				"user_id": "member-1", "username": "member",
			}))
		finally:
			connection.close()

		contexts = [call.args[1] for call in registry.dispatch.call_args_list]
		self.assertEqual([context.community_id for context in contexts], [community_id, community_id])

	def test_twitch_connector_sends_plaintext_credit_response(self) -> None:
		captured_messages: list[str] = []

		connector = TwitchConnector(self.database_path)
		result = ingest_and_analyze(connector, 
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
		captured_messages: list[str] = []
		connector = TwitchConnector(self.database_path)
		result = ingest_and_analyze(
			connector,
			{
				"message_id": "twitch-msg-credit-runner",
				"timestamp": "2026-08-06T05:10:00Z",
				"channel": "its_not_qwerty",
				"content": "!credit",
				"user_id": "twitch-user-1",
				"username": "sam",
			},
			reply_sink=captured_messages.append,
		)

		self.assertEqual(result.status, "persisted")
		self.assertEqual(len(captured_messages), 1)
		self.assertIn("Social Credit Profile", captured_messages[0])

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
				community_id=1,
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

	def test_command_editing_requires_tenant_scoped_capability(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			operator_id = upsert_operator_account(
				connection, discord_user_id="viewer-1", discord_username="viewer", role="viewer"
			)
			registry = build_default_command_registry()
			context = CommandContext(
				platform="discord", database_path=self.database_path, connection=connection,
				author_platform_user_id="viewer-1", author_username="viewer",
				channel_id="channel-1", guild_id="guild-1", message_id="message-1",
				content="!addcom !status ready", community_id=1,
			)

			denied = registry.dispatch(context.content, context)
			missing_tenant = registry.dispatch(context.content, replace(context, community_id=None))
			set_operator_permission_override(
				connection, operator_id=operator_id, community_id=1,
				permission="settings.manage", decision="grant", actor_operator_id=operator_id,
			)
			allowed = registry.dispatch(context.content, context)
		finally:
			connection.close()

		self.assertIn("restricted", str(render_command_reply(denied, "discord")))
		self.assertIn("restricted", str(render_command_reply(missing_tenant, "discord")))
		self.assertIn("Added !status", str(render_command_reply(allowed, "discord")))

	def test_alias_command_duplicates_existing_simple_command(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-alias-1",
				username="sam",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-alias-1",
				user_id=user_id,
			)
			upsert_operator_account(
				connection,
				discord_user_id="discord-user-alias-1",
				discord_username="sam",
				role="admin",
			)
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-alias-1",
				username="sam",
				guild_or_channel_context="its_not_qwerty",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-alias-1",
				user_id=user_id,
			)
			upsert_simple_command_definition(
				connection,
				command_name="website",
				response_template="https://gatewaycorporate.org/",
				enabled=True,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-alias-1",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-alias-1",
				content="!alias !site !website",
				community_id=1,
			)

			reply = registry.dispatch("!alias !site !website", context)
			self.assertIsNotNone(reply)
			assert reply is not None
			self.assertIn("Aliased !site to !website", render_command_reply(reply, "twitch"))

			aliased_reply = registry.dispatch("!site", context)
			self.assertIsNotNone(aliased_reply)
			assert aliased_reply is not None
			self.assertEqual(render_command_reply(aliased_reply, "twitch"), "https://gatewaycorporate.org/")
		finally:
			connection.close()

	def test_alias_command_requires_existing_source_command(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-alias-2",
				username="sam",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-alias-2",
				user_id=user_id,
			)
			upsert_operator_account(
				connection,
				discord_user_id="discord-user-alias-2",
				discord_username="sam",
				role="admin",
			)
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-alias-2",
				username="sam",
				guild_or_channel_context="its_not_qwerty",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-alias-2",
				user_id=user_id,
			)

			registry = build_default_command_registry()
			context = CommandContext(
				platform="twitch",
				database_path=self.database_path,
				connection=connection,
				author_platform_user_id="twitch-user-alias-2",
				author_username="sam",
				channel_id="its_not_qwerty",
				guild_id=None,
				message_id="message-alias-2",
				content="!alias !site !missing",
				community_id=1,
			)

			reply = registry.dispatch("!alias !site !missing", context)
			self.assertIsNotNone(reply)
			assert reply is not None
			self.assertIn("!missing does not exist.", render_command_reply(reply, "twitch"))
		finally:
			connection.close()


if __name__ == "__main__":
	unittest.main()
