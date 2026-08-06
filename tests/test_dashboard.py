from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import parse_qs, urlparse
from unittest import mock

from src.config import AppSettings
from src.db import connect_database, initialize_database
from src.discord import DiscordConnector
from src.health import create_health_server
from src.dashboard.auth import DiscordIdentity
from src.twitch import TwitchConnector


class NoRedirectHandler(HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
		return None


class DashboardTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tempdir = TemporaryDirectory()
		self.database_path = Path(self.tempdir.name) / "dashboard.sqlite3"
		self.settings = AppSettings.from_env(
			{
				"QBOT_DATABASE_PATH": str(self.database_path),
				"QBOT_ENABLED_SERVICES": "web,jobs",
				"QBOT_DASHBOARD_SESSION_SECRET": "session-secret",
				"QBOT_DISCORD_OAUTH_CLIENT_ID": "oauth-client-id",
				"QBOT_DISCORD_OAUTH_CLIENT_SECRET": "oauth-client-secret",
				"QBOT_DISCORD_OAUTH_REDIRECT_URI": "https://dashboard.example.test/oauth/discord/callback",
				"QBOT_OPERATOR_GUILD_IDS": "guild-1",
			}
		)
		self.settings = replace(self.settings, dashboard_port=0)
		self.server = create_health_server(self.settings, {"web": "ready", "jobs": "ready"})
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.base_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
		self.opener = build_opener(NoRedirectHandler())

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=5)
		self.tempdir.cleanup()

	def test_login_redirects_to_discord_oauth(self) -> None:
		with self.assertRaises(HTTPError) as error:
			self.opener.open(Request(f"{self.base_url}/login"))
		error.exception.close()

		self.assertEqual(error.exception.code, 302)
		location = error.exception.headers["Location"]
		self.assertIn("discord.com/oauth2/authorize", location)
		query = parse_qs(urlparse(location).query)
		self.assertEqual(query["redirect_uri"][0], "https://dashboard.example.test/oauth/discord/callback")
		self.assertIn("qbot4k_oauth_state=", error.exception.headers["Set-Cookie"])

	def test_oauth_callback_sets_session_and_unlocks_dashboard(self) -> None:
		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as error:
					self.opener.open(request)
				error.exception.close()

		self.assertEqual(error.exception.code, 302)
		cookies = error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		with self.opener.open(Request(f"{self.base_url}/api/overview", headers={"Cookie": cookie_value})) as response:
			payload = json.loads(response.read().decode("utf-8"))

		self.assertEqual(payload["overview"]["messages_total"], 0)
		self.assertEqual(payload["services"]["web"], "ready")

		with self.opener.open(Request(f"{self.base_url}/dashboard", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("QBot4K dashboard", body)
		self.assertIn("sam", body)

	def test_oauth_callback_accepts_signed_state_without_cookie(self) -> None:
		with self.assertRaises(HTTPError) as login_error:
			self.opener.open(Request(f"{self.base_url}/login"))
		login_error.exception.close()

		location = login_error.exception.headers["Location"]
		query = parse_qs(urlparse(location).query)
		state = query["state"][0]

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(f"{self.base_url}/oauth/discord/callback?code=abc&state={state}")
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		self.assertEqual(callback_error.exception.code, 302)
		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		with self.opener.open(Request(f"{self.base_url}/api/overview", headers={"Cookie": cookie_value})) as response:
			payload = json.loads(response.read().decode("utf-8"))

		self.assertEqual(payload["overview"]["messages_total"], 0)

	def test_users_page_lists_ingested_accounts(self) -> None:
		connector = DiscordConnector(self.database_path)
		connector.ingest_message(
			{
				"id": "discord-msg-1",
				"timestamp": "2026-08-06T05:00:00Z",
				"channel_id": "channel-1",
				"guild_id": "guild-1",
				"content": "hello",
				"author": {
					"id": "user-1",
					"username": "viewer_one",
					"bot": False,
				},
			}
		)

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		with self.opener.open(Request(f"{self.base_url}/users", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("viewer_one", body)

	def test_user_detail_page_shows_recent_messages(self) -> None:
		connector = DiscordConnector(self.database_path)
		connector.ingest_message(
			{
				"id": "discord-msg-2",
				"timestamp": "2026-08-06T05:10:00Z",
				"channel_id": "channel-77",
				"guild_id": "guild-1",
				"content": "this is my latest message",
				"author": {
					"id": "user-77",
					"username": "viewer_two",
					"bot": False,
				},
			}
		)

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		with self.opener.open(Request(f"{self.base_url}/users", headers={"Cookie": cookie_value})) as response:
			users_body = response.read().decode("utf-8")

		self.assertIn("/users/1", users_body)

		with self.opener.open(Request(f"{self.base_url}/users/1", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("viewer_two", body)
		self.assertIn("this is my latest message", body)

	def test_users_page_link_button_relinks_tagged_username(self) -> None:
		connector = DiscordConnector(self.database_path)
		connector.ingest_message(
			{
				"id": "discord-msg-link-1",
				"timestamp": "2026-08-06T05:20:00Z",
				"channel_id": "channel-1",
				"guild_id": "guild-1",
				"content": "hello from one",
				"author": {
					"id": "user-link-1",
					"username": "viewer_one",
					"bot": False,
				},
			}
		)
		connector.ingest_message(
			{
				"id": "discord-msg-link-2",
				"timestamp": "2026-08-06T05:21:00Z",
				"channel_id": "channel-2",
				"guild_id": "guild-1",
				"content": "hello from two",
				"author": {
					"id": "user-link-2",
					"username": "viewer_two",
					"bot": False,
				},
			}
		)

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		with self.opener.open(Request(f"{self.base_url}/users?link_user_id=1", headers={"Cookie": cookie_value})) as response:
			users_body = response.read().decode("utf-8")

		self.assertIn("Link Target", users_body)
		self.assertIn("Tag Link", users_body)

		request = Request(
			f"{self.base_url}/users/link",
			data=b"selected_user_id=1&usernames=viewer_two&platform=discord&q=",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_error:
			self.opener.open(request)
		link_error.exception.close()

		self.assertEqual(link_error.exception.code, 302)
		self.assertIn("link_status=", link_error.exception.headers["Location"])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			owner_rows = connection.execute(
				"""
				SELECT platform_user_id, user_id
				FROM platform_accounts
				WHERE platform = 'discord' AND platform_user_id IN ('user-link-1', 'user-link-2')
				ORDER BY platform_user_id
				"""
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(owner_rows[0][0], "user-link-1")
		self.assertEqual(owner_rows[0][1], 1)
		self.assertEqual(owner_rows[1][0], "user-link-2")
		self.assertEqual(owner_rows[1][1], 1)

	def test_users_link_by_username_links_all_matching_accounts(self) -> None:
		discord_connector = DiscordConnector(self.database_path)
		twitch_connector = TwitchConnector(self.database_path)

		discord_connector.ingest_message(
			{
				"id": "discord-multi-1",
				"timestamp": "2026-08-06T05:31:00Z",
				"channel_id": "channel-1",
				"guild_id": "guild-1",
				"content": "hello",
				"author": {
					"id": "discord-user-multi",
					"username": "shared_name",
					"bot": False,
				},
			}
		)
		twitch_connector.ingest_message(
			{
				"message_id": "twitch-multi-1",
				"timestamp": "2026-08-06T05:32:00Z",
				"channel": "its_not_qwerty",
				"content": "hello",
				"user_id": "twitch-user-multi",
				"username": "shared_name",
			}
		)

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		request = Request(
			f"{self.base_url}/users/link",
			data=b"selected_user_id=1&usernames=shared_name&platform=any&q=",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_error:
			self.opener.open(request)
		link_error.exception.close()

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			owners = connection.execute(
				"""
				SELECT platform, platform_user_id, user_id
				FROM platform_accounts
				WHERE username = 'shared_name'
				ORDER BY platform
				"""
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(len(owners), 2)
		self.assertEqual(owners[0][0], "discord")
		self.assertEqual(owners[0][2], 1)
		self.assertEqual(owners[1][0], "twitch")
		self.assertEqual(owners[1][2], 1)

	def test_users_link_promotes_unlinked_target_and_links_tagged_usernames(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"""
					INSERT INTO platform_accounts (
						platform,
						platform_user_id,
						username,
						guild_or_channel_context,
						user_id
					) VALUES (?, ?, ?, ?, NULL)
					""",
					("discord", "legacy-target", "legacy_target", "guild-1"),
				)
				connection.execute(
					"""
					INSERT INTO platform_accounts (
						platform,
						platform_user_id,
						username,
						guild_or_channel_context,
						user_id
					) VALUES (?, ?, ?, ?, NULL)
					""",
					("twitch", "legacy-other", "legacy_other", "its_not_qwerty"),
				)
		finally:
			connection.close()

		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="discord-access-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="123",
					username="sam",
					guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				request = Request(
					f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
					headers={"Cookie": "qbot4k_oauth_state=state-1"},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		cookie_value = session_cookie.split(";", 1)[0]

		request = Request(
			f"{self.base_url}/users/link",
			data=b"selected_user_id=-1&usernames=legacy_other&platform=any&q=",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_error:
			self.opener.open(request)
		link_error.exception.close()

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			rows = connection.execute(
				"""
				SELECT platform_user_id, user_id
				FROM platform_accounts
				WHERE platform_user_id IN ('legacy-target', 'legacy-other')
				ORDER BY platform_user_id
				"""
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0][0], "legacy-other")
		self.assertIsNotNone(rows[0][1])
		self.assertEqual(rows[1][0], "legacy-target")
		self.assertEqual(rows[0][1], rows[1][1])
