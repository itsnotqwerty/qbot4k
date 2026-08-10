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
from src.db import connect_database, initialize_database, upsert_service_reliability_bucket
from src.discord import DiscordConnector
from src.health import create_health_server
from src.dashboard.auth import DiscordIdentity
from src.twitch import TwitchConnector
from tests.pipeline_support import ingest_and_analyze


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

	def _issue_operator_session_cookie(self) -> str:
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
		return session_cookie.split(";", 1)[0]

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

	def test_oauth_callback_hides_upstream_error_details(self) -> None:
		with mock.patch(
			"src.dashboard.server.exchange_discord_code_for_token",
			side_effect=ValueError("Discord token exchange failed: HTTP 401 - invalid_client"),
		):
			with self.assertRaises(HTTPError) as error:
				self.opener.open(
					Request(
						f"{self.base_url}/oauth/discord/callback?code=abc&state=state-1",
						headers={"Cookie": "qbot4k_oauth_state=state-1"},
					)
				)
			body = error.exception.read().decode("utf-8")
			error.exception.close()

		self.assertEqual(error.exception.code, 502)
		self.assertEqual(body, "Discord OAuth failed")

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

	def test_dashboard_overview_shows_discord_and_twitch_status_indicators(self) -> None:
		health_settings = replace(self.settings, enabled_services=("web", "jobs", "discord", "twitch"), dashboard_port=0)
		service_started_at = {
			"web": "2026-08-06T00:00:00+00:00",
			"jobs": "2026-08-06T00:00:00+00:00",
			"discord": "2026-08-06T00:00:00+00:00",
			"twitch": "2026-08-06T00:00:00+00:00",
		}
		health_server = create_health_server(
			health_settings,
			{"web": "ready", "jobs": "ready", "discord": "ready", "twitch": "down"},
			service_started_at=service_started_at,
			app_started_at="2026-08-06T00:00:00+00:00",
		)
		health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
		health_thread.start()
		base_url = f"http://{health_server.server_address[0]}:{health_server.server_address[1]}"
		opener = build_opener(NoRedirectHandler())

		try:
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
						f"{base_url}/oauth/discord/callback?code=abc&state=state-1",
						headers={"Cookie": "qbot4k_oauth_state=state-1"},
					)
					with self.assertRaises(HTTPError) as callback_error:
						opener.open(request)
					callback_error.exception.close()

			cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
			session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
			cookie_value = session_cookie.split(";", 1)[0]

			with opener.open(Request(f"{base_url}/dashboard", headers={"Cookie": cookie_value})) as response:
				body = response.read().decode("utf-8")

			self.assertIn("Connector Status", body)
			self.assertIn("Connected and authenticated", body)
			self.assertIn("Down", body)
		finally:
			health_server.shutdown()
			health_server.server_close()
			health_thread.join(timeout=5)

	def test_api_health_reflects_live_service_state_mutations(self) -> None:
		live_states = {"web": "ready", "jobs": "ready", "discord": "idle", "twitch": "idle"}
		health_settings = replace(self.settings, enabled_services=("web", "jobs", "discord", "twitch"), dashboard_port=0)
		health_server = create_health_server(health_settings, live_states)
		health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
		health_thread.start()
		base_url = f"http://{health_server.server_address[0]}:{health_server.server_address[1]}"
		opener = build_opener(NoRedirectHandler())

		try:
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
						f"{base_url}/oauth/discord/callback?code=abc&state=state-1",
						headers={"Cookie": "qbot4k_oauth_state=state-1"},
					)
					with self.assertRaises(HTTPError) as callback_error:
						opener.open(request)
					callback_error.exception.close()

			cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
			session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
			cookie_value = session_cookie.split(";", 1)[0]

			live_states["discord"] = "ready"
			live_states["twitch"] = "ready"

			with opener.open(Request(f"{base_url}/api/health", headers={"Cookie": cookie_value})) as response:
				payload = json.loads(response.read().decode("utf-8"))

			self.assertEqual(payload["services"]["discord"], "ready")
			self.assertEqual(payload["services"]["twitch"], "ready")
		finally:
			health_server.shutdown()
			health_server.server_close()
			health_thread.join(timeout=5)

	def test_system_health_page_and_api_include_uptime_details(self) -> None:
		cookie_value = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			upsert_service_reliability_bucket(
				connection,
				service_name="system",
				bucket_start="2026-08-06T12:00:00+00:00",
				is_up=True,
				status="ready",
			)
			upsert_service_reliability_bucket(
				connection,
				service_name="system",
				bucket_start="2026-08-06T12:01:00+00:00",
				is_up=False,
				status="down",
			)
			upsert_service_reliability_bucket(
				connection,
				service_name="system",
				bucket_start="2026-08-06T12:02:00+00:00",
				is_up=False,
				status="down",
			)
			upsert_service_reliability_bucket(
				connection,
				service_name="system",
				bucket_start="2026-08-06T12:03:00+00:00",
				is_up=True,
				status="ready",
			)
		finally:
			connection.close()

		with self.opener.open(Request(f"{self.base_url}/system-health", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("System health", body)
		self.assertIn("App uptime", body)
		self.assertIn("Database", body)
		self.assertIn("Each bar is 1 minute. Green = uptime, red = downtime.", body)
		self.assertIn("reliability-bar up", body)
		self.assertIn("reliability-bar down", body)
		self.assertIn("Outage start", body)
		self.assertIn("2m", body)

		with self.opener.open(Request(f"{self.base_url}/api/health", headers={"Cookie": cookie_value})) as response:
			payload = json.loads(response.read().decode("utf-8"))

		self.assertIn("services_detail", payload)
		self.assertIn("uptime", payload)
		self.assertIn("app_uptime_seconds", payload["uptime"])

	def test_users_page_lists_ingested_accounts(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
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

	def test_signals_page_user_detail_and_apis_show_derived_signals(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(
			connector,
			{
				"id": "dashboard-signal-message",
				"timestamp": "2026-08-09T12:00:00Z",
				"channel_id": "channel-1",
				"guild_id": "guild-1",
				"content": "you are an asshole",
				"author": {"id": "dashboard-signal-user", "username": "signal_target", "bot": False},
			},
		)
		connection = connect_database(self.database_path)
		try:
			user_id = int(connection.execute("SELECT id FROM users WHERE primary_display_name = 'signal_target'").fetchone()[0])
		finally:
			connection.close()

		cookie = self._issue_operator_session_cookie()
		signal_query = "signal=risk.composite&signal=activity.message_count&sort=evidence&dir=asc"
		with self.opener.open(Request(f"{self.base_url}/signals?{signal_query}", headers={"Cookie": cookie})) as response:
			signals_body = response.read().decode("utf-8")
		with self.opener.open(Request(f"{self.base_url}/users/{user_id}", headers={"Cookie": cookie})) as response:
			user_body = response.read().decode("utf-8")
		with self.opener.open(Request(f"{self.base_url}/api/signals?{signal_query}", headers={"Cookie": cookie})) as response:
			signals_api = json.loads(response.read().decode("utf-8"))
		with self.opener.open(Request(f"{self.base_url}/api/users/{user_id}", headers={"Cookie": cookie})) as response:
			user_api = json.loads(response.read().decode("utf-8"))

		self.assertIn("Derived signals", signals_body)
		self.assertIn("signal_target", signals_body)
		self.assertIn("Composite risk", signals_body)
		self.assertIn("multiple", signals_body)
		self.assertIn("Ctrl/Cmd-click for multiple", signals_body)
		self.assertIn("sort=value", signals_body)
		self.assertIn("sort=confidence", signals_body)
		self.assertIn("sort=evidence", signals_body)
		self.assertIn("sort=timestamp", signals_body)
		self.assertIn("Derived signals", user_body)
		self.assertIn("Negative message ratio", user_body)
		self.assertEqual(len(signals_api["items"]), 2)
		self.assertEqual(signals_api["filters"]["signals"], ["risk.composite", "activity.message_count"])
		self.assertEqual(signals_api["sort"], {"by": "evidence", "dir": "asc"})
		self.assertEqual(len(user_api["signals"]), 9)
		self.assertEqual(user_api["signals"][0]["signal_key"], "risk.composite")

	def test_users_api_supports_sorting_by_requested_fields(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-anna-1",
				"timestamp": "2026-08-06T05:00:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "anna one",
				"author": {
					"id": "sort-anna",
					"username": "anna",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-anna-2",
				"timestamp": "2026-08-06T05:01:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "anna two",
				"author": {
					"id": "sort-anna",
					"username": "anna",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-anna-3",
				"timestamp": "2026-08-06T05:02:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "anna three",
				"author": {
					"id": "sort-anna",
					"username": "anna",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-brad-1",
				"timestamp": "2026-08-06T05:03:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "brad one",
				"author": {
					"id": "sort-brad",
					"username": "brad",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-carl-1",
				"timestamp": "2026-08-06T05:04:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "carl one",
				"author": {
					"id": "sort-carl",
					"username": "carl",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-sort-carl-2",
				"timestamp": "2026-08-06T05:05:00Z",
				"channel_id": "channel-sort",
				"guild_id": "guild-1",
				"content": "carl two",
				"author": {
					"id": "sort-carl",
					"username": "carl",
					"bot": False,
				},
			}
		)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"UPDATE users SET current_reputation_score = 100, candidate_flag = 0 WHERE primary_display_name = 'anna'"
				)
				connection.execute(
					"UPDATE users SET current_reputation_score = 900, candidate_flag = 1 WHERE primary_display_name = 'brad'"
				)
				connection.execute(
					"UPDATE users SET current_reputation_score = 500, candidate_flag = 0 WHERE primary_display_name = 'carl'"
				)
				brad_id_row = connection.execute(
					"SELECT id FROM users WHERE primary_display_name = 'brad'"
				).fetchone()
				self.assertIsNotNone(brad_id_row)
				connection.execute(
					"""
					INSERT INTO platform_accounts (platform, platform_user_id, username, user_id)
					VALUES ('twitch', 'sort-brad-alt', 'brad_alt', ?)
					""",
					(int(brad_id_row[0]),),
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

		def fetch_sorted_names(sort: str, direction: str = "desc") -> list[str]:
			with self.opener.open(
				Request(
					f"{self.base_url}/api/users?sort={sort}&dir={direction}",
					headers={"Cookie": cookie_value},
				)
			) as response:
				payload = json.loads(response.read().decode("utf-8"))
			return [item["primary_display_name"] for item in payload["items"]]

		self.assertEqual(fetch_sorted_names("name", "asc")[:3], ["anna", "brad", "carl"])
		self.assertEqual(fetch_sorted_names("score", "desc")[0], "brad")
		self.assertEqual(fetch_sorted_names("messages", "desc")[0], "anna")
		self.assertEqual(fetch_sorted_names("accounts", "desc")[0], "brad")
		self.assertEqual(fetch_sorted_names("poweruser", "desc")[0], "brad")

	def test_user_detail_page_shows_recent_messages(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
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
		self.assertIn("Profile summary", body)
		self.assertIn("Reputation", body)
		self.assertIn(">501</div>", body)

	def test_user_detail_page_resolves_discord_channel_name(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
			{
				"id": "discord-msg-resolve-1",
				"timestamp": "2026-08-06T05:10:00Z",
				"channel_id": "channel-99",
				"guild_id": "guild-1",
				"content": "resolve this channel",
				"author": {
					"id": "user-99",
					"username": "viewer_three",
					"bot": False,
				},
			}
		)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"""
					INSERT INTO discord_channels (channel_id, guild_id, channel_name, channel_type)
					VALUES ('channel-99', 'guild-1', 'lounge', 0)
					"""
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

		with self.opener.open(Request(f"{self.base_url}/users/1", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("#lounge", body)

	def test_user_detail_page_renders_discord_attachments_as_numbered_links(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
			{
				"id": "discord-msg-attach-1",
				"timestamp": "2026-08-06T05:10:00Z",
				"channel_id": "channel-55",
				"guild_id": "guild-1",
				"content": "check this",
				"author": {
					"id": "user-55",
					"username": "viewer_attach",
					"bot": False,
				},
				"attachments": [
					"https://cdn.discordapp.com/attachments/a/file1.png",
					"https://cdn.discordapp.com/attachments/a/file2.png",
				],
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

		with self.opener.open(Request(f"{self.base_url}/users/1", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("[1]</a>", body)
		self.assertIn("[2]</a>", body)
		self.assertIn("https://cdn.discordapp.com/attachments/a/file1.png", body)
		self.assertIn("https://cdn.discordapp.com/attachments/a/file2.png", body)

	def test_user_detail_page_supports_operator_moderation_actions(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
			{
				"id": "discord-msg-mod-1",
				"timestamp": "2026-08-06T05:11:00Z",
				"channel_id": "channel-mod-1",
				"guild_id": "guild-1",
				"content": "moderation check",
				"author": {
					"id": "user-mod-1",
					"username": "viewer_mod",
					"bot": False,
				},
			}
		)
		ingest_and_analyze(connector, 
			{
				"id": "discord-msg-review-1",
				"timestamp": "2026-08-06T05:12:00Z",
				"channel_id": "channel-review-1",
				"guild_id": "guild-1",
				"content": "review me",
				"author": {
					"id": "user-review-1",
					"username": "viewer_review",
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

		with self.opener.open(Request(f"{self.base_url}/users/1", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("Moderation status", body)
		self.assertIn("Apply Action", body)

		request = Request(
			f"{self.base_url}/users/1/moderation",
			data=b"target_platform_account_id=1&action_type=timeout&reason=repeat+spam",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as mod_error:
			self.opener.open(request)
		mod_error.exception.close()

		self.assertEqual(mod_error.exception.code, 302)
		self.assertIn("mod_status=", mod_error.exception.headers["Location"])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			action_row = connection.execute(
				"""
				SELECT platform, target_platform_account_id, action_type, actor_type, reason, status
				FROM moderation_actions
				ORDER BY id DESC
				LIMIT 1
				"""
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(action_row[0], "discord")
		self.assertEqual(action_row[1], 1)
		self.assertEqual(action_row[2], "timeout")
		self.assertEqual(action_row[3], "operator")
		self.assertEqual(action_row[4], "repeat spam")
		self.assertEqual(action_row[5], "completed")

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			review_message_row = connection.execute(
				"SELECT id FROM messages WHERE platform_message_id = ?",
				("discord-msg-review-1",),
			).fetchone()
			self.assertIsNotNone(review_message_row)
			with connection:
				connection.execute(
					"""
					INSERT INTO review_queue (
						message_id,
						status,
						severity,
						queue_reason_code,
						assigned_operator_id,
						created_at,
						resolved_at
					) VALUES (?, 'open', ?, ?, NULL, CURRENT_TIMESTAMP, NULL)
					""",
					(int(review_message_row[0]), "high", "rule_spam"),
				)
		finally:
			connection.close()

		with self.opener.open(Request(f"{self.base_url}/moderation", headers={"Cookie": cookie_value})) as response:
			moderation_body = response.read().decode("utf-8")

		self.assertIn("Recent actions", moderation_body)
		self.assertIn("viewer_mod", moderation_body)
		self.assertIn("Open reviews", moderation_body)
		self.assertIn("viewer_review", moderation_body)

	def test_users_page_link_button_relinks_tagged_username(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, 
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
		ingest_and_analyze(connector, 
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

	def test_commands_page_updates_credit_template_for_twitch_and_discord(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			connection.execute(
				"""
				INSERT INTO command_definitions (command_name, title, description_template, footer_template, enabled)
				VALUES ('addcom', 'Add Command', 'ignored', NULL, 1)
				"""
			)
			connection.execute(
				"""
				INSERT INTO simple_command_definitions (command_name, response_template, enabled)
				VALUES ('delcom', 'ignored', 1)
				"""
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

		with self.opener.open(Request(f"{self.base_url}/commands", headers={"Cookie": cookie_value})) as response:
			body = response.read().decode("utf-8")

		self.assertIn("Command menu", body)
		self.assertIn("<main class='main command-page'>", body)
		self.assertIn("New Command", body)
		self.assertIn("Built-Ins", body)
		self.assertIn("Plaintext Commands", body)
		self.assertIn("Templating Information", body)
		self.assertIn("credit", body)
		self.assertNotIn("!new", body)
		self.assertNotIn("addcom", body)
		self.assertNotIn("delcom", body)
		self.assertNotIn("editcom", body)

		request = Request(
			f"{self.base_url}/commands",
			data=b"command_name=credit&title=Credit+Ledger&description_template=Profile+for+%7Bdisplay_name%7D&footer_template=Twitch+profile+for+%7Bauthor_username%7D&enabled=1",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as save_error:
			self.opener.open(request)
		save_error.exception.close()
		self.assertEqual(save_error.exception.code, 302)
		self.assertIn("/commands?status=Saved%20builtin%20command%20credit", save_error.exception.headers["Location"])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			command_row = connection.execute(
				"SELECT title, description_template, footer_template, enabled FROM command_definitions WHERE command_name = 'credit'"
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(command_row[0], "Credit Ledger")
		self.assertEqual(command_row[1], "Profile for {display_name}")
		self.assertEqual(command_row[2], "Twitch profile for {author_username}")
		self.assertEqual(command_row[3], 1)

		request = Request(
			f"{self.base_url}/commands",
			data=b"record_type=simple&command_name=wave&response_template=Hello+%7Bauthor_username%7D+from+%7Bplatform%7D&enabled=1",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as simple_save_error:
			self.opener.open(request)
		simple_save_error.exception.close()
		self.assertEqual(simple_save_error.exception.code, 302)
		self.assertIn("/commands?status=Saved%20simple%20command%20wave", simple_save_error.exception.headers["Location"])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			simple_row = connection.execute(
				"SELECT response_template, enabled FROM simple_command_definitions WHERE command_name = 'wave'"
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(simple_row[0], "Hello {author_username} from {platform}")
		self.assertEqual(simple_row[1], 1)

		reply_lines: list[str] = []
		ingest_and_analyze(TwitchConnector(self.database_path), 
			{
				"message_id": "twitch-credit-live-1",
				"timestamp": "2026-08-06T05:40:00Z",
				"channel": "its_not_qwerty",
				"content": "!credit",
				"user_id": "twitch-user-live-1",
				"username": "sam",
			},
			reply_sink=reply_lines.append,
		)

		self.assertEqual(len(reply_lines), 1)
		self.assertIn("Credit Ledger", reply_lines[0])
		self.assertIn("Twitch profile for sam", reply_lines[0])

		simple_reply_lines: list[str] = []
		ingest_and_analyze(TwitchConnector(self.database_path), 
			{
				"message_id": "twitch-wave-live-1",
				"timestamp": "2026-08-06T05:41:00Z",
				"channel": "its_not_qwerty",
				"content": "!wave",
				"user_id": "twitch-user-live-1",
				"username": "sam",
			},
			reply_sink=simple_reply_lines.append,
		)

		self.assertEqual(len(simple_reply_lines), 1)
		self.assertEqual(simple_reply_lines[0], "Hello sam from twitch")

		request = Request(
			f"{self.base_url}/commands",
			data=b"record_type=simple&action=delete&command_name=wave",
			headers={
				"Cookie": cookie_value,
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		with self.assertRaises(HTTPError) as simple_delete_error:
			self.opener.open(request)
		simple_delete_error.exception.close()
		self.assertEqual(simple_delete_error.exception.code, 302)
		self.assertIn("/commands?status=Deleted%20simple%20command%20wave", simple_delete_error.exception.headers["Location"])

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			simple_row = connection.execute(
				"SELECT response_template, enabled FROM simple_command_definitions WHERE command_name = 'wave'"
			).fetchone()
		finally:
			connection.close()

		self.assertIsNone(simple_row)

	def test_users_link_by_username_links_all_matching_accounts(self) -> None:
		discord_connector = DiscordConnector(self.database_path)
		twitch_connector = TwitchConnector(self.database_path)

		ingest_and_analyze(discord_connector,
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
		ingest_and_analyze(twitch_connector,
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
