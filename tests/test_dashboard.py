from __future__ import annotations

import json
import hashlib
import hmac
import os
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import parse_qs, urlencode, urlparse
from unittest import mock

from cryptography.fernet import Fernet

from src.config import AppSettings
from src.contexts import ActorAttribution, TenantContext
from src.db import (
	connect_database,
	initialize_database,
	record_moderation_action,
	upsert_moderation_rule,
	upsert_service_reliability_bucket,
)
from src.discord import DiscordConnector
from src.health import create_health_server
from src.dashboard.auth import DiscordIdentity
from src.dashboard.moderation import execute_bulk_moderation, list_moderation_work
from src.intelligence.community import (
	create_community,
	create_member_appeal,
	create_member_report,
	create_organization,
	create_workspace,
	grant_operator_role,
	issue_pilot_invitation,
	register_installation,
	set_operator_permission_override,
)
from src.intelligence.announcements import create_announcement
from src.intelligence.onboarding import save_onboarding_resource
from src.intelligence.quotas import configure_tenant_quota
from src.intelligence.signals import SIGNAL_ANALYZER_VERSION
from src.twitch import TwitchConnector
from src.twitch_auth import TwitchOAuthGrant, TwitchTokenValidation
from tests.pipeline_support import ingest_and_analyze


class NoRedirectHandler(HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
		return None


class DashboardTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tempdir = TemporaryDirectory()
		self.database_path: Path | str = os.environ.get(
			"QBOT_TEST_DATABASE_URL", Path(self.tempdir.name) / "dashboard.sqlite3"
		)
		if isinstance(self.database_path, str):
			connection = connect_database(self.database_path)
			try:
				connection.execute("DROP SCHEMA public CASCADE")
				connection.execute("CREATE SCHEMA public")
				connection.commit()
			finally:
				connection.close()
		database_environment = (
			{"QBOT_DATABASE_URL": self.database_path}
			if isinstance(self.database_path, str)
			else {"QBOT_DATABASE_PATH": str(self.database_path)}
		)
		self.settings = AppSettings.from_env(
			{
				**database_environment,
				"QBOT_ENABLED_SERVICES": "web,jobs",
				"QBOT_DASHBOARD_SESSION_SECRET": "session-secret",
				"QBOT_DISCORD_OAUTH_CLIENT_ID": "oauth-client-id",
				"QBOT_DISCORD_OAUTH_CLIENT_SECRET": "oauth-client-secret",
				"QBOT_DISCORD_OAUTH_REDIRECT_URI": "https://dashboard.example.test/oauth/discord/callback",
				"QBOT_OPERATOR_GUILD_IDS": "guild-1,guild-2",
				"QBOT_TWITCH_CLIENT_ID": "twitch-client-id",
				"QBOT_TWITCH_CLIENT_SECRET": "twitch-client-secret",
				"QBOT_TWITCH_EVENTSUB_SECRET": "test-eventsub-secret",
				"QBOT_TWITCH_EVENTSUB_CALLBACK_URL": "https://dashboard.example.test/webhooks/twitch/eventsub",
				"QBOT_CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
				"QBOT_LEGAL_ORGANIZATION_NAME": "QBot4K Test Operations",
				"QBOT_LEGAL_CONTACT_EMAIL": "privacy@example.test",
				"QBOT_LEGAL_JURISDICTION": "Test Jurisdiction",
				"QBOT_LEGAL_EFFECTIVE_DATE": "2026-09-02",
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

	def _issue_operator_session_cookie(self, existing_session: str | None = None) -> str:
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
					headers={
						"Cookie": "qbot4k_oauth_state=state-1"
						+ (f"; qbot4k_session={existing_session}" if existing_session else "")
					},
				)
				with self.assertRaises(HTTPError) as callback_error:
					self.opener.open(request)
				callback_error.exception.close()

		cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
		session_cookie = next(cookie for cookie in cookies if cookie.startswith("qbot4k_session="))
		return session_cookie.split(";", 1)[0]

	def test_dashboard_rejects_csrf_xss_injection_and_secret_exposure(self) -> None:
		cookie = self._issue_operator_session_cookie("attacker-fixed")
		self.assertNotEqual(cookie, "qbot4k_session=attacker-fixed")
		connection = connect_database(self.database_path)
		try:
			with connection:
				xss_user_id = int(connection.execute(
					"INSERT INTO users(primary_display_name) VALUES (?)",
					("<script>alert('stored')</script>",),
				).lastrowid)
				canary_user_id = int(connection.execute(
					"INSERT INTO users(primary_display_name) VALUES ('Injection Canary')"
				).lastrowid)
				connection.executemany(
					"""INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
					   VALUES ('discord',?,?,?)""",
					(
						("xss-user", "xss-user", xss_user_id),
						("canary-user", "canary-user", canary_user_id),
					),
				)
				connection.executemany(
					"""INSERT INTO messages(
					       community_id,platform,platform_message_id,platform_account_id,
					       channel_id,content_raw,content_normalized,sent_at,user_id
					   ) VALUES (
					       1,'discord',?,(SELECT id FROM platform_accounts WHERE platform_user_id=?),
					       'security','security','security','2026-09-02T00:00:00+00:00',?
					   )""",
					(
						("xss-message", "xss-user", xss_user_id),
						("canary-message", "canary-user", canary_user_id),
					),
				)
		finally:
			connection.close()

		csrf_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": "1"}).encode("utf-8"),
			headers={
				"Cookie": cookie,
				"Content-Type": "application/x-www-form-urlencoded",
				"Origin": "https://attacker.example",
			},
		)
		with self.assertRaises(HTTPError) as csrf_response:
			self.opener.open(csrf_request)
		self.assertEqual(csrf_response.exception.code, 403)
		self.assertEqual(
			json.loads(csrf_response.exception.read().decode("utf-8"))["error"],
			"origin_mismatch",
		)
		csrf_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/users", headers={"Cookie": cookie}
		)) as response:
			users_html = response.read().decode("utf-8")
		self.assertNotIn("<script>alert('stored')</script>", users_html)
		self.assertIn("&lt;script&gt;alert('stored')&lt;/script&gt;", users_html)

		injection = urlencode({"q": "' OR 1=1--"})
		with self.opener.open(Request(
			f"{self.base_url}/users?{injection}", headers={"Cookie": cookie}
		)) as response:
			injection_html = response.read().decode("utf-8")
		self.assertNotIn("Injection Canary", injection_html)

		with self.opener.open(Request(
			f"{self.base_url}/api/health", headers={"Cookie": cookie}
		)) as response:
			health_body = response.read().decode("utf-8")
		for secret in (
			"session-secret", "oauth-client-secret", "twitch-client-secret",
			"test-eventsub-secret", self.settings.credential_encryption_key,
		):
			self.assertNotIn(str(secret), health_body)

	def test_twitch_eventsub_replay_is_idempotent(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			register_installation(
				connection, community_id=1, platform="twitch",
				external_community_id="eventsub-broadcaster", display_name="EventSub",
			)
		finally:
			connection.close()

		message_id = "eventsub-replay-1"
		timestamp = datetime.now(timezone.utc).isoformat()
		body = json.dumps({
			"subscription": {
				"id": "subscription-1", "type": "channel.follow", "status": "enabled",
				"condition": {"broadcaster_user_id": "eventsub-broadcaster"},
			},
			"event": {
				"broadcaster_user_id": "eventsub-broadcaster",
				"user_id": "follower-1", "user_name": "Follower",
			},
		}, separators=(",", ":")).encode("utf-8")
		signature = "sha256=" + hmac.new(
			b"test-eventsub-secret", message_id.encode("utf-8") + timestamp.encode("utf-8") + body,
			hashlib.sha256,
		).hexdigest()
		headers = {
			"Content-Type": "application/json",
			"Twitch-Eventsub-Message-Id": message_id,
			"Twitch-Eventsub-Message-Timestamp": timestamp,
			"Twitch-Eventsub-Message-Signature": signature,
			"Twitch-Eventsub-Message-Type": "notification",
		}

		statuses = []
		for _ in range(2):
			with self.opener.open(Request(
				f"{self.base_url}/webhooks/twitch/eventsub", data=body, headers=headers,
			)) as response:
				statuses.append(json.loads(response.read().decode("utf-8"))["status"])
		self.assertEqual(statuses, ["persisted", "duplicate"])

		connection = connect_database(self.database_path)
		try:
			count = connection.execute(
				"""SELECT COUNT(*) FROM observations
				   WHERE community_id=1 AND platform='twitch' AND external_event_id=?""",
				(message_id,),
			).fetchone()[0]
		finally:
			connection.close()
		self.assertEqual(count, 1)

	def _seed_sortable_intelligence(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				user_ids = {}
				for name in ("Alice", "Bob", "Carol"):
					cursor = connection.execute("INSERT INTO users(primary_display_name) VALUES (?)", (name,))
					user_ids[name] = int(cursor.lastrowid)
				alerts = (
					("Alice", "high", "Alpha finding", 0.90, "open", "2026-08-11T01:00:00+00:00"),
					("Bob", "low", "Zulu finding", 0.20, "resolved", "2026-08-11T02:00:00+00:00"),
					("Carol", "critical", "Middle finding", 0.60, "in_case", "2026-08-11T03:00:00+00:00"),
				)
				alert_ids = []
				for index, (name, severity, title, confidence, status, created_at) in enumerate(alerts):
					cursor = connection.execute(
						"""INSERT INTO intelligence_alerts(community_id, user_id, alert_type, severity, title, summary,
						   confidence, status, created_at, updated_at, dedupe_key)
						   VALUES (1, ?, 'test', ?, ?, 'summary', ?, ?, ?, ?, ?)""",
						(user_ids[name], severity, title, confidence, status, created_at, created_at, f"sort-alert-{index}"),
					)
					alert_ids.append(int(cursor.lastrowid))
				cases = (
					("Amber case", "medium", "open", "2026-08-11T01:00:00+00:00", 3, 2),
					("Blue case", "critical", "active", "2026-08-11T03:00:00+00:00", 1, 3),
					("Crimson case", "low", "closed", "2026-08-11T02:00:00+00:00", 2, 1),
				)
				for title, priority, status, updated_at, entity_count, evidence_count in cases:
					cursor = connection.execute(
						"""INSERT INTO investigation_cases(community_id, title, priority, status, updated_at)
						   VALUES (1, ?, ?, ?, ?)""", (title, priority, status, updated_at),
					)
					case_id = int(cursor.lastrowid)
					for user_id in list(user_ids.values())[:entity_count]:
						connection.execute("INSERT INTO case_entities(case_id, user_id) VALUES (?, ?)", (case_id, user_id))
					for alert_id in alert_ids[:evidence_count]:
						connection.execute("INSERT INTO case_evidence(case_id, alert_id) VALUES (?, ?)", (case_id, alert_id))
				relationships = (
					("Alice", "Carol", "shared_domain", 2.0, 5, "2026-08-11T01:00:00+00:00", "one"),
					("Bob", "Alice", "mention", 9.0, 2, "2026-08-11T03:00:00+00:00", "two"),
					("Carol", "Bob", "channel_coactivity", 5.0, 8, "2026-08-11T02:00:00+00:00", "three"),
				)
				for source, target, relationship_type, strength, evidence_count, observed_at, context in relationships:
					connection.execute(
						"""INSERT INTO entity_relationships(community_id, source_user_id, target_user_id, relationship_type,
						   context_key, strength, evidence_count, first_observed_at, last_observed_at)
						   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
						(user_ids[source], user_ids[target], relationship_type, context, strength, evidence_count, observed_at, observed_at),
					)
		finally:
			connection.close()

	def test_intelligence_tables_support_independent_sorting(self) -> None:
		self._seed_sortable_intelligence()
		cookie = self._issue_operator_session_cookie()

		def payload(query: str) -> dict[str, object]:
			with self.opener.open(Request(f"{self.base_url}/api/intelligence?{query}", headers={"Cookie": cookie})) as response:
				return json.loads(response.read().decode("utf-8"))

		alert_expectations = {
			"severity": ["critical", "high", "low"],
			"subject": ["Alice", "Bob", "Carol"],
			"finding": ["Alpha finding", "Middle finding", "Zulu finding"],
			"confidence": [0.2, 0.6, 0.9],
			"status": ["open", "in_case", "resolved"],
		}
		for key, expected in alert_expectations.items():
			direction = "desc" if key == "severity" else "asc"
			result = payload(f"alert_sort={key}&alert_dir={direction}")
			field = {"subject": "primary_display_name", "finding": "title"}.get(key, key)
			self.assertEqual([item[field] for item in result["alerts"]], expected)
			self.assertEqual(result["sort"]["alerts"], {"by": key, "dir": direction})
			reverse_dir = "desc" if direction == "asc" else "asc"
			reversed_result = payload(f"alert_sort={key}&alert_dir={reverse_dir}")
			self.assertEqual([item[field] for item in reversed_result["alerts"]], list(reversed(expected)))

		case_expectations = {
			"case": ["Amber case", "Blue case", "Crimson case"],
			"priority": ["critical", "medium", "low"],
			"status": ["open", "active", "closed"],
			"entities": [3, 2, 1],
			"evidence": [3, 2, 1],
			"updated": ["2026-08-11T03:00:00+00:00", "2026-08-11T02:00:00+00:00", "2026-08-11T01:00:00+00:00"],
		}
		for key, expected in case_expectations.items():
			direction = "asc" if key in {"case", "status"} else "desc"
			result = payload(f"case_sort={key}&case_dir={direction}")
			field = {"case": "title", "entities": "entity_count", "evidence": "evidence_count", "updated": "updated_at"}.get(key, key)
			self.assertEqual([item[field] for item in result["cases"]], expected)
			reverse_dir = "desc" if direction == "asc" else "asc"
			reversed_result = payload(f"case_sort={key}&case_dir={reverse_dir}")
			self.assertEqual([item[field] for item in reversed_result["cases"]], list(reversed(expected)))

		relationship_expectations = {
			"source": ["Alice", "Bob", "Carol"],
			"relationship": ["channel_coactivity", "mention", "shared_domain"],
			"target": ["Alice", "Bob", "Carol"],
			"strength": [9.0, 5.0, 2.0],
			"evidence": [8, 5, 2],
			"last_observed": ["2026-08-11T03:00:00+00:00", "2026-08-11T02:00:00+00:00", "2026-08-11T01:00:00+00:00"],
		}
		for key, expected in relationship_expectations.items():
			direction = "asc" if key in {"source", "relationship", "target"} else "desc"
			result = payload(f"relationship_sort={key}&relationship_dir={direction}")
			field = {"source": "source_name", "relationship": "relationship_type", "target": "target_name", "evidence": "evidence_count", "last_observed": "last_observed_at"}.get(key, key)
			self.assertEqual([item[field] for item in result["relationships"]], expected)
			reverse_dir = "desc" if direction == "asc" else "asc"
			reversed_result = payload(f"relationship_sort={key}&relationship_dir={reverse_dir}")
			self.assertEqual([item[field] for item in reversed_result["relationships"]], list(reversed(expected)))

		page_query = "alert_sort=confidence&alert_dir=asc&case_sort=priority&case_dir=desc&relationship_sort=source&relationship_dir=asc"
		with self.opener.open(Request(f"{self.base_url}/intelligence?{page_query}", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Confidence ↑", body)
		self.assertIn("Priority ↓", body)
		self.assertIn("Source ↑", body)
		self.assertIn("alert_sort=confidence", body)
		self.assertIn("case_sort=priority", body)
		self.assertIn("relationship_sort=source", body)

	def test_intelligence_page_defaults_to_untriaged_alerts(self) -> None:
		self._seed_sortable_intelligence()
		cookie = self._issue_operator_session_cookie()
		with self.opener.open(Request(
			f"{self.base_url}/intelligence", headers={"Cookie": cookie}
		)) as response:
			default_body = response.read().decode("utf-8")
		self.assertIn("Alpha finding", default_body)
		self.assertNotIn("Zulu finding", default_body)
		self.assertNotIn("Middle finding", default_body)
		self.assertIn("Untriaged alerts", default_body)

		with self.opener.open(Request(
			f"{self.base_url}/intelligence?alert_status=all", headers={"Cookie": cookie}
		)) as response:
			all_body = response.read().decode("utf-8")
		self.assertIn("Alpha finding", all_body)
		self.assertIn("Zulu finding", all_body)
		self.assertIn("Middle finding", all_body)

	def test_analytics_tables_expose_independent_sort_headers_and_api_state(self) -> None:
		cookie = self._issue_operator_session_cookie()
		query = (
			"topics_sort=label&topics_dir=asc&graph_sort=influence_score&graph_dir=desc&"
			"identity_suggestions_sort=status&identity_suggestions_dir=asc&"
			"cohort_anomalies_sort=confidence&cohort_anomalies_dir=desc&"
			"evaluation_sort=model_key&evaluation_dir=asc"
		)
		with self.opener.open(Request(f"{self.base_url}/analytics?{query}", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Label ↑", body)
		self.assertIn("Influence Score ↓", body)
		self.assertIn("Status ↑", body)
		self.assertIn("Confidence ↓", body)
		self.assertIn("Model Key ↑", body)
		self.assertIn("/analytics/export.json", body)
		for parameter in (
			"topics_sort=label", "graph_sort=influence_score", "identity_suggestions_sort=status",
			"cohort_anomalies_sort=confidence", "evaluation_sort=model_key",
		):
			self.assertIn(parameter, body)

		with self.opener.open(Request(f"{self.base_url}/api/analytics?{query}", headers={"Cookie": cookie})) as response:
			payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(payload["sort"]["topics"], {"by": "label", "dir": "asc"})
		self.assertEqual(payload["sort"]["graph"], {"by": "influence_score", "dir": "desc"})
		self.assertEqual(payload["sort"]["identity_suggestions"], {"by": "status", "dir": "asc"})
		self.assertEqual(payload["sort"]["cohort_anomalies"], {"by": "confidence", "dir": "desc"})
		self.assertEqual(payload["sort"]["evaluation"], {"by": "model_key", "dir": "asc"})

	def test_analytics_export_requires_permission_and_suppresses_small_cohorts(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			with connection:
				backup_owner_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('analytics-owner','owner','admin')"
				).lastrowid)
			grant_operator_role(
				connection, operator_id=backup_owner_id, community_id=1, role="owner"
			)
			grant_operator_role(connection, operator_id=1, community_id=1, role="viewer")
			with connection:
				cohort_user_id = int(connection.execute(
					"INSERT INTO users(primary_display_name) VALUES ('Cohort member')"
				).lastrowid)
				for cohort_key, sample_size, confidence in (
					("private", 2, 0.9), ("low-confidence", 6, 0.5), ("publishable", 6, 0.9)
				):
					connection.execute(
						"""INSERT INTO community_cohort_baselines(
						       community_id,cohort_type,cohort_key,signal_key,sample_size,
						       mean_value,stddev_value,median_value,p90_value,calculated_at
						   ) VALUES (1,'platform',?,'activity',?,10,2,10,12,?)""",
						(cohort_key, sample_size, "2026-08-26T12:00:00+00:00"),
					)
					connection.execute(
						"""INSERT INTO community_cohort_anomalies(
						       community_id,user_id,cohort_type,cohort_key,signal_key,
						       observed_value,baseline_mean,z_score,direction,confidence,calculated_at
						   ) VALUES (1,?,'platform',?,'activity',20,10,5,'above',?,?)""",
						(cohort_user_id, cohort_key, confidence, "2026-08-26T12:00:00+00:00"),
					)
		finally:
			connection.close()
		with self.assertRaises(HTTPError) as denied_response:
			self.opener.open(Request(
				f"{self.base_url}/analytics/export.json", headers={"Cookie": cookie}
			))
		self.assertEqual(denied_response.exception.code, 403)
		denied_response.exception.close()
		with self.assertRaises(HTTPError) as search_denied_response:
			self.opener.open(Request(
				f"{self.base_url}/search/export.csv", headers={"Cookie": cookie}
			))
		self.assertEqual(search_denied_response.exception.code, 403)
		search_denied_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			grant_operator_role(connection, operator_id=1, community_id=1, role="analyst")
			set_operator_permission_override(
				connection, operator_id=1, community_id=1, permission="analytics.export",
				decision="grant", actor_operator_id=backup_owner_id,
			)
		finally:
			connection.close()
		cookie = self._issue_operator_session_cookie()
		with self.opener.open(Request(
			f"{self.base_url}/analytics/export.json", headers={"Cookie": cookie}
		)) as response:
			payload = json.loads(response.read().decode("utf-8"))
			self.assertIn("qbot4k-community-1-analytics.json", response.headers["Content-Disposition"])
		self.assertEqual(
			[item["cohort_key"] for item in payload["cohort_anomalies"]], ["publishable"]
		)

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

	def test_public_home_and_policy_publication_are_end_to_end(self) -> None:
		with self.opener.open(Request(f"{self.base_url}/")) as response:
			home = response.read().decode("utf-8")
		self.assertIn("QBot4K", home)
		self.assertIn("Link Discord", home)
		self.assertIn("Twitch", home)

		cookie = self._issue_operator_session_cookie()
		publish = Request(
			f"{self.base_url}/api/moderation/rules",
			data=json.dumps({
				"name": "E2E shadow policy",
				"rule_type": "exact_term",
				"pattern": "e2e-policy-term",
				"severity": "high",
				"enabled": True,
				"enforcement_mode": "shadow",
				"action_duration_seconds": 600,
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.opener.open(publish) as response:
			published = json.loads(response.read().decode("utf-8"))
		self.assertEqual(published["status"], "saved")

		with self.opener.open(Request(
			f"{self.base_url}/api/moderation/rules", headers={"Cookie": cookie}
		)) as response:
			rules = json.loads(response.read().decode("utf-8"))["items"]
		policy = next(item for item in rules if item["name"] == "E2E shadow policy")
		self.assertEqual(policy["enforcement_mode"], "shadow")

		with self.opener.open(Request(
			f"{self.base_url}/api/audit", headers={"Cookie": cookie}
		)) as response:
			audit = json.loads(response.read().decode("utf-8"))["items"]
		self.assertTrue(any(
			item["action_type"] == "moderation.rule_saved"
			and item["actor_id"] == 1
			for item in audit
		))

	def test_rule_lifecycle_dashboard_supports_preview_publish_exemption_and_rollback(self) -> None:
		cookie = self._issue_operator_session_cookie()

		def post(path: str, values: dict[str, object]) -> HTTPError:
			request = Request(
				f"{self.base_url}{path}", data=urlencode(values, doseq=True).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
				method="POST",
			)
			with self.assertRaises(HTTPError) as response:
				self.opener.open(request)
			self.assertEqual(response.exception.code, 302)
			return response.exception

		with self.opener.open(Request(
			f"{self.base_url}/moderation", headers={"Cookie": cookie},
		)) as response:
			initial_body = response.read().decode("utf-8")
		self.assertIn("Create draft", initial_body)
		self.assertIn("Rule versions", initial_body)

		draft = post("/moderation/rules/drafts", {
			"name": "Lifecycle phrase", "rule_type": "exact_term",
			"pattern": "blocked", "severity": "high",
			"auto_enforce_action": "timeout", "action_duration_seconds": "900",
			"platform_scope": ["discord"],
		})
		self.assertIn("Rule+draft+created", draft.headers["Location"])
		draft.close()
		connection = connect_database(self.database_path)
		try:
			version_id, rule_id = connection.execute(
				"""SELECT id,moderation_rule_id FROM moderation_rule_versions
				   WHERE community_id=1 ORDER BY id DESC LIMIT 1"""
			).fetchone()
		finally:
			connection.close()

		preview = post(f"/moderation/rule-versions/{version_id}/preview", {
			"samples": "allowed\nblocked content",
		})
		self.assertIn("1%20of%202%20matched", preview.headers["Location"])
		preview.close()
		published = post(f"/moderation/rule-versions/{version_id}/publish", {
			"lifecycle_state": "shadow",
		})
		self.assertIn("Rule+published", published.headers["Location"])
		published.close()
		exemption = post(f"/moderation/rules/{rule_id}/exemptions", {
			"exemption_type": "channel", "exemption_value": "trusted",
			"reason": "Staff channel",
		})
		self.assertIn("Rule+exemption+added", exemption.headers["Location"])
		exemption.close()
		rollback = post(f"/moderation/rule-versions/{version_id}/rollback", {"confirm": "1"})
		self.assertIn("Rule+rolled+back", rollback.headers["Location"])
		rollback.close()

		with self.opener.open(Request(
			f"{self.base_url}/moderation", headers={"Cookie": cookie},
		)) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Lifecycle phrase", body)
		self.assertIn("matched_indexes", body)
		self.assertIn("Rollback to version", body)
		connection = connect_database(self.database_path)
		try:
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM moderation_rule_versions WHERE moderation_rule_id=?",
				(rule_id,),
			).fetchone()[0], 2)
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM moderation_rule_exemptions WHERE moderation_rule_id=?",
				(rule_id,),
			).fetchone()[0], 1)
		finally:
			connection.close()

	def test_permission_change_invalidates_existing_session_cookie(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			set_operator_permission_override(
				connection, operator_id=1, community_id=1, permission="members.read",
				decision="deny", actor_operator_id=1,
			)
		finally:
			connection.close()
		with self.assertRaises(HTTPError) as stale_response:
			self.opener.open(Request(f"{self.base_url}/dashboard", headers={"Cookie": cookie}))
		self.assertEqual(stale_response.exception.code, 302)
		self.assertEqual(stale_response.exception.headers["Location"], "/login")
		stale_response.exception.close()

	def test_operator_invitation_api_revocation_and_oauth_acceptance(self) -> None:
		owner_cookie = self._issue_operator_session_cookie()
		invite_request = Request(
			f"{self.base_url}/api/operators/invitations",
			data=json.dumps({
				"discord_user_id": "456", "role": "viewer", "expires_hours": 24,
			}).encode("utf-8"),
			headers={"Cookie": owner_cookie, "Content-Type": "application/json"}, method="POST",
		)
		with self.opener.open(invite_request) as response:
			invitation_id = int(json.loads(response.read().decode("utf-8"))["invitation_id"])
		self.assertGreater(invitation_id, 0)
		revoke_request = Request(
			f"{self.base_url}/api/operators/{invitation_id}/revoke-invitation",
			data=b"{}", headers={"Cookie": owner_cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.opener.open(revoke_request) as response:
			self.assertEqual(json.loads(response.read().decode("utf-8"))["status"], "completed")
		second_invite = Request(
			f"{self.base_url}/api/operators/invitations",
			data=json.dumps({
				"discord_user_id": "456", "role": "viewer", "expires_hours": 24,
			}).encode("utf-8"),
			headers={"Cookie": owner_cookie, "Content-Type": "application/json"}, method="POST",
		)
		with self.opener.open(second_invite):
			pass
		with mock.patch("src.dashboard.server.exchange_discord_code_for_token", return_value="invite-token"):
			with mock.patch(
				"src.dashboard.server.fetch_discord_identity",
				return_value=DiscordIdentity(
					user_id="456", username="invitee", guild_ids=("guild-1",),
					permissions={"guild-1": "8"},
				),
			):
				with self.assertRaises(HTTPError) as callback:
					self.opener.open(Request(
						f"{self.base_url}/oauth/discord/callback?code=invite&state=invite-state",
						headers={"Cookie": "qbot4k_oauth_state=invite-state"},
					))
		self.assertEqual(callback.exception.code, 302)
		callback.exception.close()
		connection = connect_database(self.database_path)
		try:
			row = connection.execute(
				"""SELECT r.role,i.status FROM operator_accounts o
				   JOIN operator_community_roles r ON r.operator_id=o.id AND r.community_id=1
				   JOIN operator_invitations i ON i.accepted_by_operator_id=o.id
				   WHERE o.discord_user_id='456' ORDER BY i.id DESC LIMIT 1"""
			).fetchone()
			self.assertEqual(tuple(row), ("viewer", "accepted"))
		finally:
			connection.close()

	def test_announcement_mutations_are_bound_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(connection, name="Announcements", slug="announcements")
			workspace_id = create_workspace(
				connection, organization_id=organization_id, name="Workspace", slug="workspace"
			)
			other_community_id = create_community(
				connection, workspace_id=workspace_id, name="Other", slug="other"
			)
			other_announcement_id = create_announcement(
				connection, community_id=other_community_id, platform="discord",
				target_external_id="other-channel", body="Other tenant draft",
				created_by_operator_id=1,
			)
			target_installation_id = register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="active-guild", display_name="Active Guild",
			)
			with connection:
				connection.execute(
					"UPDATE communities SET timezone='America/Los_Angeles' WHERE id=1"
				)
		finally:
			connection.close()

		create_request = Request(
			f"{self.base_url}/announcements",
			data=urlencode({
				"community_id": str(other_community_id),
				"platform": "discord",
				"target_installation_id": str(target_installation_id),
				"target_external_id": "active-channel",
				"body": "Active tenant draft",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as create_response:
			self.opener.open(create_request)
		self.assertEqual(create_response.exception.code, 302)
		create_response.exception.close()

		approve_request = Request(
			f"{self.base_url}/announcements/{other_announcement_id}/approve",
			data=urlencode({"scheduled_at": "2026-08-26T18:00:00+00:00"}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as approve_response:
			self.opener.open(approve_request)
		self.assertEqual(approve_response.exception.code, 404)
		approve_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			rows = connection.execute(
				"""SELECT id,community_id,target_installation_id,body,status
				   FROM community_announcements ORDER BY id"""
			).fetchall()
		finally:
			connection.close()
		self.assertEqual(
			[
				(int(row["community_id"]), row["target_installation_id"], row["body"], row["status"])
				for row in rows
			],
			[
				(other_community_id, None, "Other tenant draft", "draft"),
				(1, target_installation_id, "Active tenant draft", "draft"),
			],
		)
		active_announcement_id = int(rows[1]["id"])
		local_approve_request = Request(
			f"{self.base_url}/announcements/{active_announcement_id}/approve",
			data=urlencode({"scheduled_at": "2026-08-26T18:00"}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as local_approve_response:
			self.opener.open(local_approve_request)
		self.assertEqual(local_approve_response.exception.code, 302)
		local_approve_response.exception.close()
		with self.opener.open(Request(
			f"{self.base_url}/announcements", headers={"Cookie": cookie}
		)) as response:
			announcements_body = response.read().decode("utf-8")
		self.assertIn("America/Los_Angeles", announcements_body)
		self.assertIn("2026-08-26T18:00:00-07:00", announcements_body)

	def test_onboarding_welcome_configuration_is_tenant_bound_and_disableable(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			own_installation_id = register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="welcome-guild", display_name="Welcome Guild",
			)
			other_community_id = create_community(
				connection, workspace_id=1, name="Other Welcome", slug="other-welcome"
			)
			foreign_installation_id = register_installation(
				connection, community_id=other_community_id, platform="discord",
				external_community_id="foreign-welcome-guild", display_name="Foreign Guild",
			)
		finally:
			connection.close()

		def post_settings(installation_id: int, enabled: bool):
			return Request(
				f"{self.base_url}/onboarding",
				data=urlencode({
					"discord_installation_id": str(installation_id),
					"welcome_channel_id": "welcome-channel",
					"welcome_template": "Hello {mention}, welcome to {username}'s next chapter!",
					"newcomer_role_id": "newcomer-role",
					"newcomer_role_enabled": "1",
					"checkpoint_due_hours": "12",
					"checkpoint_reminder_enabled": "1",
					"checkpoint_reminder_template": "Reminder {mention}, verify now",
					"verification_resource_url": "https://example.test/community-guide",
					"verification_resource_template": "Verified {mention}. Read {resource_url}",
					"verification_evidence_required": "1",
					**({"verification_resource_enabled": "1"} if enabled else {}),
					**({"enabled": "1"} if enabled else {}),
				}).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
				method="POST",
			)

		with self.assertRaises(HTTPError) as foreign_response:
			self.opener.open(post_settings(foreign_installation_id, True))
		self.assertEqual(foreign_response.exception.code, 400)
		foreign_response.exception.close()
		with self.assertRaises(HTTPError) as enabled_response:
			self.opener.open(post_settings(own_installation_id, True))
		self.assertEqual(enabled_response.exception.code, 302)
		enabled_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			foreign_resource_id = save_onboarding_resource(
				connection, community_id=other_community_id, operator_id=1,
				title="Foreign guide", resource_url="https://example.test/foreign",
				message_template="{mention}: {resource_url}",
			)
			with connection:
				connection.execute(
					"""INSERT INTO community_onboarding_members(
					       community_id,discord_installation_id,platform_user_id,username,
					       newcomer_role_id,role_assignment_status,joined_at
					   ) VALUES (1,?,'new-member-1','New Member','newcomer-role','assigned',?)""",
					(own_installation_id, "2026-08-26T12:00:00+00:00"),
				)
		finally:
			connection.close()
		create_resource_request = Request(
			f"{self.base_url}/onboarding/resources",
			data=urlencode({
				"title": "Member handbook", "resource_url": "https://example.test/handbook",
				"message_template": "{mention} open {title}: {resource_url}",
				"sort_order": "5", "enabled": "1",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as create_resource_response:
			self.opener.open(create_resource_request)
		self.assertEqual(create_resource_response.exception.code, 302)
		create_resource_response.exception.close()
		foreign_update_request = Request(
			f"{self.base_url}/onboarding/resources",
			data=urlencode({
				"resource_id": str(foreign_resource_id), "title": "Changed",
				"resource_url": "https://example.test/changed",
				"message_template": "{mention}: {resource_url}", "sort_order": "0",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as foreign_update_response:
			self.opener.open(foreign_update_request)
		self.assertEqual(foreign_update_response.exception.code, 404)
		foreign_update_response.exception.close()
		with self.opener.open(Request(
			f"{self.base_url}/onboarding", headers={"Cookie": cookie}
		)) as page_response:
			page = page_response.read().decode("utf-8")
		self.assertIn("Hello @new-member, welcome to new-member's next chapter!", page)
		self.assertIn("Verified @new-member. Read https://example.test/community-guide", page)
		self.assertIn("Verification evidence", page)
		self.assertNotIn("Foreign Guild", page)
		self.assertIn("New Member", page)
		self.assertIn("Member handbook", page)
		self.assertNotIn("Foreign guide", page)
		connection = connect_database(self.database_path)
		try:
			own_resource_id = int(connection.execute(
				"SELECT id FROM community_onboarding_resources WHERE community_id=1"
			).fetchone()[0])
		finally:
			connection.close()
		update_resource_request = Request(
			f"{self.base_url}/onboarding/resources",
			data=urlencode({
				"resource_id": str(own_resource_id), "title": "Updated handbook",
				"resource_url": "https://example.test/updated-handbook",
				"message_template": "{mention} open {title}: {resource_url}",
				"sort_order": "10",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as update_resource_response:
			self.opener.open(update_resource_request)
		self.assertEqual(update_resource_response.exception.code, 302)
		update_resource_response.exception.close()
		delete_resource_request = Request(
			f"{self.base_url}/onboarding/resources/{own_resource_id}/delete",
			data=b"", headers={"Cookie": cookie}, method="POST",
		)
		with self.assertRaises(HTTPError) as delete_resource_response:
			self.opener.open(delete_resource_request)
		self.assertEqual(delete_resource_response.exception.code, 302)
		delete_resource_response.exception.close()
		missing_evidence_request = Request(
			f"{self.base_url}/onboarding/verify",
			data=urlencode({"platform_user_id": "new-member-1"}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as missing_evidence_response:
			self.opener.open(missing_evidence_request)
		self.assertEqual(missing_evidence_response.exception.code, 400)
		missing_evidence_response.exception.close()
		verify_request = Request(
			f"{self.base_url}/onboarding/verify",
			data=urlencode({
				"platform_user_id": "new-member-1",
				"verification_evidence": "Reviewed membership request",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as verify_response:
			self.opener.open(verify_request)
		self.assertEqual(verify_response.exception.code, 302)
		verify_response.exception.close()
		with self.assertRaises(HTTPError) as disabled_response:
			self.opener.open(post_settings(own_installation_id, False))
		self.assertEqual(disabled_response.exception.code, 302)
		disabled_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			row = connection.execute(
				"""SELECT discord_installation_id,welcome_enabled,newcomer_role_id,newcomer_role_enabled,
				          checkpoint_due_hours,checkpoint_reminder_enabled,
				          verification_resource_enabled,verification_resource_url,
				          verification_evidence_required
				   FROM community_onboarding_settings
				   WHERE community_id=1"""
			).fetchone()
			member = connection.execute(
				"""SELECT status,verification_evidence FROM community_onboarding_members
				   WHERE community_id=1 AND platform_user_id='new-member-1'"""
			).fetchone()
			audit_count = int(connection.execute(
				"""SELECT COUNT(*) FROM audit_log
				   WHERE action_type='onboarding.welcome_configured' AND entity_id=1"""
			).fetchone()[0])
			resource_count = int(connection.execute(
				"SELECT COUNT(*) FROM community_onboarding_resources WHERE community_id=1"
			).fetchone()[0])
		finally:
			connection.close()
		self.assertEqual(int(row["discord_installation_id"]), own_installation_id)
		self.assertEqual(int(row["welcome_enabled"]), 0)
		self.assertEqual(row["newcomer_role_id"], "newcomer-role")
		self.assertEqual(int(row["newcomer_role_enabled"]), 1)
		self.assertEqual(int(row["checkpoint_due_hours"]), 12)
		self.assertEqual(int(row["checkpoint_reminder_enabled"]), 1)
		self.assertEqual(int(row["verification_resource_enabled"]), 0)
		self.assertEqual(row["verification_resource_url"], "https://example.test/community-guide")
		self.assertEqual(int(row["verification_evidence_required"]), 1)
		self.assertEqual(tuple(member), ("verified", "Reviewed membership request"))
		self.assertEqual(audit_count, 2)
		self.assertEqual(resource_count, 0)

	def test_admin_can_link_discord_with_single_use_tenant_bound_intent(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			pilot_invite_code = issue_pilot_invitation(
				connection, community_id=1,
				expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
				created_by_operator_id=1,
			)
		finally:
			connection.close()
		with self.opener.open(Request(
			f"{self.base_url}/integrations", headers={"Cookie": cookie}
		)) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Link Discord", body)
		self.assertIn("guild-1", body)

		link_request = Request(
			f"{self.base_url}/integrations/discord/link",
			data=urlencode({
				"community_id": "1", "guild_id": "guild-1",
				"pilot_invite_code": pilot_invite_code,
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_response:
			self.opener.open(link_request)
		self.assertEqual(link_response.exception.code, 302)
		authorize_url = link_response.exception.headers["Location"]
		link_response.exception.close()
		authorize_query = parse_qs(urlparse(authorize_url).query)
		self.assertEqual(authorize_query["guild_id"], ["guild-1"])
		self.assertEqual(
			authorize_query["redirect_uri"],
			["https://dashboard.example.test/integrations/discord/callback"],
		)
		state = authorize_query["state"][0]
		tampered_callback_url = f"{self.base_url}/integrations/discord/callback?" + urlencode({
			"code": "discord-install-code", "guild_id": "guild-2", "state": state,
		})
		with self.assertRaises(HTTPError) as tampered_response:
			self.opener.open(Request(tampered_callback_url, headers={"Cookie": cookie}))
		self.assertEqual(tampered_response.exception.code, 400)
		tampered_response.exception.close()

		callback_url = f"{self.base_url}/integrations/discord/callback?" + urlencode({
			"code": "discord-install-code", "guild_id": "guild-1", "state": state,
		})
		with mock.patch(
			"src.dashboard.server.exchange_discord_code_for_token",
			return_value="discord-install-token",
		):
			with self.assertRaises(HTTPError) as callback_response:
				self.opener.open(Request(callback_url, headers={"Cookie": cookie}))
		self.assertEqual(callback_response.exception.code, 302)
		self.assertIn("Discord%20installation%20pending", callback_response.exception.headers["Location"])
		callback_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			installation = connection.execute(
				"""SELECT community_id, status FROM community_installations
				   WHERE platform='discord' AND external_community_id='guild-1'"""
			).fetchone()
			self.assertIsNotNone(installation)
			self.assertEqual((int(installation[0]), str(installation[1])), (1, "pending"))
			audit_actions = connection.execute(
				"""SELECT actor_type,actor_id,action_type,payload_json FROM audit_log
				   WHERE action_type IN (
				       'integration.discord_link_intent_created',
				       'integration.discord_link_pending'
				   ) ORDER BY id"""
			).fetchall()
			self.assertEqual([row[2] for row in audit_actions], [
				"integration.discord_link_intent_created", "integration.discord_link_pending",
			])
			self.assertTrue(all(row[0] == "operator" and int(row[1]) == 1 for row in audit_actions))
			self.assertTrue(all(json.loads(row[3])["guild_id"] == "guild-1" for row in audit_actions))
		finally:
			connection.close()

		with self.assertRaises(HTTPError) as replay_response:
			self.opener.open(Request(callback_url, headers={"Cookie": cookie}))
		self.assertEqual(replay_response.exception.code, 400)
		replay_response.exception.close()

	def test_admin_can_resume_twitch_broadcaster_onboarding(self) -> None:
		cookie = self._issue_operator_session_cookie()
		request = Request(
			f"{self.base_url}/integrations/twitch/link",
			data=urlencode({
				"broadcaster_login": "Streamer_A",
				"scope": ["moderator:read:followers", "channel:read:subscriptions"],
			}, doseq=True).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_response:
			self.opener.open(request)
		self.assertEqual(link_response.exception.code, 302)
		authorize_query = parse_qs(urlparse(link_response.exception.headers["Location"]).query)
		link_response.exception.close()
		self.assertEqual(authorize_query["client_id"], ["twitch-client-id"])
		self.assertEqual(set(authorize_query["scope"][0].split()), {
			"moderator:read:followers", "channel:read:subscriptions",
		})
		state = authorize_query["state"][0]

		interrupted_url = f"{self.base_url}/integrations/twitch/callback?" + urlencode({
			"error": "temporarily_unavailable", "state": state,
		})
		with self.assertRaises(HTTPError) as interrupted:
			self.opener.open(Request(interrupted_url, headers={"Cookie": cookie}))
		self.assertEqual(interrupted.exception.code, 302)
		self.assertIn("resume=", interrupted.exception.headers["Location"])
		interrupted.exception.close()

		callback_url = f"{self.base_url}/integrations/twitch/callback?" + urlencode({
			"code": "twitch-code", "state": state,
		})
		with (
			mock.patch(
				"src.dashboard.server.exchange_twitch_code_for_tokens",
				return_value=TwitchOAuthGrant(
					"access-secret", "refresh-secret",
					("moderator:read:followers", "channel:read:subscriptions"),
				),
			),
			mock.patch(
				"src.dashboard.server.TwitchTokenManager.validate_token",
				return_value=TwitchTokenValidation(
					"access-secret", "streamer_a", "twitch-client-id", "12345"
				),
			),
			mock.patch(
				"src.dashboard.server.TwitchEventSubControlPlane.reconcile",
				return_value={"existing": 0, "created": 2, "desired": 2},
			),
		):
			with self.assertRaises(HTTPError) as callback_response:
				self.opener.open(Request(callback_url, headers={"Cookie": cookie}))
		self.assertEqual(callback_response.exception.code, 302)
		self.assertIn("Twitch%20installation%20pending", callback_response.exception.headers["Location"])
		callback_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			installation = connection.execute(
				"""SELECT id,status,health_status,metadata_json
				   FROM community_installations
				   WHERE platform='twitch' AND external_community_id='12345'"""
			).fetchone()
			self.assertIsNotNone(installation)
			self.assertEqual((installation[1], installation[2]), ("active", "healthy"))
			self.assertEqual(json.loads(str(installation[3]))["moderation_mode"], "shadow")
			ciphertext = bytes(connection.execute(
				"SELECT access_token_ciphertext FROM installation_credentials WHERE installation_id=?",
				(int(installation[0]),),
			).fetchone()[0])
			self.assertNotIn(b"access-secret", ciphertext)
		finally:
			connection.close()

	def test_operator_switches_active_community_and_live_ops_ignores_query_tenant(self) -> None:
		ingest_and_analyze(
			DiscordConnector(self.database_path),
			{
				"id": "community-one-message",
				"timestamp": "2026-08-26T12:00:00Z",
				"channel_id": "channel-1",
				"guild_id": "guild-1",
				"content": "community one content",
				"author": {"id": "member-1", "username": "member", "bot": False},
			},
		)
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			message_id = int(connection.execute(
				"SELECT id FROM messages WHERE platform_message_id='community-one-message'"
			).fetchone()[0])
			organization_id = create_organization(
				connection, name="Second Organization", slug="second-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Second Workspace", slug="second-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Second Community", slug="second-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		self.assertEqual(switch_response.exception.code, 302)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session="))
		switched_cookie = switched_cookie.split(";", 1)[0]
		switch_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/api/live-ops?community_id=1",
			headers={"Cookie": switched_cookie},
		)) as response:
			payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(payload["community"]["id"], community_id)
		with self.opener.open(Request(
			f"{self.base_url}/dashboard", headers={"Cookie": switched_cookie},
		)) as response:
			dashboard_body = response.read().decode("utf-8")
		self.assertIn("Second Community", dashboard_body)
		self.assertIn(f"value='{community_id}' selected", dashboard_body)

		moderation_request = Request(
			f"{self.base_url}/api/live-ops/moderate",
			data=json.dumps({
				"message_id": message_id, "community_id": 1,
				"action_type": "timeout", "duration_seconds": 600,
			}).encode("utf-8"),
			headers={"Cookie": switched_cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as moderation_response:
			self.opener.open(moderation_request)
		self.assertEqual(moderation_response.exception.code, 404)
		moderation_response.exception.close()

	def test_member_report_and_appeal_queues_are_tenant_isolated(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, {
			"id": "member-queue-message", "timestamp": "2026-08-26T15:00:00Z",
			"channel_id": "queue-channel", "guild_id": "guild-1", "content": "queue evidence",
			"author": {"id": "queue-member", "username": "queue-member", "bot": False},
		})
		connection = connect_database(self.database_path)
		try:
			message = connection.execute(
				"""SELECT id,platform_account_id,user_id FROM messages
				   WHERE platform_message_id='member-queue-message'"""
			).fetchone()
			with connection:
				reviewer_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('queue-reviewer','reviewer','moderator')"
				).lastrowid)
			grant_operator_role(connection, operator_id=reviewer_id, community_id=1, role="moderator")
			action_id = record_moderation_action(
				connection, platform="discord", message_id=int(message[0]),
				target_platform_account_id=int(message[1]), action_type="timeout",
				reason="Original decision", status="completed", actor_type="operator", actor_id=1,
				community_id=1,
			)
			report_id = create_member_report(
				connection, community_id=1, subject_platform_account_id=int(message[1]),
				category="harassment", summary="Active tenant report", severity="high",
			)
			appeal_id = create_member_appeal(
				connection, community_id=1, moderation_action_id=action_id,
				appellant_platform_account_id=int(message[1]), reason="Independent review requested",
				severity="high",
			)
			other_community_id = create_community(
				connection, workspace_id=1, name="Foreign queue", slug="foreign-queue"
			)
			with connection:
				foreign_user_id = int(connection.execute(
					"INSERT INTO users(primary_display_name) VALUES ('Foreign queue member')"
				).lastrowid)
				foreign_account_id = int(connection.execute(
					"""INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
					   VALUES ('discord','foreign-queue-member','foreign-queue-member',?)""",
					(foreign_user_id,),
				).lastrowid)
				foreign_observation_id = int(connection.execute(
					"""INSERT INTO observations(
					       community_id,platform,event_type,external_event_id,context_id,
					       text_raw,attributes_json,raw_payload_json,occurred_at
					   ) VALUES (?,'discord','message.created','foreign-queue-event','foreign',
					             'foreign','{}','{}',CURRENT_TIMESTAMP)""", (other_community_id,),
				).lastrowid)
				connection.execute(
					"""INSERT INTO messages(
					       observation_id,platform,platform_message_id,platform_account_id,user_id,
					       community_id,channel_id,content_raw,content_normalized,sent_at
					   ) VALUES (?,'discord','foreign-queue-message',?,?,?,'foreign',
					             'foreign','foreign',CURRENT_TIMESTAMP)""",
					(foreign_observation_id, foreign_account_id, foreign_user_id, other_community_id),
				)
			create_member_report(
				connection, community_id=other_community_id,
				subject_platform_account_id=foreign_account_id, category="spam",
				summary="Foreign tenant report", severity="critical",
			)
		finally:
			connection.close()

		with self.opener.open(Request(f"{self.base_url}/moderation", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Member reports", body)
		self.assertIn("Sanction appeals", body)
		self.assertIn("Work queue", body)
		self.assertIn("Unassigned", body)
		self.assertIn("SLA age", body)
		self.assertIn("tabindex='0' class='work-row'", body)
		self.assertIn("event.key==='ArrowDown'", body)
		self.assertIn("Active tenant report", body)
		self.assertIn("Independent review requested", body)
		self.assertNotIn("Foreign tenant report", body)
		with self.opener.open(Request(f"{self.base_url}/analytics", headers={"Cookie": cookie})) as response:
			analytics_body = response.read().decode("utf-8")
		for heading in (
			"Community growth", "Repeat offenses", "Report outcomes", "Appeal outcomes", "Rule precision",
		):
			self.assertIn(heading, analytics_body)
		with self.opener.open(Request(
			f"{self.base_url}/analytics/export.json", headers={"Cookie": cookie}
		)) as response:
			export_payload = json.loads(response.read().decode("utf-8"))
		self.assertTrue({
			"growth", "repeat_offenses", "report_outcomes", "appeal_outcomes", "rule_precision",
		}.issubset(export_payload))

		def post_form(path: str, values: dict[str, str]) -> HTTPError:
			request = Request(
				f"{self.base_url}{path}", data=urlencode(values).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
				method="POST",
			)
			with self.assertRaises(HTTPError) as response:
				self.opener.open(request)
			self.assertEqual(response.exception.code, 302)
			return response.exception

		saved_response = post_form("/moderation/filters", {
			"name": "High Discord", "filters": json.dumps({
				"queue": "unassigned", "severity": "high", "platform": "discord",
			}),
		})
		self.assertIn("Filter%20saved", saved_response.headers["Location"])
		saved_response.close()
		assigned_response = post_form(f"/moderation/work/report/{report_id}/assign", {})
		self.assertIn("queue=mine", assigned_response.headers["Location"])
		assigned_response.close()
		with self.opener.open(Request(
			f"{self.base_url}/moderation?queue=mine", headers={"Cookie": cookie},
		)) as response:
			mine_body = response.read().decode("utf-8")
		self.assertIn("High Discord", mine_body)
		self.assertIn("Active tenant report", mine_body)
		self.assertIn("aria-current=page>Mine", mine_body)
		foreign_assignment = post_form(
			f"/moderation/work/report/{report_id + 1}/assign", {},
		)
		self.assertIn("not%20found", foreign_assignment.headers["Location"])
		foreign_assignment.close()
		connection = connect_database(self.database_path)
		try:
			self.assertEqual(connection.execute(
				"SELECT assigned_operator_id FROM member_reports WHERE id=?", (report_id,),
			).fetchone()[0], 1)
			self.assertIsNone(connection.execute(
				"SELECT assigned_operator_id FROM member_reports WHERE community_id=?",
				(other_community_id,),
			).fetchone()[0])
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM moderation_saved_filters WHERE community_id=1 AND operator_id=1",
			).fetchone()[0], 1)
		finally:
			connection.close()

		def resolve_request(path: str, resolution: str) -> Request:
			return Request(
				f"{self.base_url}{path}",
				data=urlencode({"resolution": resolution, "note": "Reviewed"}).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
				method="POST",
			)
		with self.assertRaises(HTTPError) as report_response:
			self.opener.open(resolve_request(f"/moderation/reports/{report_id}/resolve", "substantiated"))
		self.assertEqual(report_response.exception.code, 302)
		self.assertIn("Report+resolved", report_response.exception.headers["Location"])
		report_response.exception.close()
		with self.assertRaises(HTTPError) as appeal_response:
			self.opener.open(resolve_request(f"/moderation/appeals/{appeal_id}/resolve", "upheld"))
		self.assertEqual(appeal_response.exception.code, 302)
		self.assertIn("different%20reviewer", appeal_response.exception.headers["Location"])
		appeal_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			self.assertEqual(connection.execute(
				"SELECT status FROM member_reports WHERE id=?", (report_id,)
			).fetchone()[0], "resolved")
			self.assertEqual(connection.execute(
				"SELECT status FROM member_appeals WHERE id=?", (appeal_id,)
			).fetchone()[0], "open")
		finally:
			connection.close()

		unauthorized_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": "99999"}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as unauthorized_response:
			self.opener.open(unauthorized_request)
		self.assertEqual(unauthorized_response.exception.code, 403)
		unauthorized_response.exception.close()

	def test_destructive_apis_require_typed_confirmation_after_bulk_preview(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, {
			"id": "destructive-api-message", "timestamp": "2026-08-26T12:00:00Z",
			"channel_id": "destructive-channel", "guild_id": "guild-1", "content": "target",
			"author": {"id": "destructive-user", "username": "target", "bot": False},
		})
		connection = connect_database(self.database_path)
		try:
			message_row = connection.execute(
				"SELECT id,platform_account_id FROM messages WHERE platform_message_id='destructive-api-message'"
			).fetchone()
			message_id, target_id = int(message_row[0]), int(message_row[1])
			installation_id = register_installation(
				connection, community_id=1, platform="discord",
				external_community_id="destructive-api-guild", display_name="Destructive API Guild",
			)
		finally:
			connection.close()

		def bulk_request(payload: dict[str, object]) -> Request:
			return Request(
				f"{self.base_url}/api/moderation/bulk", data=json.dumps(payload).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST",
			)

		payload = {
			"target_platform_account_ids": [target_id, 999999], "action_type": "timeout",
			"reason": "API bulk test", "dry_run": True,
		}
		with self.opener.open(bulk_request(payload)) as response:
			preview = json.loads(response.read().decode("utf-8"))
		self.assertEqual([item["status"] for item in preview["results"]], ["eligible", "not_found"])
		payload["dry_run"] = False
		with self.assertRaises(HTTPError) as unconfirmed:
			self.opener.open(bulk_request(payload))
		self.assertEqual(unconfirmed.exception.code, 409)
		unconfirmed.exception.close()
		payload["confirmation"] = "BULK TIMEOUT 2"
		with self.opener.open(bulk_request(payload)) as response:
			executed = json.loads(response.read().decode("utf-8"))
		self.assertEqual([item["status"] for item in executed["results"]], ["queued", "not_found"])

		revoke_url = f"{self.base_url}/api/integrations/{installation_id}/revoke"
		with self.assertRaises(HTTPError) as unconfirmed_revoke:
			self.opener.open(Request(
				revoke_url, data=b"{}", headers={"Cookie": cookie, "Content-Type": "application/json"},
				method="POST",
			))
		self.assertEqual(unconfirmed_revoke.exception.code, 409)
		unconfirmed_revoke.exception.close()
		with self.opener.open(Request(
			revoke_url, data=json.dumps({"confirmation": f"REVOKE INTEGRATION {installation_id}"}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST",
		)) as response:
			self.assertEqual(json.loads(response.read().decode("utf-8"))["status"], "revoked")

		def json_post(path: str, payload: dict[str, object]) -> Request:
			return Request(
				f"{self.base_url}{path}", data=json.dumps(payload).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST",
			)

		ban_payload = {"message_id": message_id, "action_type": "ban", "reason": "Permanent threat"}
		with self.assertRaises(HTTPError) as unconfirmed_ban:
			self.opener.open(json_post("/api/live-ops/moderate", ban_payload))
		self.assertEqual(unconfirmed_ban.exception.code, 409)
		unconfirmed_ban.exception.close()
		ban_payload["confirmation"] = "PERMANENT BAN"
		with self.opener.open(json_post("/api/live-ops/moderate", ban_payload)) as response:
			self.assertEqual(response.status, 202)

		connection = connect_database(self.database_path)
		try:
			with connection:
				review_id = int(connection.execute(
					"""INSERT INTO review_queue(message_id,status,severity,queue_reason_code)
					   VALUES (?,'open','critical','permanent_threat')""", (message_id,),
				).lastrowid)
		finally:
			connection.close()
		def review_request(confirmation: str = "") -> Request:
			return Request(
				f"{self.base_url}/moderation/reviews/{review_id}/resolve",
				data=urlencode({
					"resolution": "confirmed", "action_type": "ban", "note": "Permanent threat",
					"confirmation": confirmation,
				}).encode("utf-8"),
				headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
				method="POST",
			)
		with self.assertRaises(HTTPError) as unconfirmed_review:
			self.opener.open(review_request())
		self.assertEqual(unconfirmed_review.exception.code, 302)
		unconfirmed_review.exception.close()
		connection = connect_database(self.database_path)
		try:
			self.assertEqual(connection.execute(
				"SELECT status FROM review_queue WHERE id=?", (review_id,)
			).fetchone()[0], "open")
		finally:
			connection.close()
		with self.assertRaises(HTTPError) as confirmed_review:
			self.opener.open(review_request("PERMANENT BAN"))
		self.assertEqual(confirmed_review.exception.code, 302)
		confirmed_review.exception.close()

		connection = connect_database(self.database_path)
		try:
			with connection:
				target_operator_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('destructive-target','target','moderator')"
				).lastrowid)
			grant_operator_role(connection, operator_id=target_operator_id, community_id=1, role="moderator")
		finally:
			connection.close()
		for action, confirmation in (
			("emergency-remove", f"EMERGENCY REMOVE {target_operator_id}"),
			("transfer-ownership", f"TRANSFER OWNERSHIP {target_operator_id}"),
		):
			path = f"/api/operators/{target_operator_id}/{action}"
			with self.assertRaises(HTTPError) as unconfirmed_operator_action:
				self.opener.open(json_post(path, {"reason": "Security response"}))
			self.assertEqual(unconfirmed_operator_action.exception.code, 409)
			unconfirmed_operator_action.exception.close()
			if action == "emergency-remove":
				with self.opener.open(json_post(path, {
					"reason": "Security response", "confirmation": confirmation,
				})) as response:
					self.assertEqual(response.status, 200)

				connection = connect_database(self.database_path)
				try:
					with connection:
						connection.execute(
							"UPDATE operator_accounts SET status='active' WHERE id=?", (target_operator_id,)
						)
					grant_operator_role(
						connection, operator_id=target_operator_id, community_id=1, role="moderator"
					)
				finally:
					connection.close()
			else:
				with self.opener.open(json_post(path, {"confirmation": confirmation})) as response:
					self.assertEqual(response.status, 200)

	def test_community_settings_hub_updates_active_tenant_and_audits(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			other_community_id = create_community(
				connection, workspace_id=1,
				name="Untouched Community", slug="untouched-community",
			)
		finally:
			connection.close()
		with self.opener.open(Request(
			f"{self.base_url}/settings", headers={"Cookie": cookie},
		)) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Community administration", body)
		self.assertIn("Profile and locale", body)
		self.assertIn("Retention and anti-abuse policy", body)
		self.assertIn("Notification destinations", body)
		self.assertIn("Operators", body)

		settings_request = Request(
			f"{self.base_url}/settings",
			data=urlencode({
				"name": "Configured Community", "locale": "en-GB",
				"timezone": "Europe/London", "description": "A configured tenant profile.",
				"guidelines": "Be useful and respectful.", "notifications_enabled": "on",
				"message_retention_days": "45", "analytics_retention_days": "180",
				"anti_abuse_enabled": "on", "anti_abuse_enforcement_mode": "enforce",
				"message_burst_limit": "15", "message_burst_window_seconds": "20",
				"mention_limit": "6", "join_raid_limit": "30",
				"join_raid_window_seconds": "90",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as update_response:
			self.opener.open(settings_request)
		self.assertEqual(update_response.exception.code, 302)
		self.assertEqual(update_response.exception.headers["Location"], "/settings?status=Settings%20saved")
		update_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			community = connection.execute(
				"""SELECT name,locale,timezone,description,guidelines,notifications_enabled
				   FROM communities WHERE id=1"""
			).fetchone()
			policy = connection.execute(
				"""SELECT message_retention_days,analytics_retention_days,
				          anti_abuse_enforcement_mode,message_burst_limit,join_raid_limit
				   FROM community_policy_settings WHERE community_id=1"""
			).fetchone()
			other = connection.execute(
				"SELECT name,locale,timezone FROM communities WHERE id=?", (other_community_id,),
			).fetchone()
			actions = [str(row[0]) for row in connection.execute(
				"""SELECT action_type FROM audit_log
				   WHERE entity_type='community' AND entity_id=1
				     AND action_type IN ('community.settings_updated',
				                         'retention.policy_updated','anti_abuse.policy_updated')
				   ORDER BY id"""
			).fetchall()]
		finally:
			connection.close()
		self.assertEqual(tuple(community), (
			"Configured Community", "en-GB", "Europe/London",
			"A configured tenant profile.", "Be useful and respectful.", 1,
		))
		self.assertEqual(tuple(policy), (45, 180, "enforce", 15, 30))
		self.assertEqual(tuple(other), ("Untouched Community", "en-US", "UTC"))
		self.assertEqual(actions, [
			"community.settings_updated", "retention.policy_updated",
			"anti_abuse.policy_updated",
		])
		invite_request = Request(
			f"{self.base_url}/settings/operators/invite",
			data=urlencode({
				"discord_user_id": "settings-invitee",
				"role": "moderator",
				"expires_hours": "48",
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as invite_response:
			self.opener.open(invite_request)
		self.assertEqual(invite_response.exception.code, 302)
		invite_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			invitation = connection.execute(
				"""SELECT community_id,invited_role,status
				   FROM operator_invitations WHERE target_discord_user_id='settings-invitee'"""
			).fetchone()
		finally:
			connection.close()
		self.assertEqual(tuple(invitation), (1, "moderator", "pending"))

	def test_application_shell_hides_denied_capabilities_and_renders_breadcrumb(self) -> None:
		self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			set_operator_permission_override(
				connection, operator_id=1, community_id=1,
				permission="settings.manage", decision="deny",
				actor_operator_id=1,
			)
		finally:
			connection.close()
		cookie = self._issue_operator_session_cookie()

		with self.opener.open(Request(
			f"{self.base_url}/analytics", headers={"Cookie": cookie},
		)) as response:
			body = response.read().decode("utf-8")

		self.assertIn("<a href='/dashboard'>Overview</a>", body)
		self.assertNotIn("<a href='/settings'>Settings</a>", body)
		self.assertNotIn("<a href='/commands'>Commands</a>", body)
		self.assertNotIn("<a href='/onboarding'>Onboarding</a>", body)
		self.assertIn("aria-label='Breadcrumb'", body)
		self.assertIn("aria-current='page'>Analytics", body)

	def test_moderation_daily_workflow_enforces_permission_denials(self) -> None:
		self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			set_operator_permission_override(
				connection, operator_id=1, community_id=1,
				permission="moderation.queues.read", decision="deny",
				actor_operator_id=1,
			)
		finally:
			connection.close()
		cookie = self._issue_operator_session_cookie()
		for path in ("/moderation", "/api/moderation/reviews", "/api/moderation/actions"):
			with self.subTest(path=path):
				with self.assertRaises(HTTPError) as denied:
					self.opener.open(Request(
						f"{self.base_url}{path}", headers={"Cookie": cookie},
					))
				self.assertEqual(denied.exception.code, 403)
				denied.exception.close()

		connection = connect_database(self.database_path)
		try:
			set_operator_permission_override(
				connection, operator_id=1, community_id=1,
				permission="moderation.queues.read", decision="grant",
				actor_operator_id=1,
			)
			set_operator_permission_override(
				connection, operator_id=1, community_id=1,
				permission="moderation.manage", decision="deny",
				actor_operator_id=1,
			)
		finally:
			connection.close()
		cookie = self._issue_operator_session_cookie()
		assignment = Request(
			f"{self.base_url}/moderation/work/report/1/assign",
			data=b"", headers={
				"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded",
			}, method="POST",
		)
		with self.assertRaises(HTTPError) as denied_assignment:
			self.opener.open(assignment)
		self.assertEqual(denied_assignment.exception.code, 403)
		denied_assignment.exception.close()

	def test_slo_api_uses_active_tenant_and_requires_analytics_permission(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			other_community_id = create_community(
				connection, workspace_id=1, name="SLO API peer", slug="slo-api-peer",
			)
		finally:
			connection.close()
		with self.opener.open(Request(
			f"{self.base_url}/api/slo?community_id={other_community_id}",
			headers={"Cookie": cookie},
		)) as response:
			payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(payload["community_id"], 1)
		self.assertEqual(len(payload["items"]), 8)

		connection = connect_database(self.database_path)
		try:
			set_operator_permission_override(
				connection, operator_id=1, community_id=1,
				permission="analytics.read", decision="deny", actor_operator_id=1,
			)
		finally:
			connection.close()
		cookie = self._issue_operator_session_cookie()
		with self.assertRaises(HTTPError) as denied:
			self.opener.open(Request(
				f"{self.base_url}/api/slo", headers={"Cookie": cookie},
			))
		self.assertEqual(denied.exception.code, 403)
		denied.exception.close()

	def test_api_and_export_quotas_return_retryable_backpressure(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			for quota_type in ("api", "exports"):
				configure_tenant_quota(
					connection, tenant=TenantContext(1),
					actor=ActorAttribution("operator", 1), quota_type=quota_type,
					limit_count=1, window_seconds=60,
				)
		finally:
			connection.close()
		with self.opener.open(Request(
			f"{self.base_url}/api/slo", headers={"Cookie": cookie},
		)) as response:
			self.assertEqual(response.status, 200)
		with self.assertRaises(HTTPError) as api_limited:
			self.opener.open(Request(
				f"{self.base_url}/api/slo", headers={"Cookie": cookie},
			))
		self.assertEqual(api_limited.exception.code, 429)
		self.assertGreater(int(api_limited.exception.headers["Retry-After"]), 0)
		api_limited.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/analytics/export.json", headers={"Cookie": cookie},
		)) as response:
			self.assertEqual(response.status, 200)
		with self.assertRaises(HTTPError) as export_limited:
			self.opener.open(Request(
				f"{self.base_url}/analytics/export.json", headers={"Cookie": cookie},
			))
		self.assertEqual(export_limited.exception.code, 429)
		self.assertEqual(json.loads(export_limited.exception.read())["quota_type"], "exports")
		export_limited.exception.close()

	def test_moderation_work_queues_filter_page_and_isolate_tenants(self) -> None:
		self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			other_community_id = create_community(
				connection, workspace_id=1, name="Queue Peer", slug="queue-peer",
			)
			with connection:
				account_id = int(connection.execute(
					"""INSERT INTO platform_accounts(platform,platform_user_id,username)
					   VALUES ('discord','queue-subject','Queue Subject') RETURNING id"""
				).fetchone()[0])
				for index, community_id in enumerate((1, 1, 1, 1, other_community_id), 1):
					observation_id = int(connection.execute(
						"""INSERT INTO observations(
						       community_id,platform,event_type,external_event_id,
						       text_raw,attributes_json,raw_payload_json,occurred_at
						   ) VALUES (?,'discord','message.created',?,?,'{}','{}','2026-09-01T00:00:00+00:00')
						   RETURNING id""",
						(community_id, f"queue-event-{index}", f"queue content {index}"),
					).fetchone()[0])
					message_id = int(connection.execute(
						"""INSERT INTO messages(
						       observation_id,platform,platform_message_id,platform_account_id,
						       community_id,channel_id,content_raw,content_normalized,sent_at
						   ) VALUES (?,'discord',?,?,?,'queue',?,?,'2026-09-01T00:00:00+00:00')
						   RETURNING id""",
						(observation_id, f"queue-message-{index}", account_id, community_id,
						 f"queue content {index}", f"queue content {index}"),
					).fetchone()[0])
					connection.execute(
						"""INSERT INTO review_queue(
						       message_id,status,severity,queue_reason_code,
						       assigned_operator_id,resolution,created_at
						   ) VALUES (?,?,?,?,?,?,?)""",
						(message_id, "open" if index in {1, 2, 5} else "resolved",
						 "high" if index in {1, 3, 5} else "low",
						 "rule.alpha" if index in {1, 3, 5} else "rule.beta",
						 1 if index == 2 else None,
						 "escalated" if index == 3 else ("confirmed" if index == 4 else None),
						 f"2026-09-0{index}T00:00:00+00:00"),
					)
				action_id = record_moderation_action(
					connection, platform="discord", message_id=None,
					target_platform_account_id=account_id, action_type="timeout",
					reason="appealed", status="completed", actor_type="operator",
					actor_id=1, community_id=1,
				)
				create_member_appeal(
					connection, community_id=1, moderation_action_id=action_id,
					appellant_platform_account_id=account_id,
					reason="appeal reason", severity="critical",
				)

			unassigned, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="unassigned",
			)
			mine, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="mine",
			)
			escalated, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="escalated",
			)
			appeals, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="appeals",
			)
			resolved, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="resolved",
			)
			filtered, _ = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="all",
				search="content 1", severity="high", rule="rule.alpha",
				platform="discord",
			)
			page, total = list_moderation_work(
				connection, community_id=1, operator_id=1, queue="all", limit=2, offset=2,
			)
		finally:
			connection.close()
		self.assertEqual({item.work_type for item in unassigned}, {"review", "appeal"})
		self.assertEqual([item.reason for item in mine], ["rule.beta"])
		self.assertEqual([item.reason for item in escalated], ["rule.alpha"])
		self.assertEqual([item.work_type for item in appeals], ["appeal"])
		self.assertEqual(len(resolved), 2)
		self.assertEqual([item.summary for item in filtered], ["queue content 1"])
		self.assertEqual((len(page), total), (2, 5))
		self.assertTrue(all(item.sla_age_hours >= 0 for item in page))

	def test_live_ops_shift_schedule_routes_incident_to_on_call(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			with connection:
				on_call_operator_id = int(connection.execute(
					"INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('on-call-api','on-call','moderator')"
				).lastrowid)
				incident_id = int(connection.execute(
					"""INSERT INTO operations_incidents(
					       community_id,incident_type,severity,title,summary
					   ) VALUES (1,'api-routing','high','API incident','Route this')"""
				).lastrowid)
			grant_operator_role(
				connection, operator_id=on_call_operator_id, community_id=1, role="moderator"
			)
		finally:
			connection.close()
		now = datetime.now(timezone.utc)
		schedule_request = Request(
			f"{self.base_url}/api/live-ops/shifts",
			data=json.dumps({
				"operator_id": on_call_operator_id,
				"starts_at": (now - timedelta(minutes=5)).isoformat(),
				"ends_at": (now + timedelta(hours=1)).isoformat(),
			}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST",
		)
		with self.opener.open(schedule_request) as response:
			schedule_payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(schedule_payload["shifts"][0]["operator_id"], on_call_operator_id)
		with self.opener.open(Request(
			f"{self.base_url}/api/live-ops/shifts", headers={"Cookie": cookie}
		)) as response:
			list_payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(list_payload["shifts"][0]["discord_username"], "on-call")
		route_request = Request(
			f"{self.base_url}/api/live-ops/incidents/{incident_id}/route-on-call",
			data=b"{}", headers={"Cookie": cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.opener.open(route_request) as response:
			route_payload = json.loads(response.read().decode("utf-8"))
		self.assertEqual(route_payload["assigned_operator_id"], on_call_operator_id)

	def test_moderation_apis_are_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="Moderation Organization", slug="moderation-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Moderation Workspace", slug="moderation-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Moderation Community", slug="moderation-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				account_id = int(connection.execute(
					"""INSERT INTO platform_accounts(platform,platform_user_id,username)
					   VALUES ('discord','moderation-member','moderation-member')"""
				).lastrowid)
				message_ids = {}
				for tenant_id, label in ((1, "tenant-a"), (community_id, "tenant-b")):
					message_ids[label] = int(connection.execute(
						"""INSERT INTO messages(
						       platform,platform_message_id,platform_account_id,community_id,
						       channel_id,content_raw,content_normalized,sent_at
						   ) VALUES ('discord',?,?,?,?,?,?,?)""",
						(
							f"{label}-message", account_id, tenant_id, "channel",
							f"{label} review content", f"{label} review content",
							"2026-08-26T12:00:00+00:00",
						),
					).lastrowid)
				review_a = int(connection.execute(
					"""INSERT INTO review_queue(message_id,severity,queue_reason_code)
					   VALUES (?,'high','tenant_a_review')""", (message_ids["tenant-a"],)
				).lastrowid)
				connection.execute(
					"""INSERT INTO review_queue(message_id,severity,queue_reason_code)
					   VALUES (?,'medium','tenant_b_review')""", (message_ids["tenant-b"],)
				)
			upsert_moderation_rule(
				connection, community_id=1, name="Tenant A Rule",
				rule_type="exact_term", pattern="tenant-a", severity="high",
			)
			upsert_moderation_rule(
				connection, community_id=community_id, name="Tenant B Rule",
				rule_type="exact_term", pattern="tenant-b", severity="medium",
			)
			record_moderation_action(
				connection, community_id=1, platform="discord",
				message_id=message_ids["tenant-a"], target_platform_account_id=account_id,
				action_type="warn", reason="tenant-a-action",
			)
			record_moderation_action(
				connection, community_id=community_id, platform="discord",
				message_id=message_ids["tenant-b"], target_platform_account_id=account_id,
				action_type="timeout", reason="tenant-b-action",
			)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		for endpoint, included, excluded in (
			("reviews", "tenant_b_review", "tenant_a_review"),
			("rules", "Tenant B Rule", "Tenant A Rule"),
			("actions", "tenant-b-action", "tenant-a-action"),
		):
			with self.opener.open(Request(
				f"{self.base_url}/api/moderation/{endpoint}",
				headers={"Cookie": switched_cookie},
			)) as response:
				body = response.read().decode("utf-8")
			self.assertIn(included, body)
			self.assertNotIn(excluded, body)

		resolve_request = Request(
			f"{self.base_url}/api/moderation/reviews/{review_a}/resolve",
			data=json.dumps({"resolution": "dismissed"}).encode("utf-8"),
			headers={"Cookie": switched_cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as resolve_response:
			self.opener.open(resolve_request)
		self.assertEqual(resolve_response.exception.code, 400)
		resolve_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			status = connection.execute(
				"SELECT status FROM review_queue WHERE id=?", (review_a,)
			).fetchone()[0]
		finally:
			connection.close()
		self.assertEqual(status, "open")

	def test_user_surfaces_and_mutations_are_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="User Organization", slug="user-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="User Workspace", slug="user-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="User Community", slug="user-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				user_ids = {}
				account_ids = {}
				for tenant_id, label in ((1, "tenant-a-user"), (community_id, "tenant-b-user")):
					user_ids[label] = int(connection.execute(
						"INSERT INTO users(primary_display_name) VALUES (?)", (label,)
					).lastrowid)
					account_ids[label] = int(connection.execute(
						"""INSERT INTO platform_accounts(
						       platform,platform_user_id,username,user_id
						   ) VALUES ('discord',?,?,?)""",
						(label, label, user_ids[label]),
					).lastrowid)
					connection.execute(
						"""INSERT INTO messages(
						       platform,platform_message_id,platform_account_id,user_id,
						       community_id,channel_id,content_raw,content_normalized,sent_at
						   ) VALUES ('discord',?,?,?,?,?,'message','message',?)""",
						(
							f"{label}-message", account_ids[label], user_ids[label],
							tenant_id, "channel", "2026-08-26T12:00:00+00:00",
						),
					)
				shared_account_id = int(connection.execute(
					"""INSERT INTO platform_accounts(
					       platform,platform_user_id,username,user_id
					   ) VALUES ('twitch','tenant-a-shared','tenant-a-shared',?)""",
					(user_ids["tenant-b-user"],),
				).lastrowid)
				connection.execute(
					"""INSERT INTO messages(
					       platform,platform_message_id,platform_account_id,user_id,
					       community_id,channel_id,content_raw,content_normalized,sent_at
					   ) VALUES ('twitch','tenant-a-shared-message',?,?,1,'channel','message','message',?)""",
					(shared_account_id, user_ids["tenant-b-user"], "2026-08-26T12:00:00+00:00"),
				)
				connection.execute(
					"""INSERT INTO user_notes(user_id,community_id,operator_id,body)
					   VALUES (?,?,1,?)""",
					(user_ids["tenant-b-user"], 1, "tenant-a-private-note"),
				)
				connection.execute(
					"""INSERT INTO user_notes(user_id,community_id,operator_id,body)
					   VALUES (?,?,1,?)""",
					(user_ids["tenant-b-user"], community_id, "tenant-b-note"),
				)
				connection.execute(
					"""INSERT INTO observations(
					       platform,community_id,event_type,external_event_id,
					       target_platform_account_id,attributes_json,occurred_at
					   ) VALUES ('twitch',1,'member.roles_changed','tenant-a-role',?,?,?)""",
					(shared_account_id, json.dumps({"roles": ["foreign-role"]}), "2026-08-26T12:01:00Z"),
				)
				connection.execute(
					"""INSERT INTO observations(
					       platform,community_id,event_type,external_event_id,
					       target_platform_account_id,attributes_json,occurred_at
					   ) VALUES ('discord',?,'member.roles_changed','tenant-b-role',?,?,?)""",
					(
						community_id, account_ids["tenant-b-user"],
						json.dumps({"roles": ["local-role"]}), "2026-08-26T12:02:00Z",
					),
				)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/api/users", headers={"Cookie": switched_cookie},
		)) as response:
			users_body = response.read().decode("utf-8")
		self.assertIn("tenant-b-user", users_body)
		self.assertNotIn("tenant-a-user", users_body)

		with self.assertRaises(HTTPError) as detail_response:
			self.opener.open(Request(
				f"{self.base_url}/api/users/{user_ids['tenant-a-user']}",
				headers={"Cookie": switched_cookie},
			))
		self.assertEqual(detail_response.exception.code, 404)
		detail_response.exception.close()
		with self.opener.open(Request(
			f"{self.base_url}/api/users/{user_ids['tenant-b-user']}",
			headers={"Cookie": switched_cookie},
		)) as response:
			profile_body = response.read().decode("utf-8")
		self.assertIn("tenant-b-user", profile_body)
		self.assertIn("tenant-b-note", profile_body)
		self.assertIn("local-role", profile_body)
		self.assertNotIn("tenant-a-shared", profile_body)
		self.assertNotIn("tenant-a-private-note", profile_body)
		self.assertNotIn("foreign-role", profile_body)
		with self.opener.open(Request(
			f"{self.base_url}/users/{user_ids['tenant-b-user']}",
			headers={"Cookie": switched_cookie},
		)) as response:
			lifecycle_body = response.read().decode("utf-8")
		self.assertIn("Member lifecycle", lifecycle_body)
		self.assertIn("tenant-b-note", lifecycle_body)
		self.assertIn("local-role", lifecycle_body)
		self.assertNotIn("tenant-a-private-note", lifecycle_body)
		self.assertNotIn("foreign-role", lifecycle_body)
		with self.opener.open(Request(
			f"{self.base_url}/users/{user_ids['tenant-b-user']}/lifecycle.csv",
			headers={"Cookie": switched_cookie},
		)) as response:
			export_body = response.read().decode("utf-8")
		self.assertIn("tenant-b-note", export_body)
		self.assertIn("local-role", export_body)
		self.assertNotIn("tenant-a-private-note", export_body)
		self.assertNotIn("foreign-role", export_body)
		with self.assertRaises(HTTPError) as export_response:
			self.opener.open(Request(
				f"{self.base_url}/users/{user_ids['tenant-a-user']}/lifecycle.csv",
				headers={"Cookie": switched_cookie},
			))
		self.assertEqual(export_response.exception.code, 404)
		export_response.exception.close()

		note_request = Request(
			f"{self.base_url}/api/users/{user_ids['tenant-a-user']}/notes",
			data=json.dumps({"body": "cross tenant note"}).encode("utf-8"),
			headers={"Cookie": switched_cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as note_response:
			self.opener.open(note_request)
		self.assertEqual(note_response.exception.code, 404)
		note_response.exception.close()

		link_request = Request(
			f"{self.base_url}/api/users/link",
			data=json.dumps({
				"user_id": user_ids["tenant-b-user"],
				"discord_user_id": "tenant-a-user",
			}).encode("utf-8"),
			headers={"Cookie": switched_cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as link_response:
			self.opener.open(link_request)
		self.assertEqual(link_response.exception.code, 404)
		link_response.exception.close()

		moderation_request = Request(
			f"{self.base_url}/users/{user_ids['tenant-b-user']}/moderation",
			data=urlencode({
				"target_platform_account_id": account_ids["tenant-a-user"],
				"action_type": "ban", "reason": "cross tenant action",
			}).encode("utf-8"),
			headers={"Cookie": switched_cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as moderation_response:
			self.opener.open(moderation_request)
		self.assertEqual(moderation_response.exception.code, 302)
		moderation_response.exception.close()
		connection = connect_database(self.database_path)
		try:
			cross_tenant_actions = int(connection.execute(
				"""SELECT COUNT(*) FROM moderation_actions
				   WHERE target_platform_account_id=? AND community_id=?""",
				(account_ids["tenant-a-user"], community_id),
			).fetchone()[0])
		finally:
			connection.close()
		self.assertEqual(cross_tenant_actions, 0)

	def test_admin_can_restart_bot_through_configured_systemd_service(self) -> None:
		cookie = self._issue_operator_session_cookie()
		with self.opener.open(Request(f"{self.base_url}/dashboard", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Go Live", body)
		self.assertIn("Restart Bot", body)
		self.assertIn("/dashboard/restart", body)
		self.assertIn("Reset Database", body)

		request = Request(
			f"{self.base_url}/dashboard/restart",
			data=b"",
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with mock.patch("src.dashboard.server.DashboardApp._schedule_systemd_restart") as schedule_restart:
			with self.assertRaises(HTTPError) as restart_response:
				self.opener.open(request)

		self.assertEqual(restart_response.exception.code, 302)
		self.assertIn("Restart%20requested%20for%20qbot4k.service", restart_response.exception.headers["Location"])
		restart_response.exception.close()
		schedule_restart.assert_called_once_with("qbot4k.service")

	def test_admin_can_reset_entire_database_with_explicit_confirmation(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(
			connector,
			{
				"id": "database-reset-message",
				"timestamp": "2026-08-10T12:00:00Z",
				"channel_id": "channel-reset",
				"guild_id": "guild-1",
				"content": "message to erase",
				"author": {"id": "reset-user", "username": "reset_user", "bot": False},
			},
		)
		cookie = self._issue_operator_session_cookie()

		with self.opener.open(Request(f"{self.base_url}/dashboard", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Reset Database", body)
		self.assertIn("/dashboard/reset-database", body)

		invalid_request = Request(
			f"{self.base_url}/dashboard/reset-database",
			data=b"confirmation=NO",
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as invalid_response:
			self.opener.open(invalid_request)
		invalid_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]), 1)
		finally:
			connection.close()

		reset_request = Request(
			f"{self.base_url}/dashboard/reset-database",
			data=b"confirmation=RESET",
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as reset_response:
			self.opener.open(reset_request)
		self.assertEqual(reset_response.exception.code, 302)
		self.assertIn("Database%20reset%20complete", reset_response.exception.headers["Location"])
		reset_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]), 0)
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]), 0)
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM operator_accounts").fetchone()[0]), 0)
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM command_definitions").fetchone()[0]), 1)
			self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM moderation_rules").fetchone()[0]), 2)
			if isinstance(self.database_path, str):
				self.assertEqual(int(connection.execute(
					"SELECT COUNT(*) FROM pg_constraint WHERE contype='f' AND NOT convalidated"
				).fetchone()[0]), 0)
			else:
				self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
		finally:
			connection.close()

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
		connection = connect_database(self.database_path)
		try:
			audit = connection.execute(
				"""SELECT actor_type,actor_id,payload_json FROM audit_log
				   WHERE action_type='operator.discord_guild_permissions_refreshed'
				   ORDER BY id DESC LIMIT 1"""
			).fetchone()
			self.assertIsNotNone(audit)
			self.assertEqual((audit[0], int(audit[1])), ("operator", 1))
			self.assertEqual(json.loads(audit[2])["guild_permissions"], {"guild-1": 8})
		finally:
			connection.close()

	def test_overview_aggregates_are_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="Overview Organization", slug="overview-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Overview Workspace", slug="overview-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Overview Community", slug="overview-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				for tenant_id, label, platform in (
					(1, "tenant-a-overview", "discord"),
					(community_id, "tenant-b-overview", "twitch"),
				):
					user_id = int(connection.execute(
						"INSERT INTO users(primary_display_name) VALUES (?)", (label,)
					).lastrowid)
					account_id = int(connection.execute(
						"""INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
						   VALUES (?,?,?,?)""",
						(platform, label, label, user_id),
					).lastrowid)
					message_id = int(connection.execute(
						"""INSERT INTO messages(
						       platform,platform_message_id,platform_account_id,user_id,
						       community_id,channel_id,content_raw,content_normalized,sent_at
						   ) VALUES (?,?,?,?,?,?,?,'overview',?)""",
						(
							platform, f"{label}-message", account_id, user_id, tenant_id,
							f"{label}-channel", "overview", "2026-08-26T12:00:00+00:00",
						),
					).lastrowid)
					connection.execute(
						"""INSERT INTO review_queue(message_id,severity,queue_reason_code)
						   VALUES (?,'medium','overview_test')""",
						(message_id,),
					)
					connection.execute(
						"""INSERT INTO moderation_actions(
						       community_id,platform,target_platform_account_id,
						       action_type,actor_type,status
						   ) VALUES (?,?,?,'timeout','operator','pending')""",
						(tenant_id, platform, account_id),
					)
					connection.execute(
						"""INSERT INTO derived_signals(
						       user_id,signal_key,analyzer_version,value_real,confidence,evidence_count
						   ) VALUES (?,'overview_test',1,1.0,1.0,1)""",
						(user_id,),
					)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/api/overview?community_id=1",
			headers={"Cookie": switched_cookie},
		)) as response:
			overview = json.loads(response.read().decode("utf-8"))["overview"]

		self.assertEqual(overview["messages_total"], 1)
		self.assertEqual(overview["open_reviews"], 1)
		self.assertEqual(overview["pending_actions"], 1)
		self.assertEqual(overview["derived_signals"], 1)
		self.assertEqual(overview["top_channels"], [["tenant-b-overview-channel", 1]])
		self.assertEqual(overview["top_platforms"], [["twitch", 1]])

	def test_observation_search_and_pivots_are_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="Search Organization", slug="search-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Search Workspace", slug="search-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Search Community", slug="search-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				observation_ids = {}
				for tenant_id, label in (
					(1, "tenant-a-private-observation"),
					(community_id, "tenant-b-visible-observation"),
				):
					observation_ids[label] = int(connection.execute(
						"""INSERT INTO observations(
						       platform,community_id,event_type,external_event_id,
						       context_id,text_raw,occurred_at
						   ) VALUES ('discord',?,'message.created',?,'shared-context',?,?)""",
						(tenant_id, label, label, "2026-08-26T12:00:00+00:00"),
					).lastrowid)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		for path in ("/search?community_id=1", "/api/search?community_id=1"):
			with self.opener.open(Request(
				f"{self.base_url}{path}", headers={"Cookie": switched_cookie},
			)) as response:
				body = response.read().decode("utf-8")
			self.assertIn("tenant-b-visible-observation", body)
			self.assertNotIn("tenant-a-private-observation", body)

		with self.opener.open(Request(
			f"{self.base_url}/search/export.csv?community_id=1",
			headers={"Cookie": switched_cookie},
		)) as response:
			export_body = response.read().decode("utf-8")
		self.assertIn("tenant-b-visible-observation", export_body)
		self.assertNotIn("tenant-a-private-observation", export_body)

		with self.assertRaises(HTTPError) as hidden_pivot_response:
			self.opener.open(Request(
				f"{self.base_url}/api/observations/{observation_ids['tenant-a-private-observation']}/pivots",
				headers={"Cookie": switched_cookie},
			))
		self.assertEqual(hidden_pivot_response.exception.code, 404)
		hidden_pivot_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/api/observations/{observation_ids['tenant-b-visible-observation']}/pivots",
			headers={"Cookie": switched_cookie},
		)) as response:
			pivots = json.loads(response.read().decode("utf-8"))
		self.assertEqual(pivots["related_observation_count"], 0)

	def test_intelligence_lists_cases_and_reports_are_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="Intel Organization", slug="intel-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Intel Workspace", slug="intel-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Intel Community", slug="intel-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				connection.execute(
					"""INSERT INTO intelligence_alerts(
					       community_id,alert_type,severity,title,summary,confidence,dedupe_key
					   ) VALUES (1,'test','high','tenant-a-private-alert','private',0.9,'tenant-a-private-alert')"""
				)
				connection.execute(
					"""INSERT INTO intelligence_alerts(
					       community_id,alert_type,severity,title,summary,confidence,dedupe_key
					   ) VALUES (?,'test','low','tenant-b-visible-alert','visible',0.5,'tenant-b-visible-alert')""",
					(community_id,),
				)
				case_id = int(connection.execute(
					"""INSERT INTO investigation_cases(community_id,title,summary,priority)
					   VALUES (1,'tenant-a-private-case','private','high')"""
				).lastrowid)
				report_id = int(connection.execute(
					"""INSERT INTO intelligence_reports(
					       community_id,report_type,title,summary,content_json
					   ) VALUES (1,'daily_summary','tenant-a-private-report','private','{}')"""
				).lastrowid)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		for path in ("/intelligence", "/api/intelligence"):
			with self.opener.open(Request(
				f"{self.base_url}{path}", headers={"Cookie": switched_cookie},
			)) as response:
				body = response.read().decode("utf-8")
			self.assertIn("tenant-b-visible-alert", body)
			self.assertNotIn("tenant-a-private-alert", body)
			self.assertNotIn("tenant-a-private-case", body)
			self.assertNotIn("tenant-a-private-report", body)

		for path in (
			f"/intelligence/cases/{case_id}",
			f"/api/intelligence/cases/{case_id}/export",
			f"/api/intelligence/reports/{report_id}",
		):
			with self.assertRaises(HTTPError) as hidden_response:
				self.opener.open(Request(
					f"{self.base_url}{path}", headers={"Cookie": switched_cookie},
				))
			self.assertEqual(hidden_response.exception.code, 404)
			hidden_response.exception.close()

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
		self.assertEqual(
			{item["signal_key"] for item in user_api["signals"]},
			{
				"activity.message_count",
				"behavior.positive_message_ratio",
				"behavior.negative_message_ratio",
				"moderation.finding_rate",
				"moderation.severity_index",
				"risk.composite",
			},
		)
		self.assertEqual(user_api["signals"][0]["signal_key"], "risk.composite")

	def test_signal_api_is_isolated_to_active_community(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connection = connect_database(self.database_path)
		try:
			organization_id = create_organization(
				connection, name="Signal API Organization", slug="signal-api-organization"
			)
			workspace_id = create_workspace(
				connection, organization_id=organization_id,
				name="Signal API Workspace", slug="signal-api-workspace",
			)
			community_id = create_community(
				connection, workspace_id=workspace_id,
				name="Signal API Community", slug="signal-api-community",
			)
			grant_operator_role(
				connection, operator_id=1, community_id=community_id, role="owner"
			)
			with connection:
				for tenant_id, label, value in (
					(1, "tenant-a-private-signal", 91.0),
					(community_id, "tenant-b-visible-signal", 12.0),
				):
					user_id = int(connection.execute(
						"INSERT INTO users(primary_display_name) VALUES (?)", (label,)
					).lastrowid)
					connection.execute(
						"""INSERT INTO community_derived_signal_windows(
						       community_id,user_id,signal_key,window_name,analyzer_version,
						       value_real,confidence,evidence_count,calculated_at
						   ) VALUES (?,?,'risk.composite','24h',?, ?,0.9,20,?)""",
						(
							tenant_id, user_id, SIGNAL_ANALYZER_VERSION, value,
							"2026-08-26T12:00:00+00:00",
						),
					)
		finally:
			connection.close()

		switch_request = Request(
			f"{self.base_url}/community/switch",
			data=urlencode({"community_id": str(community_id)}).encode("utf-8"),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as switch_response:
			self.opener.open(switch_request)
		cookies = switch_response.exception.headers.get_all("Set-Cookie") or []
		switched_cookie = next(item for item in cookies if item.startswith("qbot4k_session=")).split(";", 1)[0]
		switch_response.exception.close()

		with self.opener.open(Request(
			f"{self.base_url}/api/signals?community_id=1",
			headers={"Cookie": switched_cookie},
		)) as response:
			body = response.read().decode("utf-8")
		self.assertIn("tenant-b-visible-signal", body)
		self.assertNotIn("tenant-a-private-signal", body)

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
		connection = connect_database(self.database_path)
		try:
			account = connection.execute(
				"""SELECT id,user_id FROM platform_accounts
				   WHERE platform='discord' AND platform_user_id='user-77'"""
			).fetchone()
			with connection:
				connection.execute(
					"""INSERT INTO observations(
					       platform,community_id,event_type,external_event_id,
					       target_platform_account_id,attributes_json,occurred_at
					   ) VALUES ('discord',1,'member.roles_changed','roles-user-77',?,?,?)""",
					(
						int(account[0]),
						json.dumps({
							"roles": ["role-123"],
							"resolved_role_names": ["Trusted Member"],
						}),
						"2026-08-06T05:11:00Z",
					),
				)
				connection.execute(
					"""INSERT INTO user_notes(user_id,community_id,operator_id,body)
					   VALUES (?,1,1,'Completed orientation review')""",
					(int(account[1]),),
				)
				connection.execute(
					"""INSERT INTO moderation_actions(
					       community_id,platform,target_platform_account_id,user_id,
					       action_type,actor_type,actor_id,reason,status
					   ) VALUES (1,'discord',?,?,'warn','operator',1,'Posting reminder','completed')""",
					(int(account[0]), int(account[1])),
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
		self.assertIn("Member lifecycle", body)
		self.assertIn("Discord roles changed", body)
		self.assertIn("Roles: Trusted Member", body)
		self.assertIn("Completed orientation review", body)
		self.assertIn("Moderation: warn", body)
		self.assertIn("Posting reminder", body)
		with self.opener.open(Request(
			f"{self.base_url}/users/1?lifecycle=roles", headers={"Cookie": cookie_value}
		)) as response:
			filtered_body = response.read().decode("utf-8")
		filtered_lifecycle = filtered_body.split("<h2>Member lifecycle</h2>", 1)[1].split(
			"<h2>Score explanation</h2>", 1
		)[0]
		self.assertIn("Roles: Trusted Member", filtered_lifecycle)
		self.assertNotIn("Completed orientation review", filtered_lifecycle)
		self.assertNotIn("Posting reminder", filtered_lifecycle)
		with self.opener.open(Request(
			f"{self.base_url}/users/1/lifecycle.csv?lifecycle=roles",
			headers={"Cookie": cookie_value},
		)) as response:
			export_body = response.read().decode("utf-8")
			self.assertIn("attachment; filename=\"qbot4k-user-1-lifecycle.csv\"", response.headers["Content-Disposition"])
		self.assertIn("member.roles_changed", export_body)
		self.assertIn("Trusted Member", export_body)
		self.assertNotIn("Operator note", export_body)

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
					"""INSERT INTO review_queue (
					       message_id,status,severity,queue_reason_code,assigned_operator_id,
					       created_at,resolved_at
					   ) VALUES (?, 'open', ?, ?, NULL, CURRENT_TIMESTAMP, NULL)""",
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

	def test_bulk_moderation_dry_run_queues_bounded_partial_outcomes(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, {
			"id": "bulk-message-1", "timestamp": "2026-08-26T12:00:00Z",
			"channel_id": "bulk-channel", "guild_id": "guild-1", "content": "bulk target",
			"author": {"id": "bulk-user-1", "username": "bulk-user", "bot": False},
		})
		connection = connect_database(self.database_path)
		try:
			target_id = int(connection.execute(
				"SELECT platform_account_id FROM messages WHERE platform_message_id='bulk-message-1'"
			).fetchone()[0])
			preview = execute_bulk_moderation(
				connection, tenant=TenantContext(1), actor=ActorAttribution("operator", 1),
				target_platform_account_ids=[target_id, 999999], action_type="timeout",
				reason="Coordinated spam", dry_run=True,
			)
			self.assertEqual([item["status"] for item in preview["results"]], ["eligible", "not_found"])
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM moderation_actions WHERE reason='Coordinated spam'"
			).fetchone()[0], 0)
			result = execute_bulk_moderation(
				connection, tenant=TenantContext(1), actor=ActorAttribution("operator", 1),
				target_platform_account_ids=[target_id, 999999], action_type="timeout",
				reason="Coordinated spam", dry_run=False,
			)
			self.assertEqual([item["status"] for item in result["results"]], ["queued", "not_found"])
			self.assertEqual(connection.execute(
				"SELECT COUNT(*) FROM audit_log WHERE action_type='moderation.bulk_queued'"
			).fetchone()[0], 1)
			with self.assertRaisesRegex(ValueError, "1 to 25"):
				execute_bulk_moderation(
					connection, tenant=TenantContext(1), actor=ActorAttribution("operator", 1),
					target_platform_account_ids=list(range(1, 27)), action_type="warn",
					reason="Too many", dry_run=True,
				)
			with self.assertRaisesRegex(PermissionError, "operator actor"):
				execute_bulk_moderation(
					connection, tenant=TenantContext(1), actor=ActorAttribution("system"),
					target_platform_account_ids=[target_id], action_type="warn",
					reason="Invalid actor", dry_run=True,
				)
		finally:
			connection.close()

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

	def test_admin_can_unlink_platform_account_without_deleting_history(self) -> None:
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(
			connector,
			{
				"id": "discord-msg-unlink-1",
				"timestamp": "2026-08-10T15:00:00Z",
				"channel_id": "channel-unlink",
				"guild_id": "guild-1",
				"content": "preserve this message",
				"author": {
					"id": "user-unlink-1",
					"username": "viewer_unlink",
					"bot": False,
				},
			},
		)

		connection = connect_database(self.database_path)
		try:
			account = connection.execute(
				"SELECT id, user_id FROM platform_accounts WHERE platform_user_id = 'user-unlink-1'"
			).fetchone()
			self.assertIsNotNone(account)
			platform_account_id = int(account[0])
			user_id = int(account[1])
		finally:
			connection.close()

		cookie = self._issue_operator_session_cookie()
		with self.opener.open(Request(f"{self.base_url}/users/{user_id}", headers={"Cookie": cookie})) as response:
			body = response.read().decode("utf-8")
		self.assertIn("Linked accounts", body)
		self.assertIn("/users/unlink", body)
		self.assertIn("viewer_unlink", body)

		invalid_request = Request(
			f"{self.base_url}/users/unlink",
			data=f"user_id={user_id}&platform_account_id={platform_account_id}&confirmation=NO".encode(),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as invalid_response:
			self.opener.open(invalid_request)
		invalid_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			self.assertEqual(
				int(connection.execute("SELECT user_id FROM platform_accounts WHERE id = ?", (platform_account_id,)).fetchone()[0]),
				user_id,
			)
		finally:
			connection.close()

		unlink_request = Request(
			f"{self.base_url}/users/unlink",
			data=f"user_id={user_id}&platform_account_id={platform_account_id}&confirmation=UNLINK".encode(),
			headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
			method="POST",
		)
		with self.assertRaises(HTTPError) as unlink_response:
			self.opener.open(unlink_request)
		self.assertEqual(unlink_response.exception.code, 302)
		self.assertIn("account_status=", unlink_response.exception.headers["Location"])
		unlink_response.exception.close()

		connection = connect_database(self.database_path)
		try:
			owner = connection.execute(
				"SELECT user_id, detached_from_user_id FROM platform_accounts WHERE id = ?",
				(platform_account_id,),
			).fetchone()
			message_owner = connection.execute(
				"SELECT user_id FROM messages WHERE platform_account_id = ?",
				(platform_account_id,),
			).fetchone()
			message_count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
			audit_count = int(
				connection.execute(
					"SELECT COUNT(*) FROM audit_log WHERE action_type = 'user_account_unlink' AND entity_id = ?",
					(platform_account_id,),
				).fetchone()[0]
			)
			user_count = int(connection.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0])
		finally:
			connection.close()

		self.assertIsNone(owner[0])
		self.assertEqual(int(owner[1]), user_id)
		self.assertEqual(int(message_owner[0]), user_id)
		self.assertEqual(message_count, 1)
		self.assertEqual(audit_count, 1)
		self.assertEqual(user_count, 1)

		with self.opener.open(Request(f"{self.base_url}/users/{user_id}", headers={"Cookie": cookie})) as response:
			user_body = response.read().decode("utf-8")
		self.assertIn("preserve this message", user_body)
		self.assertIn("No linked accounts", user_body)

		with self.opener.open(Request(f"{self.base_url}/users", headers={"Cookie": cookie})) as response:
			users_body = response.read().decode("utf-8")
		self.assertNotIn("viewer_unlink (unlinked)", users_body)

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
				target_account_id = int(connection.execute(
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
				).lastrowid)
				other_account_id = int(connection.execute(
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
				).lastrowid)
				for account_id, message_id in (
					(target_account_id, "legacy-target-message"),
					(other_account_id, "legacy-other-message"),
				):
					connection.execute(
						"""INSERT INTO messages(
						       platform,platform_message_id,platform_account_id,community_id,
						       channel_id,content_raw,content_normalized,sent_at
						   ) VALUES ('discord',?,?,1,'legacy','legacy','legacy',?)""",
						(message_id, account_id, "2026-08-26T12:00:00+00:00"),
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

	def test_analyst_mvp_workflows_export_and_readiness_end_to_end(self) -> None:
		cookie = self._issue_operator_session_cookie()
		connector = DiscordConnector(self.database_path)
		ingest_and_analyze(connector, {
			"id": "analyst-mvp-review-1",
			"timestamp": "2026-08-11T12:00:00Z",
			"channel_id": "analyst-ops",
			"guild_id": "guild-1",
			"content": "review this analyst evidence",
			"author": {"id": "analyst-subject", "username": "subject", "bot": False},
		})
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				message = connection.execute(
					"SELECT id,observation_id FROM messages WHERE platform_message_id='analyst-mvp-review-1'"
				).fetchone()
				review_id = int(connection.execute(
					"INSERT INTO review_queue(message_id,severity,queue_reason_code) VALUES (?,'high','manual_review')",
					(int(message[0]),),
				).lastrowid)
				case_id = int(connection.execute(
					"INSERT INTO investigation_cases(community_id,title,summary,priority,status) VALUES (1,'Analyst case','Evidence package','high','active')"
				).lastrowid)
				connection.execute(
					"INSERT INTO case_evidence(case_id,observation_id,note) VALUES (?,?,?)",
					(case_id, int(message[1]), "source evidence"),
				)
		finally:
			connection.close()

		resolution = Request(
			f"{self.base_url}/api/moderation/reviews/{review_id}/resolve",
			data=json.dumps({"resolution": "confirmed", "action_type": "warn", "note": "validated"}).encode(),
			headers={"Cookie": cookie, "Content-Type": "application/json"},
			method="POST",
		)
		with self.opener.open(resolution) as response:
			resolved = json.loads(response.read().decode())
		self.assertEqual(resolved["status"], "resolved")
		self.assertIsNotNone(resolved["action_id"])

		with self.opener.open(Request(
			f"{self.base_url}/api/intelligence/cases/{case_id}/export",
			headers={"Cookie": cookie},
		)) as response:
			case_export = json.loads(response.read().decode())
			self.assertIn(f"qbot4k-case-{case_id}.json", response.headers["Content-Disposition"])
		self.assertEqual(case_export["case"]["title"], "Analyst case")
		self.assertEqual(case_export["evidence"][0]["note"], "source evidence")

		with self.opener.open(Request(
			f"{self.base_url}/search/export.csv?q=analyst",
			headers={"Cookie": cookie},
		)) as response:
			csv_export = response.read().decode()
			self.assertEqual(response.headers.get_content_type(), "text/csv")
		self.assertIn("external_event_id", csv_export.splitlines()[0])
		self.assertIn("analyst-mvp-review-1", csv_export)

		with self.opener.open(Request(f"{self.base_url}/api/health", headers={"Cookie": cookie})) as response:
			health = json.loads(response.read().decode())
		self.assertEqual(health["database"]["integrity"], "ok")
		self.assertIn("failed_actions_24h", health["operations"]["counters"])

		with self.opener.open(Request(f"{self.base_url}/api/audit", headers={"Cookie": cookie})) as response:
			audit = json.loads(response.read().decode())
		actions = {item["action_type"] for item in audit["items"]}
		self.assertTrue({"auth.login", "moderation.review_resolved", "case.exported", "search.exported"} <= actions)
