from __future__ import annotations

import logging
import csv
import io
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Mapping
from urllib.parse import parse_qs, quote, urlencode, urlparse
from statistics import NormalDist

from ..config import AppSettings
from ..contexts import ActorAttribution, TenantContext
from ..db import (
    connect_database,
	collect_observation,
	enqueue_processing_job,
	database_health,
	operational_readiness_snapshot,
	delete_simple_command_definition,
	initialize_database,
	list_service_reliability_buckets,
	record_moderation_action,
	reset_database,
    upsert_operator_account,
	list_command_definitions,
	list_simple_command_definitions,
	upsert_command_definition,
	upsert_simple_command_definition,
)
from ..models import Observation, coerce_timestamp
from ..intelligence.userprofiles import (
    add_user_note,
	create_canonical_user,
    get_canonical_user_profile,
    link_platform_account,
    unlink_platform_account,
)
from ..intelligence.signals import (
    SIGNAL_LABELS,
    list_signal_overview,
    list_user_derived_signals,
)
from ..intelligence.scoring import get_current_social_score
from ..intelligence.workflows import (
	add_case_entity,
	add_case_evidence,
	add_case_note,
	create_case_from_alert,
	dispose_alert,
	generate_intelligence_report,
	intelligence_summary,
	update_alert_workflow,
	update_case,
)
from ..intelligence.search import list_saved_queries, observation_pivots, save_query, search_observations
from ..intelligence.analytics import analytics_snapshot, review_identity_suggestion
from ..intelligence.announcements import (
	approve_announcement,
	cancel_announcement,
	create_announcement,
	preview_announcement,
	retry_announcement,
)
from ..intelligence.onboarding import (
	configure_welcome,
	delete_onboarding_resource,
	list_onboarding_resources,
	save_onboarding_resource,
	verify_onboarding_member,
)
from ..intelligence.events import SUPPORTED_EVENT_TYPES, collect_external_feed_item
from ..intelligence.community import (
	accept_operator_invitations,
	complete_discord_install_intent,
	complete_twitch_install_intent,
	configure_community_profile,
	create_discord_install_intent,
	create_twitch_install_intent,
	discord_install_intent_is_pending,
	emergency_remove_operator_access,
	invite_operator,
	list_operator_communities,
	operator_has_permission,
	record_operator_discord_guild_permissions,
	revoke_installation,
	revoke_operator_invitation,
	resolve_member_queue_item,
	resolve_community_id,
	resolve_tenant_context,
	transfer_community_ownership,
	twitch_install_intent_is_pending,
	update_installation_health,
)
from ..intelligence.abuse import configure_anti_abuse_policy
from ..intelligence.governance import configure_retention_policy
from ..intelligence.slo import collect_tenant_slo_snapshot
from ..intelligence.quotas import TenantQuotaExceededError, consume_tenant_quota
from ..intelligence.liveops import live_operations_snapshot
from ..intelligence.professional_ops import (
    activate_playbook,
    assign_incident,
    conversation_context,
    create_notification_destination,
    escalate_incident,
    generate_post_stream_briefing,
    handoff_shift,
	list_moderation_shift_schedule,
    queue_incident_notifications,
	route_incident_to_on_call,
	schedule_moderation_shift,
)
from ..twitch_control import TwitchControlPlane
from ..twitch_auth import TwitchTokenManager
from ..twitch_auth import exchange_twitch_code_for_tokens, TwitchAuthError
from ..token_store import persist_refreshed_twitch_tokens
from ..surface_policy import DASHBOARD_SURFACE_POLICIES
from ..twitch_eventsub import (
	TwitchEventSubControlPlane,
    mark_subscription_event,
    observation_from_eventsub,
    record_subscription,
    verify_eventsub_signature,
)
from ..jobs import send_manual_twitch_live_announcements
from .auth import (
    DashboardSession,
	build_discord_install_url,
	build_twitch_oauth_url,
    build_discord_oauth_url,
    build_oauth_state,
    build_session,
	create_discord_install_state,
	create_twitch_install_state,
    create_session_cookie,
    determine_operator_role,
    exchange_discord_code_for_token,
    fetch_discord_identity,
	parse_discord_install_state,
	parse_twitch_install_state,
    parse_session_cookie,
)
from .moderation import (
	add_moderation_rule_exemption,
	assign_moderation_work,
	create_moderation_rule_draft,
	execute_bulk_moderation,
	list_member_queue,
	list_moderation_filters,
	list_moderation_rules,
	list_moderation_work,
	list_open_reviews,
	list_recent_actions,
	preview_moderation_rule_version,
	publish_moderation_rule_version,
	resolve_review,
	rollback_moderation_rule,
	save_moderation_filter,
	save_moderation_rule,
)
from .overview import load_overview_snapshot
from .users import (
	get_user_moderation_status,
	list_recent_user_messages,
	list_recent_user_moderation_actions,
	list_user_lifecycle_events,
	list_user_platform_accounts,
	search_users,
	user_is_visible,
)


def _restart_systemd_service(service_name: str) -> None:
	command = ["systemctl", "restart", service_name]
	result = subprocess.run(
		command,
		check=False,
		capture_output=True,
		text=True,
		timeout=30,
	)
	if result.returncode != 0:
		detail = (result.stderr or result.stdout or "systemctl restart failed").strip()
		raise RuntimeError(detail[:500])


def _optional_string(value: object) -> str | None:
	return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
	return int(value) if value not in (None, "") else None


@dataclass(frozen=True)
class DashboardResponse:
	status: HTTPStatus
	body: bytes
	content_type: str
	headers: Mapping[str, str]


class DashboardApp:
	def __init__(
		self,
		settings: AppSettings,
		service_states: Mapping[str, str] | None = None,
		*,
		service_started_at: Mapping[str, str] | None = None,
		app_started_at: str | None = None,
	) -> None:
		self.settings = settings
		self.service_states = service_states if service_states is not None else {}
		self.service_started_at = dict(service_started_at or {})
		self.app_started_at = app_started_at

	def dispatch(self, handler: BaseHTTPRequestHandler) -> bool:
		parsed = urlparse(handler.path)
		path = parsed.path
		if handler.command == "POST" and path == "/webhooks/twitch/eventsub":
			self._serve_twitch_eventsub(handler)
			return True
		if handler.command in {"POST", "PUT", "PATCH", "DELETE"} and not self._valid_request_origin(handler):
			self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "origin_mismatch"})
			return True

		if handler.command == "GET" and path == "/":
			self._serve_public_home(handler)
			return True
		if handler.command == "GET" and path == "/dashboard":
			self._serve_dashboard(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/system-health":
			self._serve_system_health(handler)
			return True
		if handler.command == "GET" and path == "/live-ops":
			self._serve_live_ops(handler)
			return True
		if handler.command == "POST" and path == "/dashboard/go-live":
			self._serve_dashboard_go_live(handler)
			return True
		if handler.command == "POST" and path == "/dashboard/restart":
			self._serve_dashboard_restart(handler)
			return True
		if handler.command == "POST" and path == "/dashboard/reset-database":
			self._serve_dashboard_reset_database(handler)
			return True
		if handler.command == "POST" and path == "/community/switch":
			self._serve_community_switch(handler)
			return True
		if handler.command == "GET" and path == "/users":
			self._serve_users(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/signals":
			self._serve_signals(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/intelligence":
			self._serve_intelligence(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/search":
			self._serve_search(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/search/export.csv":
			self._serve_search_export(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/search/saved":
			self._serve_search_save(handler)
			return True
		if handler.command == "GET" and path == "/analytics":
			self._serve_analytics(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/analytics/export.json":
			self._serve_analytics_export(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path.startswith("/intelligence/cases/"):
			self._serve_intelligence_case(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/intelligence/cases/") and path.endswith("/action"):
			self._serve_intelligence_case_action(handler, path)
			return True
		if handler.command == "GET" and path == "/audit":
			self._serve_audit(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path.startswith("/intelligence/alerts/") and path.endswith("/case"):
			self._serve_intelligence_alert_case(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/intelligence/alerts/") and path.endswith("/disposition"):
			self._serve_intelligence_alert_disposition(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/intelligence/alerts/") and path.endswith("/workflow"):
			self._serve_intelligence_alert_workflow(handler, path)
			return True
		if handler.command == "POST" and path == "/intelligence/reports/generate":
			self._serve_intelligence_report_generate(handler)
			return True
		if handler.command == "POST" and path == "/users/link":
			self._serve_users_link(handler)
			return True
		if handler.command == "POST" and path == "/users/unlink":
			self._serve_users_unlink(handler)
			return True
		if handler.command == "POST" and path.startswith(
		    "/users/") and path.endswith("/moderation"):
			self._serve_user_moderation_action(handler, path)
			return True
		if handler.command == "GET" and path.startswith("/users/") and path.endswith("/lifecycle.csv"):
			self._serve_user_lifecycle_export(handler, path, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path.startswith("/users/"):
			self._serve_user_messages(handler, path, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/moderation":
			self._serve_moderation(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/moderation/filters":
			self._serve_moderation_filter_save(handler)
			return True
		if handler.command == "POST" and path.startswith("/moderation/work/") and path.endswith("/assign"):
			self._serve_moderation_work_assign(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/moderation/reviews/") and path.endswith("/resolve"):
			self._serve_moderation_review_resolve(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/moderation/reports/") and path.endswith("/resolve"):
			self._serve_member_queue_resolve(handler, path, "report")
			return True
		if handler.command == "POST" and path.startswith("/moderation/appeals/") and path.endswith("/resolve"):
			self._serve_member_queue_resolve(handler, path, "appeal")
			return True
		if handler.command == "POST" and path == "/moderation/rules":
			self._serve_moderation_rule_save(handler)
			return True
		if handler.command == "POST" and path == "/moderation/rules/drafts":
			self._serve_moderation_rule_draft(handler)
			return True
		if handler.command == "POST" and path.startswith("/moderation/rule-versions/") and path.endswith("/preview"):
			self._serve_moderation_rule_preview(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/moderation/rule-versions/") and path.endswith("/publish"):
			self._serve_moderation_rule_publish(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/moderation/rule-versions/") and path.endswith("/rollback"):
			self._serve_moderation_rule_rollback(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/moderation/rules/") and path.endswith("/exemptions"):
			self._serve_moderation_rule_exemption(handler, path)
			return True
		if handler.command == "GET" and path == "/commands":
			self._serve_commands(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/integrations":
			self._serve_integrations(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/settings":
			self._serve_settings(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/settings":
			self._serve_settings_update(handler)
			return True
		if handler.command == "POST" and path == "/settings/operators/invite":
			self._serve_settings_operator_invite(handler)
			return True
		if handler.command == "GET" and path == "/announcements":
			self._serve_announcements(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/onboarding":
			self._serve_onboarding(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/onboarding":
			self._serve_onboarding_update(handler)
			return True
		if handler.command == "POST" and path == "/onboarding/verify":
			self._serve_onboarding_verify(handler)
			return True
		if handler.command == "POST" and path == "/onboarding/resources":
			self._serve_onboarding_resource_save(handler)
			return True
		if handler.command == "POST" and path.startswith("/onboarding/resources/") and path.endswith("/delete"):
			self._serve_onboarding_resource_delete(handler, path)
			return True
		if handler.command == "POST" and path == "/announcements":
			self._serve_announcement_create(handler)
			return True
		if handler.command == "POST" and path.startswith("/announcements/") and path.endswith("/approve"):
			self._serve_announcement_approve(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/announcements/") and path.endswith("/cancel"):
			self._serve_announcement_cancel(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/announcements/") and path.endswith("/retry"):
			self._serve_announcement_retry(handler, path)
			return True
		if handler.command == "POST" and path == "/integrations/discord/link":
			self._serve_discord_link(handler)
			return True
		if handler.command == "GET" and path == "/integrations/discord/callback":
			self._serve_discord_install_callback(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/integrations/twitch/link":
			self._serve_twitch_link(handler)
			return True
		if handler.command == "GET" and path == "/integrations/twitch/callback":
			self._serve_twitch_install_callback(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/commands":
			self._serve_commands_update(handler)
			return True
		if handler.command == "GET" and path == "/login":
			self._serve_login(handler)
			return True
		if handler.command == "GET" and path in {
		    "/auth/discord/callback", "/oauth/discord/callback"}:
			self._serve_oauth_callback(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/logout":
			self._serve_logout(handler)
			return True
		if handler.command == "GET" and path == "/api/overview":
			self._serve_api_overview(handler)
			return True
		if handler.command == "GET" and path == "/api/live-ops":
			self._serve_api_live_ops(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/api/live-ops/stream":
			self._serve_live_ops_stream(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path.startswith("/api/observations/") and path.endswith("/context"):
			self._serve_api_conversation_context(handler, path)
			return True
		if handler.command == "POST" and path == "/api/live-ops/moderate":
			self._serve_live_ops_moderate(handler)
			return True
		if handler.command == "POST" and path.startswith("/api/live-ops/incidents/"):
			self._serve_live_ops_incident_action(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/api/live-ops/playbooks/") and path.endswith("/activate"):
			self._serve_live_ops_playbook(handler, path)
			return True
		if handler.command == "POST" and path == "/api/live-ops/shifts/handoff":
			self._serve_live_ops_handoff(handler)
			return True
		if handler.command == "GET" and path == "/api/live-ops/shifts":
			self._serve_live_ops_shift_schedule(handler)
			return True
		if handler.command == "POST" and path == "/api/live-ops/shifts":
			self._serve_live_ops_shift_schedule(handler)
			return True
		if handler.command == "POST" and path == "/api/live-ops/twitch/shield-mode":
			self._serve_live_ops_shield_mode(handler)
			return True
		if handler.command == "POST" and path == "/api/live-ops/twitch/chat-settings":
			self._serve_live_ops_chat_settings(handler)
			return True
		if handler.command == "POST" and path == "/api/live-ops/notifications":
			self._serve_live_ops_notification_destination(handler)
			return True
		if handler.command == "GET" and path == "/api/users":
			self._serve_api_users(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/api/signals":
			self._serve_api_signals(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/api/intelligence":
			self._serve_api_intelligence(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/api/search":
			self._serve_api_search(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/api/search/saved":
			self._serve_api_save_query(handler)
			return True
		if handler.command == "GET" and path.startswith("/api/observations/") and path.endswith("/pivots"):
			self._serve_api_observation_pivots(handler, path)
			return True
		if handler.command == "GET" and path == "/api/analytics":
			self._serve_api_analytics(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path.startswith("/api/identity-suggestions/"):
			self._serve_api_identity_review(handler, path)
			return True
		if handler.command == "POST" and path == "/api/operators/invitations":
			self._serve_operator_invitation(handler)
			return True
		if handler.command == "POST" and path.startswith("/api/operators/"):
			self._serve_operator_access_action(handler, path)
			return True
		if handler.command == "POST" and path == "/api/moderation/bulk":
			self._serve_api_bulk_moderation(handler)
			return True
		if handler.command == "POST" and path.startswith("/api/integrations/") and path.endswith("/revoke"):
			self._serve_api_integration_revoke(handler, path)
			return True
		if handler.command == "POST" and path == "/api/external/observations":
			self._serve_api_external_observation(handler)
			return True
		if handler.command == "POST" and path == "/api/events":
			self._serve_api_event(handler)
			return True
		if handler.command == "GET" and path.startswith("/api/intelligence/reports/"):
			self._serve_api_intelligence_report(handler, path)
			return True
		if handler.command == "GET" and path.startswith("/api/intelligence/cases/") and path.endswith("/export"):
			self._serve_api_case_export(handler, path)
			return True
		if handler.command in {"GET", "POST"} and path.startswith("/api/intelligence/cases/"):
			self._serve_api_case(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/api/intelligence/alerts/"):
			self._serve_api_alert_workflow(handler, path)
			return True
		if handler.command == "GET" and path.startswith("/api/users/"):
			self._serve_api_user_detail(handler, path)
			return True
		if handler.command == "POST" and path == "/api/users/link":
			self._serve_api_link_user(handler)
			return True
		if handler.command == "POST" and path.startswith(
		    "/api/users/") and path.endswith("/notes"):
			self._serve_api_add_note(handler, path)
			return True
		if handler.command == "GET" and path == "/api/moderation/actions":
			self._serve_api_actions(handler)
			return True
		if handler.command == "GET" and path == "/api/slo":
			self._serve_api_slo(handler)
			return True
		if handler.command == "GET" and path == "/api/moderation/reviews":
			self._serve_api_reviews(handler)
			return True
		if handler.command == "POST" and path.startswith("/api/moderation/reviews/") and path.endswith("/resolve"):
			self._serve_api_review_resolve(handler, path)
			return True
		if handler.command == "GET" and path == "/api/moderation/rules":
			self._serve_api_rules(handler)
			return True
		if handler.command == "POST" and path == "/api/moderation/rules":
			self._serve_api_rule_save(handler)
			return True
		if handler.command == "GET" and path == "/api/health":
			self._serve_api_health(handler)
			return True
		if handler.command == "GET" and path == "/api/audit":
			self._serve_api_audit(handler, parse_qs(parsed.query))
			return True
		return False

	def _serve_api_live_ops(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None or session.community_id is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			payload = live_operations_snapshot(connection, community_id=session.community_id)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_live_ops_stream(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None or session.community_id is None:
			return
		community_id = session.community_id
		handler.send_response(HTTPStatus.OK)
		handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
		handler.send_header("Cache-Control", "no-cache, no-transform")
		handler.send_header("Connection", "keep-alive")
		handler.send_header("X-Accel-Buffering", "no")
		self._send_security_headers(handler)
		handler.end_headers()
		last_watermark = ""
		try:
			for tick in range(30):
				connection = connect_database(self.settings.database_path)
				try:
					initialize_database(connection)
					payload = live_operations_snapshot(connection, community_id=community_id)
				finally:
					connection.close()
				watermark = str(payload.get("watermark") or "")
				if tick == 0 or watermark != last_watermark:
					wire = json.dumps(payload, sort_keys=True, separators=(",", ":"))
					handler.wfile.write(f"event: snapshot\nid: {watermark}\ndata: {wire}\n\n".encode("utf-8"))
					last_watermark = watermark
				else:
					handler.wfile.write(b": heartbeat\n\n")
				handler.wfile.flush()
				time.sleep(1)
		except (BrokenPipeError, ConnectionResetError, OSError):
			return

	def _serve_api_conversation_context(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None or session.community_id is None:
			return
		parts = [part for part in path.split("/") if part]
		try:
			observation_id = int(parts[2])
		except (IndexError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_observation_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			observation = connection.execute(
				"SELECT 1 FROM observations WHERE id=? AND community_id=?",
				(observation_id, session.community_id),
			).fetchone()
			if observation is None:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "observation_not_found"})
				return
			try:
				payload = conversation_context(connection, observation_id)
			except ValueError as exc:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_live_ops_moderate(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			message_id = int(payload.get("message_id") or 0)
			duration_seconds = max(1, min(int(payload.get("duration_seconds") or 600), 2_419_200))
		except (TypeError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_action_parameters"})
			return
		action_type = str(payload.get("action_type") or "").strip().casefold()
		if action_type not in {"warn", "timeout", "ban"}:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_action_type"})
			return
		if action_type == "ban" and not hmac.compare_digest(
			str(payload.get("confirmation") or ""), "PERMANENT BAN"
		):
			self._send_json(handler, HTTPStatus.CONFLICT, {"error": "permanent_ban_confirmation_required"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			row = connection.execute(
				"""SELECT id,platform,observation_id,platform_account_id FROM messages
				   WHERE id=? AND community_id=?""", (message_id, session.community_id),
			).fetchone()
			if row is None:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "message_not_found"})
				return
			action_id = record_moderation_action(
				connection, platform=str(row[1]), message_id=int(row[0]),
				target_platform_account_id=int(row[3]), action_type=action_type,
				reason=str(payload.get("reason") or "Live operations keyboard action")[:500],
				status="pending", actor_type="operator", actor_id=int(session.user_id),
				community_id=session.community_id,
			)
			connection.execute(
				"UPDATE moderation_actions SET duration_seconds=?,assigned_operator_id=? WHERE id=?",
				(duration_seconds, int(session.user_id), action_id),
			)
			enqueue_processing_job(
				connection, stage="action", job_type=f"{row[1]}.moderation.execute",
				observation_id=int(row[2]) if row[2] is not None else None,
				payload={"message_id": int(row[0])},
				idempotency_key=f"liveops:{action_id}:execute", priority=5,
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.ACCEPTED, {
			"action_id": action_id, "status": "pending_provider_confirmation",
		})

	def _serve_live_ops_incident_action(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		parts = [part for part in path.split("/") if part]
		try:
			incident_id = int(parts[3])
			action = parts[4]
		except (IndexError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_incident_action"})
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			incident = connection.execute(
				"SELECT 1 FROM operations_incidents WHERE id=? AND community_id=?",
				(incident_id, session.community_id),
			).fetchone()
			if incident is None:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "incident_not_found"})
				return
			if action == "assign":
				operator_id = int(payload.get("operator_id") or session.user_id)
				assign_incident(connection, incident_id=incident_id, operator_id=operator_id,
				                assigned_by=int(session.user_id))
				result: dict[str, object] = {"incident_id": incident_id, "assigned_operator_id": operator_id}
			elif action == "escalate":
				level = escalate_incident(connection, incident_id=incident_id,
				                          operator_id=int(session.user_id), note=str(payload.get("note") or ""))
				result = {"incident_id": incident_id, "escalation_level": level}
			elif action == "route-on-call":
				operator_id = route_incident_to_on_call(
					connection, community_id=int(session.community_id), incident_id=incident_id,
					routed_by_operator_id=int(session.user_id),
				)
				result = {"incident_id": incident_id, "assigned_operator_id": operator_id}
			else:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "unsupported_incident_action"})
				return
		except (TypeError, ValueError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, result)

	def _serve_live_ops_handoff(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			incoming = int(payload.get("incoming_operator_id") or 0)
		except (TypeError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_handoff"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				shift_id = handoff_shift(connection, tenant=TenantContext(session.community_id),
					actor=ActorAttribution("operator", int(session.user_id)), incoming_operator_id=incoming,
					note=str(payload.get("note") or ""))
			except (PermissionError, ValueError) as exc:
				self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"shift_id": shift_id, "status": "handed_off"})

	def _serve_live_ops_shift_schedule(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if handler.command == "POST":
				payload = self._read_json_body(handler)
				if payload is None:
					return
				try:
					schedule_moderation_shift(
						connection, tenant=TenantContext(int(session.community_id)),
						actor=ActorAttribution("operator", int(session.user_id)),
						operator_id=int(payload.get("operator_id") or 0),
						starts_at=str(payload.get("starts_at") or ""),
						ends_at=str(payload.get("ends_at") or ""),
					)
				except (PermissionError, TypeError, ValueError) as exc:
					self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
					return
			rows = [dict(row) for row in list_moderation_shift_schedule(
				connection, community_id=int(session.community_id)
			)]
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"shifts": rows})

	def _twitch_control_plane(self) -> TwitchControlPlane:
		if not self.settings.twitch_bot_token:
			raise ValueError("Twitch authorization is not configured")
		return TwitchControlPlane(TwitchTokenManager(
			initial_access_token=self.settings.twitch_bot_token,
			refresh_token=self.settings.twitch_refresh_token,
			client_id=self.settings.twitch_client_id,
			client_secret=self.settings.twitch_client_secret,
			on_token_refresh=persist_refreshed_twitch_tokens,
		))

	def _live_ops_broadcaster(self, payload: Mapping[str, object]) -> str:
		return str(payload.get("broadcaster") or self.settings.twitch_channels[0]).strip()

	def _serve_live_ops_shield_mode(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				result = self._twitch_control_plane().set_shield_mode(
					connection, community_id=session.community_id,
					broadcaster=self._live_ops_broadcaster(payload), active=bool(payload.get("active")),
					operator_id=int(session.user_id),
				)
			except (PermissionError, RuntimeError, ValueError) as exc:
				self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, result)

	def _serve_live_ops_chat_settings(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		settings = payload.get("settings")
		if not isinstance(settings, Mapping):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "settings_object_required"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				result = self._twitch_control_plane().update_chat_settings(
					connection, community_id=session.community_id,
					broadcaster=self._live_ops_broadcaster(payload), settings=settings,
					operator_id=int(session.user_id),
				)
			except (PermissionError, RuntimeError, ValueError) as exc:
				self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, result)

	def _serve_live_ops_playbook(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		parts = [part for part in path.split("/") if part]
		playbook_key = parts[3] if len(parts) > 4 else ""
		payload = self._read_json_body(handler)
		if payload is None:
			return
		community_id = session.community_id
		incident_id = _optional_int(payload.get("incident_id"))
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				if incident_id is not None:
					incident = connection.execute(
						"SELECT 1 FROM operational_incidents WHERE id=? AND community_id=?",
						(incident_id, community_id),
					).fetchone()
					if incident is None:
						self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "incident_not_found"})
						return
				run = activate_playbook(
					connection, tenant=TenantContext(community_id),
					actor=ActorAttribution("operator", int(session.user_id)),
					playbook_key=playbook_key, incident_id=incident_id,
				)
				completed: list[dict[str, object]] = []
				for step in run["steps"]:
					control = str(step.get("control") or "")
					result: object = "recorded"
					if control == "shield_mode":
						result = self._twitch_control_plane().set_shield_mode(
							connection, community_id=community_id,
							broadcaster=self._live_ops_broadcaster(payload), active=bool(step.get("value")),
							operator_id=int(session.user_id))
					elif control == "chat_settings":
						result = self._twitch_control_plane().update_chat_settings(
							connection, community_id=community_id,
							broadcaster=self._live_ops_broadcaster(payload), settings=step.get("settings") or {},
							operator_id=int(session.user_id))
					elif control == "notify" and incident_id is not None:
						result = {"queued": queue_incident_notifications(connection, incident_id, force=True)}
					elif control == "assign" and incident_id is not None:
						assign_incident(connection, incident_id=incident_id, operator_id=int(session.user_id),
						                assigned_by=int(session.user_id))
					elif control == "briefing":
						session_row = connection.execute(
							"SELECT id FROM stream_sessions WHERE community_id=? ORDER BY started_at DESC LIMIT 1",
							(community_id,),).fetchone()
						if session_row is not None:
							result = {"briefing_id": generate_post_stream_briefing(connection, int(session_row[0]))}
					completed.append({"key": step.get("key"), "result": result})
				connection.execute(
					"""UPDATE raid_playbook_runs SET status='completed',current_step=?,state_json=?,
					   completed_at=CURRENT_TIMESTAMP WHERE id=?""",
					(len(completed), json.dumps({"completed": completed}, sort_keys=True), int(run["run_id"])),
				)
			except (PermissionError, RuntimeError, ValueError, TypeError) as exc:
				self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {**run, "completed": completed})

	def _serve_live_ops_notification_destination(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None or session.community_id is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				destination_id = create_notification_destination(
					connection, community_id=session.community_id,
					destination_type=str(payload.get("destination_type") or ""),
					name=str(payload.get("name") or ""), target=str(payload.get("target") or ""),
					minimum_severity=str(payload.get("minimum_severity") or "high"),
				)
			except (TypeError, ValueError) as exc:
				self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
				return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.CREATED, {"destination_id": destination_id})

	def _serve_live_ops(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		admin_destination = "" if session.role not in {"admin", "owner"} else """
<section class='card'><h2>Escalation destinations</h2>
<form id='destinationForm' class='compact-form'><input name='name' required placeholder='On-call channel'>
<select name='destination_type'><option value='discord_webhook'>Discord webhook</option><option value='slack_webhook'>Slack webhook</option><option value='generic_webhook'>Generic webhook</option></select>
<input name='target' type='url' required placeholder='https://…'><select name='minimum_severity'><option>high</option><option>critical</option><option>medium</option></select><button>Add destination</button></form>
<div id='destinations' class='tag-row'></div></section>"""
		body = self._render_page(
			"Live Ops", session,
			"""<style>
.connection-dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;background:#d97706;margin-right:.4rem}.connection-dot.live{background:#16a34a}
.emergency-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}.emergency{min-height:3.2rem;font-weight:700}.danger{background:#b91c1c;color:#fff}.safe{background:#166534;color:#fff}
.selected-row{outline:2px solid #f0a629;outline-offset:-2px;background:rgba(240,166,41,.1)}.context-list{max-height:28rem;overflow:auto}.context-item{padding:.6rem;border-left:3px solid transparent}.context-item.finding{border-color:#ef4444;background:rgba(239,68,68,.1)}
.two-col{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);gap:1rem}.spark{width:100%;height:130px}.graph{width:100%;height:260px}.tag-row{display:flex;flex-wrap:wrap;gap:.45rem}.tag{padding:.3rem .55rem;border:1px solid #596064;border-radius:2px;background:#191d20}
.compact-form{display:flex;flex-wrap:wrap;gap:.5rem}.compact-form input,.compact-form select{min-width:150px;flex:1}.status-banner{position:sticky;bottom:1rem;padding:.65rem;border-radius:.5rem;background:#111827;color:white;display:none;z-index:4}
@media(max-width:780px){.two-col{grid-template-columns:1fr}.emergency-grid{grid-template-columns:1fr 1fr}.main{padding:.75rem}table{font-size:.82rem}.hide-mobile{display:none}.emergency{min-height:3.8rem}}
</style>
<section class='hero'><p class='eyebrow'>Live operations</p><h1>Community command center</h1><p class='lede'><span id='connectionDot' class='connection-dot'></span><span id='connectionState'>Connecting to live stream…</span> Select a finding with J/K; W warns, T times out, B bans, and C opens conversation context.</p></section>
<section class='card'><h2>Emergency controls</h2><div class='emergency-grid'>
<button class='emergency danger' data-shield='true'>Shield Mode on</button><button class='emergency safe' data-shield='false'>Shield Mode off</button>
<button class='emergency' data-chat='followers'>Followers 10m</button><button class='emergency' data-chat='slow'>Slow mode 10s</button><button class='emergency' data-chat='normal'>Restore chat</button>
</div><h3>Attack playbooks</h3><div id='playbookButtons' class='emergency-grid'></div><p class='muted'>Twitch control results appear only after Twitch confirms the request.</p></section>
<div class='grid'><div class='metric'><div class='label'>Current velocity</div><div id='metricVelocity' class='value'>—</div></div><div class='metric'><div class='label'>Unique chatters</div><div id='metricChatters' class='value'>—</div></div><div class='metric'><div class='label'>Priority findings</div><div id='metricAlerts' class='value'>—</div></div><div class='metric'><div class='label'>Grouped incidents</div><div id='metricIncidents' class='value'>—</div></div><div class='metric'><div class='label'>Pending actions</div><div id='metricPending' class='value'>—</div></div><div class='metric'><div class='label'>Twitch confirmed</div><div id='metricConfirmed' class='value'>—</div></div></div>
<section class='card'><h2>Chat velocity</h2><svg id='velocityGraph' class='spark' viewBox='0 0 800 130' role='img' aria-label='Thirty-minute chat velocity'></svg></section>
<div class='two-col'><section class='card'><h2>Priority findings</h2><table><thead><tr><th>Severity</th><th>Subject</th><th>Finding</th><th class='hide-mobile'>Time</th></tr></thead><tbody id='alertRows'></tbody></table></section>
<section class='card'><h2>Conversation context</h2><div id='contextPanel' class='context-list muted'>Press C on a selected finding.</div></section></div>
<section class='card'><h2>Grouped incidents</h2><table><thead><tr><th>ID</th><th>Severity</th><th>Incident</th><th>Findings</th><th>Assignee</th><th>Escalation</th><th>Actions</th></tr></thead><tbody id='incidentRows'></tbody></table></section>
<div class='two-col'><section class='card'><h2>Stream timeline</h2><div id='timeline'></div></section><section class='card'><h2>Audience cohorts</h2><div id='cohorts' class='grid'></div></section></div>
<div class='two-col'><section class='card'><h2>Raid &amp; shared-audience graph</h2><svg id='audienceGraph' class='graph' viewBox='0 0 600 260' role='img' aria-label='Raid and shared audience graph'></svg></section><section class='card'><h2>Moderator workload &amp; consistency</h2><div id='workload'></div></section></div>
<div class='two-col'><section class='card'><h2>Twitch-confirmed controls</h2><div id='controls'></div></section><section class='card'><h2>Post-stream briefings</h2><div id='briefings'></div></section></div>
""" + admin_destination + """
<div id='statusBanner' class='status-banner'></div>
<script>
(() => {
  let snapshot = null, selected = 0;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const post = async (url, body) => { const response = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const data=await response.json(); if(!response.ok) throw new Error(data.error||'Request failed'); return data; };
  const notice = (message, error=false) => { const node=$('statusBanner'); node.textContent=message; node.style.background=error?'#991b1b':'#111827'; node.style.display='block'; window.setTimeout(()=>node.style.display='none',4500); };
	const drawVelocity = rows => { const svg=$('velocityGraph'), values=rows.map(r=>Number(r.messages||0)), max=Math.max(1,...values); const points=values.map((v,i)=>`${i*800/Math.max(1,values.length-1)},${118-v*105/max}`).join(' '); svg.innerHTML=`<line x1='0' y1='118' x2='800' y2='118' stroke='currentColor' opacity='.25'/><polyline points='${points}' fill='none' stroke='#f0a629' stroke-width='3'/><text x='8' y='18' fill='currentColor'>Peak ${max} msg/min</text>`; };
	const drawAudience = edges => { const svg=$('audienceGraph'); if(!edges.length){svg.innerHTML='<text x="20" y="40" fill="currentColor">No raid or shared-audience events yet.</text>';return;} const names=[...new Set(edges.flatMap(e=>[e.source_key,e.target_key]))].slice(0,12), cx=300,cy=130,r=95, points=new Map(names.map((n,i)=>[n,[cx+Math.cos(i*2*Math.PI/names.length)*r,cy+Math.sin(i*2*Math.PI/names.length)*r]])); let html=''; edges.slice(0,20).forEach(e=>{if(!points.has(e.source_key)||!points.has(e.target_key))return;const a=points.get(e.source_key),b=points.get(e.target_key);html+=`<line x1='${a[0]}' y1='${a[1]}' x2='${b[0]}' y2='${b[1]}' stroke='${e.edge_type==='raid'?'#ef4444':'#f0a629'}' stroke-width='${Math.min(8,1+Number(e.weight||1)/10)}' opacity='.55'/>`;}); points.forEach((p,n)=>{html+=`<circle cx='${p[0]}' cy='${p[1]}' r='8' fill='#111416'/><text x='${p[0]+10}' y='${p[1]+4}' fill='currentColor' font-size='12'>${esc(n)}</text>`;});svg.innerHTML=html; };
  const render = s => { snapshot=s; const m=s.last_5_minutes,o=s.operations; $('metricVelocity').textContent=m.current_velocity+' / min'; $('metricChatters').textContent=m.unique_chatters; $('metricAlerts').textContent=s.open_alerts.length; $('metricIncidents').textContent=s.active_incidents.length; $('metricPending').textContent=o.pending_actions; $('metricConfirmed').textContent=o.provider_confirmed_actions; drawVelocity(s.velocity||[]); drawAudience(s.audience_graph||[]);
    $('alertRows').innerHTML=s.open_alerts.map((a,i)=>`<tr tabindex='0' data-index='${i}' class='${i===selected?'selected-row':''}'><td>${esc(a.severity)}</td><td>${esc(a.subject)}</td><td>${esc(a.title)}</td><td class='hide-mobile'>${esc(a.created_at)}</td></tr>`).join('')||'<tr><td colspan="4">No priority findings.</td></tr>';
    document.querySelectorAll('#alertRows tr[data-index]').forEach(row=>row.onclick=()=>{selected=Number(row.dataset.index);render(snapshot);});
    $('incidentRows').innerHTML=s.active_incidents.map(i=>`<tr><td>${i.id}</td><td>${esc(i.severity)}</td><td>${esc(i.title)}</td><td>${i.finding_count}</td><td>${esc(i.assignee||'Unassigned')}</td><td>${i.escalation_level}</td><td><button data-assign='${i.id}'>Assign me</button> <button data-escalate='${i.id}'>Escalate</button></td></tr>`).join('')||'<tr><td colspan="7">No active incidents.</td></tr>';
    document.querySelectorAll('[data-assign]').forEach(b=>b.onclick=()=>post(`/api/live-ops/incidents/${b.dataset.assign}/assign`,{}).then(()=>notice('Incident assigned')).catch(e=>notice(e.message,true))); document.querySelectorAll('[data-escalate]').forEach(b=>b.onclick=()=>post(`/api/live-ops/incidents/${b.dataset.escalate}/escalate`,{note:'Escalated from live command center'}).then(()=>notice('Incident escalated')).catch(e=>notice(e.message,true)));
    $('playbookButtons').innerHTML=s.playbooks.map(p=>`<button class='emergency' data-playbook='${esc(p.playbook_key)}'>${esc(p.name)}</button>`).join(''); document.querySelectorAll('[data-playbook]').forEach(b=>b.onclick=()=>post(`/api/live-ops/playbooks/${b.dataset.playbook}/activate`,{incident_id:s.active_incidents[0]?.id}).then(()=>notice('Playbook completed and controls confirmed')).catch(e=>notice(e.message,true)));
    $('timeline').innerHTML=(s.timeline||[]).slice(-20).reverse().map(t=>`<p><strong>${esc(t.event_type)}</strong> ${esc(t.text||'')}<br><small>${esc(t.occurred_at)}</small></p>`).join('')||'<p class="muted">Timeline begins when the stream starts.</p>';
    $('cohorts').innerHTML=Object.entries(s.cohorts||{}).map(([k,v])=>`<div class='metric'><div class='label'>${esc(k)}</div><div class='value'>${v.members}</div><small>${v.messages} messages</small></div>`).join('')||'<p class="muted">Cohorts calculate during an active stream.</p>';
    const w=s.moderator_workload||{}; $('workload').innerHTML=`<p><strong>Workload balance:</strong> ${Math.round(Number(w.workload_balance_score||0)*100)}%<br><strong>Enforcement consistency:</strong> ${Math.round(Number(w.enforcement_consistency_score||0)*100)}%</p>`+(w.operators||[]).map(x=>`<p>${esc(x.discord_username)} — ${x.actions} actions, ${x.reviews} reviews, ${x.incidents} incidents</p>`).join('');
    $('controls').innerHTML=(s.controls||[]).map(c=>`<p><strong>${esc(c.control_type)}</strong> <span class='tag'>${esc(c.status)}</span> ${esc(c.provider_status||'awaiting Twitch')}<br><small>${esc(c.confirmed_at||c.requested_at)}</small></p>`).join('')||'<p class="muted">No control actions yet.</p>';
    $('briefings').innerHTML=(s.briefings||[]).map(b=>`<article><h3>${esc(b.title)}</h3><p>${esc(b.executive_summary)}</p></article>`).join('')||'<p class="muted">The briefing is generated when a stream ends.</p>';
    if($('destinations')) $('destinations').innerHTML=(s.notification_destinations||[]).map(d=>`<span class='tag'>${esc(d.name)} · ${esc(d.minimum_severity)}+</span>`).join('');
  };
  const context = async () => { const a=snapshot?.open_alerts[selected]; if(!a?.observation_id)return notice('Selected finding has no conversation context',true); try{const r=await fetch(`/api/observations/${a.observation_id}/context`),d=await r.json();$('contextPanel').innerHTML=d.items.map(x=>`<div class='context-item ${x.is_finding?'finding':''}'><strong>${esc(x.username)}</strong> ${esc(x.text||x.event_type)}<br><small>${esc(x.occurred_at)}</small></div>`).join('');}catch(e){notice(e.message,true);} };
  const moderate = action => { const a=snapshot?.open_alerts[selected]; if(!a?.message_id)return notice('Select a message-backed finding',true); post('/api/live-ops/moderate',{message_id:a.message_id,action_type:action,duration_seconds:600}).then(d=>notice(`${action} queued; action ${d.action_id} awaits Twitch confirmation`)).catch(e=>notice(e.message,true)); };
  document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return; if(!snapshot)return; if(e.key.toLowerCase()==='j')selected=Math.min(snapshot.open_alerts.length-1,selected+1);else if(e.key.toLowerCase()==='k')selected=Math.max(0,selected-1);else if(e.key.toLowerCase()==='w')moderate('warn');else if(e.key.toLowerCase()==='t')moderate('timeout');else if(e.key.toLowerCase()==='b')moderate('ban');else if(e.key.toLowerCase()==='c')context();else return;e.preventDefault();render(snapshot);});
  document.querySelectorAll('[data-shield]').forEach(b=>b.onclick=()=>post('/api/live-ops/twitch/shield-mode',{active:b.dataset.shield==='true'}).then(()=>notice('Shield Mode confirmed by Twitch')).catch(e=>notice(e.message,true))); document.querySelectorAll('[data-chat]').forEach(b=>b.onclick=()=>{const presets={followers:{follower_mode:true,follower_mode_duration:10},slow:{slow_mode:true,slow_mode_wait_time:10},normal:{follower_mode:false,slow_mode:false,subscriber_mode:false,unique_chat_mode:false}};post('/api/live-ops/twitch/chat-settings',{settings:presets[b.dataset.chat]}).then(()=>notice('Chat settings confirmed by Twitch')).catch(e=>notice(e.message,true));});
  if($('destinationForm')) $('destinationForm').onsubmit=e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));post('/api/live-ops/notifications',body).then(()=>{notice('Destination added');e.target.reset();}).catch(x=>notice(x.message,true));};
	const source=new EventSource('/api/live-ops/stream'); source.addEventListener('snapshot',e=>{render(JSON.parse(e.data));$('connectionDot').classList.add('live');$('connectionState').textContent='Live · '+new Date().toLocaleTimeString();}); source.onerror=()=>{$('connectionDot').classList.remove('live');$('connectionState').textContent='Reconnecting to live stream…';};
})();
</script>""",
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_twitch_eventsub(self, handler: BaseHTTPRequestHandler) -> None:
		secret = self.settings.twitch_eventsub_secret
		if not secret:
			self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "eventsub_not_configured"})
			return
		try:
			length = int(handler.headers.get("Content-Length", "0") or 0)
		except (TypeError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
			return
		if length <= 0 or length > 1_048_576:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_body_length"})
			return
		body = handler.rfile.read(length)
		message_id = (handler.headers.get("Twitch-Eventsub-Message-Id") or "").strip()
		timestamp = (handler.headers.get("Twitch-Eventsub-Message-Timestamp") or "").strip()
		signature = (handler.headers.get("Twitch-Eventsub-Message-Signature") or "").strip()
		if not verify_eventsub_signature(
			secret, message_id=message_id, timestamp=timestamp, body=body, signature=signature
		):
			self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "invalid_eventsub_signature"})
			return
		try:
			payload = json.loads(body.decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
			return
		if not isinstance(payload, dict):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
			return
		subscription = payload.get("subscription")
		event = payload.get("event")
		subscription_map = subscription if isinstance(subscription, Mapping) else {}
		event_map = event if isinstance(event, Mapping) else {}
		condition = subscription_map.get("condition")
		condition_map = condition if isinstance(condition, Mapping) else {}
		broadcaster_id = str(
			event_map.get("broadcaster_user_id") or condition_map.get("broadcaster_user_id")
			or event_map.get("to_broadcaster_user_id") or ""
		).strip()
		if not broadcaster_id:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_broadcaster_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				tenant = resolve_tenant_context(
					connection, platform="twitch", external_community_id=broadcaster_id
				)
			except LookupError:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "unknown_installation"})
				return
			if subscription_map:
				record_subscription(connection, community_id=tenant.community_id, subscription=subscription_map)
			message_type = (handler.headers.get("Twitch-Eventsub-Message-Type") or "notification").strip()
			if message_type == "webhook_callback_verification":
				challenge = str(payload.get("challenge") or "")
				self._send_text(handler, HTTPStatus.OK, challenge)
				return
			if message_type == "revocation":
				mark_subscription_event(
					connection, str(subscription_map.get("id") or ""),
					str(subscription_map.get("status") or "revoked"),
				)
				self._send_json(handler, HTTPStatus.NO_CONTENT, {})
				return
			observation = observation_from_eventsub(
				payload, message_id=message_id, community_id=tenant.community_id
			)
			if observation is None:
				self._send_json(handler, HTTPStatus.ACCEPTED, {"status": "unsupported_event"})
				return
			result = collect_observation(
				connection, replace(observation, installation_id=tenant.installation_id)
			)
			mark_subscription_event(connection, str(subscription_map.get("id") or ""))
			self._send_json(handler, HTTPStatus.ACCEPTED, {
				"status": result.status, "observation_id": result.observation_id,
			})
		finally:
			connection.close()

	def _read_session(
	    self, handler: BaseHTTPRequestHandler) -> DashboardSession | None:
		return parse_session_cookie(
			self.settings.dashboard_session_secret or "",
			self._read_cookie_value(handler, "qbot4k_session"),
		)

	def _read_cookie_value(self, handler: BaseHTTPRequestHandler,
	                       cookie_name: str) -> str | None:
		cookie = handler.headers.get("Cookie", "")
		for part in cookie.split(";"):
			name, _, value = part.strip().partition("=")
			if name == cookie_name:
				return value
		return None

	def _oauth_redirect_uri(self, handler: BaseHTTPRequestHandler) -> str:
		if self.settings.discord_oauth_redirect_uri:
			return self.settings.discord_oauth_redirect_uri
		origin = self._request_origin(handler)
		if origin is not None:
			return f"{origin}/oauth/discord/callback"
		raise ValueError("Unable to determine Discord OAuth redirect URI")

	def _discord_install_redirect_uri(self, handler: BaseHTTPRequestHandler) -> str:
		if self.settings.discord_oauth_redirect_uri:
			parsed = urlparse(self.settings.discord_oauth_redirect_uri)
			return parsed._replace(
				path="/integrations/discord/callback", params="", query="", fragment=""
			).geturl()
		origin = self._request_origin(handler)
		if origin is not None:
			return f"{origin}/integrations/discord/callback"
		raise ValueError("Unable to determine Discord installation redirect URI")

	def _twitch_install_redirect_uri(self, handler: BaseHTTPRequestHandler) -> str:
		origin = self._request_origin(handler)
		if origin is not None:
			return f"{origin}/integrations/twitch/callback"
		raise ValueError("Unable to determine Twitch installation redirect URI")

	def _request_origin(self, handler: BaseHTTPRequestHandler) -> str | None:
		scheme = (handler.headers.get("X-Forwarded-Proto")
		          or "").split(",", 1)[0].strip() or "http"
		host = (handler.headers.get("X-Forwarded-Host")
		        or handler.headers.get("Host") or "").split(",", 1)[0].strip()
		if not host:
			return None
		return f"{scheme}://{host}"

	def _valid_request_origin(self, handler: BaseHTTPRequestHandler) -> bool:
		origin = (handler.headers.get("Origin") or "").strip().rstrip("/")
		if not origin:
			return True
		expected = self._request_origin(handler)
		return bool(expected) and hmac.compare_digest(origin, expected.rstrip("/"))

	def _log_exception(self, message: str, exc: Exception) -> None:
		logging.getLogger("qbot4k.dashboard").exception("%s: %s", message, exc)

	def _build_oauth_state_token(self) -> str:
		if not self.settings.dashboard_session_secret:
			return build_oauth_state()
		nonce = build_oauth_state()
		signature = hmac.new(
			self.settings.dashboard_session_secret.encode("utf-8"),
			nonce.encode("utf-8"),
			hashlib.sha256,
		).hexdigest()
		return f"{nonce}.{signature}"

	def _is_valid_oauth_state_token(self, token: str) -> bool:
		if not self.settings.dashboard_session_secret or "." not in token:
			return False
		nonce, signature = token.rsplit(".", 1)
		if not nonce or not signature:
			return False
		expected_signature = hmac.new(
			self.settings.dashboard_session_secret.encode("utf-8"),
			nonce.encode("utf-8"),
			hashlib.sha256,
		).hexdigest()
		return hmac.compare_digest(signature, expected_signature)

	def _require_session(
		self, handler: BaseHTTPRequestHandler, admin_only: bool = False,
		permission: str | None = None,
	) -> DashboardSession | None:
		caller_name = sys._getframe(1).f_code.co_name
		policy = DASHBOARD_SURFACE_POLICIES.get(caller_name)
		permission = permission or self._calling_surface_capability()
		session = self._read_session(handler)
		if session is None:
			self._redirect(handler, "/login")
			return None
		try:
			operator_id = int(session.user_id)
		except ValueError:
			self._redirect(handler, "/login")
			return None
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			operator = connection.execute(
				"SELECT discord_username,role,session_version,status FROM operator_accounts WHERE id=?",
				(operator_id,),
			).fetchone()
			memberships = list_operator_communities(connection, operator_id)
		finally:
			connection.close()
		if operator is None or str(operator[3]) != "active" or not memberships:
			self._redirect(handler, "/login")
			return None
		if session.session_version != int(operator[2]):
			self._redirect(handler, "/login")
			return None
		if session.community_id is None:
			membership = memberships[0]
		else:
			membership = next(
				(row for row in memberships if int(row["id"]) == session.community_id), None
			)
			if membership is None:
				self._send_text(handler, HTTPStatus.FORBIDDEN, "Community access is no longer available")
				return None
		current_session = DashboardSession(
			user_id=session.user_id,
			username=str(operator[0]),
			role=str(membership["role"]),
			expires_at=session.expires_at,
			community_id=int(membership["id"]),
			session_version=int(operator[2]),
		)
		if admin_only and current_session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Forbidden")
			return None
		if permission is not None:
			connection = connect_database(self.settings.database_path)
			try:
				initialize_database(connection)
				authorized = operator_has_permission(
					connection, operator_id=operator_id,
					community_id=int(current_session.community_id), permission=permission,
				)
			finally:
				connection.close()
			if not authorized:
				self._send_text(handler, HTTPStatus.FORBIDDEN, "Forbidden")
				return None
		quota_type = "exports" if policy is not None and policy.kind == "export" else (
			"api" if caller_name.startswith("_serve_api_") else None
		)
		if quota_type is not None:
			connection = connect_database(self.settings.database_path)
			try:
				initialize_database(connection)
				consume_tenant_quota(
					connection, tenant=TenantContext(int(current_session.community_id)),
					quota_type=quota_type,
				)
				connection.commit()
			except TenantQuotaExceededError as exc:
				self._send_quota_exceeded(handler, exc)
				return None
			finally:
				connection.close()
		return current_session

	def _calling_surface_capability(self) -> str | None:
		caller_name = sys._getframe(2).f_code.co_name
		policy = DASHBOARD_SURFACE_POLICIES.get(caller_name)
		return policy.capability if policy is not None else None

	def _serve_community_switch(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			community_id = int((form.get("community_id") or [""])[0])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid community")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			membership = connection.execute(
				"""SELECT role FROM operator_community_roles
				   WHERE operator_id=? AND community_id=?""",
				(int(session.user_id), community_id),
			).fetchone()
			if membership is None:
				self._send_text(handler, HTTPStatus.FORBIDDEN, "Community access is not available")
				return
			connection.execute(
				"""INSERT INTO audit_log(
				       actor_type, actor_id, action_type, entity_type, entity_id, payload_json
				   ) VALUES ('operator', ?, 'community.switched', 'community', ?, ?)""",
				(
					int(session.user_id), community_id,
					json.dumps({"from_community_id": session.community_id}, sort_keys=True),
				),
			)
			connection.commit()
		finally:
			connection.close()
		switched_session = DashboardSession(
			user_id=session.user_id, username=session.username, role=str(membership[0]),
			expires_at=session.expires_at, community_id=community_id,
			session_version=session.session_version,
		)
		cookie_value = create_session_cookie(
			self.settings.dashboard_session_secret or "", switched_session
		)
		self._redirect(
			handler, "/dashboard",
			cookies=(
				f"qbot4k_session={cookie_value}; Path=/; HttpOnly; SameSite=Lax{self._secure_cookie_suffix(handler)}",
			),
		)

	def _serve_login(self, handler: BaseHTTPRequestHandler) -> None:
		if not self.settings.discord_oauth_client_id or not self.settings.dashboard_session_secret:
			self._send_text(
    handler,
    HTTPStatus.SERVICE_UNAVAILABLE,
     "Discord OAuth is not configured")
			return
		state = self._build_oauth_state_token()
		redirect_uri = self._oauth_redirect_uri(handler)
		redirect_target = build_discord_oauth_url(
			self.settings.discord_oauth_client_id,
			redirect_uri,
			state,
		)
		self._redirect(handler, redirect_target, cookies=(
		    f"qbot4k_oauth_state={state}; Path=/; HttpOnly; SameSite=Lax{self._secure_cookie_suffix(handler)}",))

	def _serve_oauth_callback(
	    self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		if not self.settings.dashboard_session_secret:
			self._send_text(
    handler,
    HTTPStatus.SERVICE_UNAVAILABLE,
     "Session secret is not configured")
			return
		code = (query.get("code") or [""])[0].strip()
		state = (query.get("state") or [""])[0].strip()
		cookie_state = self._read_cookie_value(handler, "qbot4k_oauth_state") or ""
		state_matches_cookie = bool(cookie_state) and state == cookie_state
		state_matches_signature = self._is_valid_oauth_state_token(state)
		if not state or not state_matches_cookie and not state_matches_signature:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid OAuth state")
			return
		if not code:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Missing OAuth code")
			return
		if not self.settings.discord_oauth_client_id or not self.settings.discord_oauth_client_secret:
			self._send_text(
    handler,
    HTTPStatus.SERVICE_UNAVAILABLE,
     "Discord OAuth is not configured")
			return
		redirect_uri = self._oauth_redirect_uri(handler)

		try:
			access_token = exchange_discord_code_for_token(
				self.settings.discord_oauth_client_id,
				self.settings.discord_oauth_client_secret,
				redirect_uri,
				code,
			)
			identity = fetch_discord_identity(access_token)
			role = determine_operator_role(identity, self.settings.operator_guild_ids)
			if role is None:
				self._send_text(
    handler,
    HTTPStatus.FORBIDDEN,
     "You are not authorized to access the dashboard")
				return
		except Exception as exc:
			self._log_exception("discord oauth failed", exc)
			self._send_text(handler, HTTPStatus.BAD_GATEWAY, "Discord OAuth failed")
			return

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			operator_id = upsert_operator_account(
				connection,
				discord_user_id=identity.user_id,
				discord_username=identity.username,
				role=role,
			)
			accept_operator_invitations(
				connection, operator_id=operator_id, discord_user_id=identity.user_id,
			)
			operator_status = str(connection.execute(
				"SELECT status FROM operator_accounts WHERE id=?", (operator_id,)
			).fetchone()[0])
			if operator_status != "active":
				self._send_text(handler, HTTPStatus.FORBIDDEN, "Operator access is disabled")
				return
			record_operator_discord_guild_permissions(
				connection, operator_id=operator_id, permissions=identity.permissions,
				actor_operator_id=operator_id,
			)
			connection.execute(
				"""INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
				   VALUES ('operator',?,'auth.login','operator_account',?,?)""",
				(operator_id, operator_id, json.dumps({"discord_user_id": identity.user_id, "role": role}, sort_keys=True)),
			)
			connection.commit()
		finally:
			connection.close()

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			memberships = list_operator_communities(connection, operator_id)
			session_version = int(connection.execute(
				"SELECT session_version FROM operator_accounts WHERE id=?", (operator_id,)
			).fetchone()[0])
		finally:
			connection.close()
		active_membership = memberships[0] if memberships else None
		session = build_session(
			str(operator_id), identity.username,
			str(active_membership["role"]) if active_membership is not None else role,
			community_id=int(active_membership["id"]) if active_membership is not None else None,
			session_version=session_version,
		)
		cookie_value = create_session_cookie(
    self.settings.dashboard_session_secret, session)
		self._redirect(
			handler,
			"/dashboard",
			cookies=(
				f"qbot4k_session={cookie_value}; Path=/; HttpOnly; SameSite=Lax{self._secure_cookie_suffix(handler)}",
				f"qbot4k_oauth_state=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{self._secure_cookie_suffix(handler)}",
			),
		)

	def _serve_operator_invitation(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="operators.manage")
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			expires_hours = max(1, min(int(payload.get("expires_hours") or 72), 720))
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			invitation_id = invite_operator(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)),
				target_discord_user_id=str(payload.get("discord_user_id") or ""),
				role=str(payload.get("role") or ""),
				expires_at=(datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat(),
			)
		except (TypeError, ValueError, sqlite3.IntegrityError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.CREATED, {"invitation_id": invitation_id})

	def _serve_operator_access_action(
		self, handler: BaseHTTPRequestHandler, path: str,
	) -> None:
		session = self._require_session(handler, permission="operators.manage")
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		parts = [part for part in path.split("/") if part]
		try:
			entity_id = int(parts[2])
			action = parts[3]
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			if action == "revoke-invitation":
				revoke_operator_invitation(
					connection, invitation_id=entity_id,
					tenant=TenantContext(int(session.community_id)),
					actor=ActorAttribution("operator", int(session.user_id)),
				)
			elif action == "transfer-ownership":
				expected = f"TRANSFER OWNERSHIP {entity_id}"
				if not hmac.compare_digest(str(payload.get("confirmation") or ""), expected):
					raise ValueError(f"confirmation must be {expected}")
				transfer_community_ownership(
					connection, tenant=TenantContext(int(session.community_id)),
					actor=ActorAttribution("operator", int(session.user_id)), new_owner_id=entity_id,
				)
			elif action == "emergency-remove":
				expected = f"EMERGENCY REMOVE {entity_id}"
				if not hmac.compare_digest(str(payload.get("confirmation") or ""), expected):
					raise ValueError(f"confirmation must be {expected}")
				emergency_remove_operator_access(
					connection, tenant=TenantContext(int(session.community_id)),
					actor=ActorAttribution("operator", int(session.user_id)), operator_id=entity_id,
					reason=str(payload.get("reason") or ""),
				)
			else:
				raise ValueError("unsupported operator access action")
		except (IndexError, LookupError, PermissionError, ValueError) as exc:
			self._send_json(handler, HTTPStatus.CONFLICT, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "completed"})

	def _serve_api_bulk_moderation(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="moderation.bulk")
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			targets_raw = payload.get("target_platform_account_ids")
			if not isinstance(targets_raw, list):
				raise ValueError("target_platform_account_ids must be a list")
			targets = list(dict.fromkeys(int(item) for item in targets_raw))
			action_type = str(payload.get("action_type") or "").strip().casefold()
			dry_run = bool(payload.get("dry_run", True))
			if not dry_run:
				label = "PERMANENT BAN" if action_type == "ban" else action_type.upper()
				expected = f"BULK {label} {len(targets)}"
				if not hmac.compare_digest(str(payload.get("confirmation") or ""), expected):
					raise ValueError(f"confirmation must be {expected}")
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			result = execute_bulk_moderation(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)),
				target_platform_account_ids=targets, action_type=action_type,
				reason=str(payload.get("reason") or ""),
				duration_seconds=int(payload.get("duration_seconds") or 600), dry_run=dry_run,
			)
		except (PermissionError, TypeError, ValueError) as exc:
			self._send_json(handler, HTTPStatus.CONFLICT, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, result)

	def _serve_api_integration_revoke(
		self, handler: BaseHTTPRequestHandler, path: str,
	) -> None:
		session = self._require_session(handler, permission="integrations.manage")
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			installation_id = int([part for part in path.split("/") if part][2])
			expected = f"REVOKE INTEGRATION {installation_id}"
			if not hmac.compare_digest(str(payload.get("confirmation") or ""), expected):
				raise ValueError(f"confirmation must be {expected}")
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			revoke_installation(
				connection, community_id=int(session.community_id), installation_id=installation_id,
				actor_operator_id=int(session.user_id),
			)
		except (IndexError, LookupError, ValueError) as exc:
			self._send_json(handler, HTTPStatus.CONFLICT, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "revoked", "installation_id": installation_id})

	def _serve_settings(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None or session.community_id is None:
			return
		community_id = int(session.community_id)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			community = connection.execute(
				"""SELECT name,slug,locale,timezone,description,guidelines,
				          notifications_enabled
				   FROM communities WHERE id=? AND status='active'""",
				(community_id,),
			).fetchone()
			policy = connection.execute(
				"""SELECT message_retention_days,analytics_retention_days,
				          anti_abuse_enabled,anti_abuse_enforcement_mode,
				          message_burst_limit,message_burst_window_seconds,
				          mention_limit,join_raid_limit,join_raid_window_seconds
				   FROM community_policy_settings WHERE community_id=?""",
				(community_id,),
			).fetchone()
			installations = connection.execute(
				"""SELECT platform,display_name,status,health_status
				   FROM community_installations WHERE community_id=?
				   ORDER BY platform,display_name COLLATE NOCASE""",
				(community_id,),
			).fetchall()
			destinations = connection.execute(
				"""SELECT name,destination_type,minimum_severity,enabled
				   FROM notification_destinations WHERE community_id=? ORDER BY name""",
				(community_id,),
			).fetchall()
			operators = connection.execute(
				"""SELECT o.discord_username,r.role FROM operator_community_roles r
				   JOIN operator_accounts o ON o.id=r.operator_id
				   WHERE r.community_id=? AND o.status='active'
				   ORDER BY o.discord_username COLLATE NOCASE""",
				(community_id,),
			).fetchall()
			invitations = connection.execute(
				"""SELECT target_discord_user_id,invited_role,expires_at
				   FROM operator_invitations
				   WHERE community_id=? AND status='pending' ORDER BY created_at DESC""",
				(community_id,),
			).fetchall()
			can_manage_integrations = operator_has_permission(
				connection, operator_id=int(session.user_id), community_id=community_id,
				permission="integrations.manage",
			)
			can_manage_operators = operator_has_permission(
				connection, operator_id=int(session.user_id), community_id=community_id,
				permission="operators.manage",
			)
		finally:
			connection.close()
		if community is None or policy is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Community settings not found")
			return
		status_message = (query.get("status") or [""])[0].strip()
		installation_rows = "".join(
			f"<tr><td>{self._escape(row['platform'])}</td><td>{self._escape(row['display_name'])}</td>"
			f"<td>{self._escape(row['status'])}</td><td>{self._escape(row['health_status'])}</td></tr>"
			for row in installations
		) or "<tr><td colspan='4'>No integrations configured</td></tr>"
		destination_rows = "".join(
			f"<tr><td>{self._escape(row['name'])}</td><td>{self._escape(row['destination_type'])}</td>"
			f"<td>{self._escape(row['minimum_severity'])}</td><td>{'Enabled' if row['enabled'] else 'Disabled'}</td></tr>"
			for row in destinations
		) or "<tr><td colspan='4'>No notification destinations</td></tr>"
		operator_rows = "".join(
			f"<tr><td>{self._escape(row['discord_username'])}</td><td>{self._escape(row['role'])}</td></tr>"
			for row in operators
		) or "<tr><td colspan='2'>No active operators</td></tr>"
		invitation_rows = "".join(
			f"<tr><td>{self._escape(row['target_discord_user_id'])}</td>"
			f"<td>{self._escape(row['invited_role'])}</td><td>{self._escape(row['expires_at'])}</td></tr>"
			for row in invitations
		) or "<tr><td colspan='3'>No pending invitations</td></tr>"
		content = (
			"<section class='hero'><p class='eyebrow'>Community administration</p>"
			f"<h1>{self._escape(community['name'])}</h1>"
			f"<p class='lede'>Profile, policy, retention, integrations, notifications, and access for {self._escape(community['slug'])}.</p>"
			+ (f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else "")
			+ "</section><form method='post' action='/settings' class='card'><h2>Profile and locale</h2><div class='columns'>"
			f"<label>Community name<input name='name' required maxlength='120' value='{self._escape(community['name'])}'></label>"
			f"<label>Locale<input name='locale' required maxlength='35' value='{self._escape(community['locale'])}'></label>"
			f"<label>Timezone<input name='timezone' required value='{self._escape(community['timezone'])}'></label>"
			f"<label class='checkbox'><input type='checkbox' name='notifications_enabled'{' checked' if community['notifications_enabled'] else ''}> Enable notifications</label></div>"
			f"<label>Description<textarea name='description' maxlength='1000' rows='4'>{self._escape(community['description'])}</textarea></label>"
			f"<label>Guidelines<textarea name='guidelines' maxlength='10000' rows='8'>{self._escape(community['guidelines'])}</textarea></label>"
			"<h2>Retention and anti-abuse policy</h2><div class='columns'>"
			f"<label>Message retention days<input type='number' name='message_retention_days' min='1' max='3650' value='{int(policy['message_retention_days'])}'></label>"
			f"<label>Analytics retention days<input type='number' name='analytics_retention_days' min='1' max='3650' value='{int(policy['analytics_retention_days'])}'></label>"
			f"<label>Enforcement mode<select name='anti_abuse_enforcement_mode'><option value='shadow'{' selected' if policy['anti_abuse_enforcement_mode']=='shadow' else ''}>Shadow</option><option value='enforce'{' selected' if policy['anti_abuse_enforcement_mode']=='enforce' else ''}>Enforce</option></select></label>"
			f"<label class='checkbox'><input type='checkbox' name='anti_abuse_enabled'{' checked' if policy['anti_abuse_enabled'] else ''}> Enable anti-abuse controls</label>"
			f"<label>Message burst limit<input type='number' name='message_burst_limit' min='2' max='100' value='{int(policy['message_burst_limit'])}'></label>"
			f"<label>Burst window seconds<input type='number' name='message_burst_window_seconds' min='1' max='300' value='{int(policy['message_burst_window_seconds'])}'></label>"
			f"<label>Mention limit<input type='number' name='mention_limit' min='1' max='100' value='{int(policy['mention_limit'])}'></label>"
			f"<label>Join raid limit<input type='number' name='join_raid_limit' min='2' max='1000' value='{int(policy['join_raid_limit'])}'></label>"
			f"<label>Join window seconds<input type='number' name='join_raid_window_seconds' min='1' max='3600' value='{int(policy['join_raid_window_seconds'])}'></label>"
			"</div><button type='submit'>Save settings</button></form>"
			"<section class='card'><h2>Integrations</h2><table><thead><tr><th>Platform</th><th>Name</th><th>Status</th><th>Health</th></tr></thead><tbody>"
			+ installation_rows + "</tbody></table>"
			+ ("<p><a href='/integrations'>Manage integrations</a></p>" if can_manage_integrations else "")
			+ "</section><section class='card'><h2>Notification destinations</h2><table><thead><tr><th>Name</th><th>Type</th><th>Minimum severity</th><th>Status</th></tr></thead><tbody>"
			+ destination_rows + "</tbody></table><p><a href='/live-ops'>Manage notification routing</a></p></section>"
			+ ("<section class='card'><h2>Operators</h2><table><thead><tr><th>Operator</th><th>Role</th></tr></thead><tbody>" + operator_rows + "</tbody></table>"
			   "<h3>Pending invitations</h3><table><thead><tr><th>Discord user ID</th><th>Role</th><th>Expires</th></tr></thead><tbody>" + invitation_rows + "</tbody></table>"
			   "<form method='post' action='/settings/operators/invite' class='toolbar'>"
			   "<label>Discord user ID<input name='discord_user_id' required></label>"
			   "<label>Role<select name='role'><option value='viewer'>Viewer</option><option value='analyst'>Analyst</option><option value='moderator'>Moderator</option><option value='admin'>Admin</option></select></label>"
			   "<label>Expires in hours<input type='number' name='expires_hours' min='1' max='720' value='72'></label>"
			   "<button type='submit'>Invite operator</button></form></section>" if can_manage_operators else "")
			+ "<section class='card'><h2>Onboarding policy</h2><p><a href='/onboarding'>Manage welcomes, verification, resources, and guidelines delivery</a></p></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Settings", session, content))

	def _serve_settings_update(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		tenant = TenantContext(int(session.community_id))
		actor = ActorAttribution("operator", int(session.user_id))
		try:
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			configure_community_profile(
				connection, tenant=tenant, actor=actor,
				name=(form.get("name") or [""])[0], locale=(form.get("locale") or [""])[0],
				timezone_name=(form.get("timezone") or [""])[0],
				description=(form.get("description") or [""])[0],
				guidelines=(form.get("guidelines") or [""])[0],
				notifications_enabled="notifications_enabled" in form,
			)
			configure_retention_policy(
				connection, tenant=tenant, actor=actor,
				message_retention_days=int((form.get("message_retention_days") or [""])[0]),
				analytics_retention_days=int((form.get("analytics_retention_days") or [""])[0]),
			)
			configure_anti_abuse_policy(
				connection, tenant=tenant, actor=actor,
				enabled="anti_abuse_enabled" in form,
				enforcement_mode=(form.get("anti_abuse_enforcement_mode") or [""])[0],
				message_burst_limit=int((form.get("message_burst_limit") or [""])[0]),
				message_burst_window_seconds=int((form.get("message_burst_window_seconds") or [""])[0]),
				mention_limit=int((form.get("mention_limit") or [""])[0]),
				join_raid_limit=int((form.get("join_raid_limit") or [""])[0]),
				join_raid_window_seconds=int((form.get("join_raid_window_seconds") or [""])[0]),
			)
		except (LookupError, ValueError) as exc:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/settings?status=" + quote("Settings saved"))

	def _serve_settings_operator_invite(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="operators.manage")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			expires_hours = max(1, min(int((form.get("expires_hours") or ["72"])[0]), 720))
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			invite_operator(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)),
				target_discord_user_id=(form.get("discord_user_id") or [""])[0],
				role=(form.get("role") or [""])[0],
				expires_at=(datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat(),
			)
		except (sqlite3.IntegrityError, TypeError, ValueError) as exc:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/settings?status=" + quote("Operator invited"))

	def _serve_integrations(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		operator_id = int(session.user_id)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			communities = list_operator_communities(connection, operator_id)
			guilds = connection.execute(
				"""SELECT guild_id, permissions FROM operator_discord_guild_permissions
				   WHERE operator_id=? AND (permissions & ?) != 0 ORDER BY guild_id""",
				(operator_id, (1 << 3) | (1 << 5)),
			).fetchall()
			installations = connection.execute(
				"""SELECT i.community_id, i.platform, i.external_community_id,
				          i.display_name, i.status, i.health_status, i.last_error,
				          i.last_health_check_at, i.metadata_json
				   FROM community_installations i
				   JOIN operator_community_roles r ON r.community_id=i.community_id
				   WHERE r.operator_id=?
				   ORDER BY i.platform,display_name COLLATE NOCASE"""
				, (operator_id,)
			).fetchall()
		finally:
			connection.close()
		status_message = (query.get("status") or [""])[0].strip()
		error_message = (query.get("error") or [""])[0].strip()
		discord_forms = "".join(
			f"<form method='post' action='/integrations/discord/link' class='card'>"
			f"<h2>{self._escape(community['name'])}</h2>"
			f"<label>Discord server<select name='guild_id' required>"
			+ "".join(
				f"<option value='{self._escape(guild['guild_id'])}'>{self._escape(guild['guild_id'])}</option>"
				for guild in guilds
			)
			+ "</select></label>"
			"<label>Pilot invitation code<input name='pilot_invite_code' required autocomplete='off'></label>"
			f"<input type='hidden' name='community_id' value='{int(community['id'])}'>"
			"<button type='submit'>Link Discord</button></form>"
			for community in communities if str(community["role"]) in {"admin", "owner"}
		)
		twitch_forms = "".join(
			f"<form method='post' action='/integrations/twitch/link' class='card'>"
			f"<h2>{self._escape(community['name'])}</h2>"
			"<label>Twitch broadcaster login<input name='broadcaster_login' required "
			"autocomplete='off' placeholder='channel_name'></label>"
			"<fieldset><legend>Reviewed access</legend>"
			"<label><input type='checkbox' name='scope' value='moderator:read:followers' checked> Followers</label>"
			"<label><input type='checkbox' name='scope' value='channel:read:subscriptions' checked> Subscriptions</label>"
			"<label><input type='checkbox' name='scope' value='moderator:manage:banned_users'> Moderation actions</label>"
			"<label><input type='checkbox' name='scope' value='moderator:manage:chat_settings'> Chat controls</label>"
			"<label><input type='checkbox' name='scope' value='moderator:manage:shield_mode'> Shield mode</label>"
			"</fieldset><button type='submit'>Link Twitch</button></form>"
			for community in communities if str(community["role"]) in {"admin", "owner"}
		)
		discord_rows = "".join(
			f"<tr><td>{self._escape(row['display_name'])}</td>"
			f"<td>{self._escape(row['external_community_id'])}</td>"
			f"<td>{self._escape(row['status'])}</td></tr>"
			for row in installations if str(row["platform"]) == "discord"
		) or "<tr><td colspan='3'>No Discord installations</td></tr>"
		twitch_rows = "".join(
			f"<tr><td>{self._escape(row['display_name'])}</td>"
			f"<td>{self._escape(row['status'])}</td>"
			f"<td>{self._escape(row['health_status'])}</td>"
			f"<td>{self._escape(row['last_error'] or 'Awaiting connection check')}</td></tr>"
			for row in installations if str(row["platform"]) == "twitch"
		) or "<tr><td colspan='4'>No Twitch installations</td></tr>"
		content = (
			"<section class='hero'><p class='eyebrow'>Community integrations</p>"
			"<h1>Discord and Twitch</h1><p class='lede'>Connect provider installations to the active community.</p>"
			+ (f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else "")
			+ (f"<p class='status-banner error'>{self._escape(error_message)} Retry the Twitch connection.</p>" if error_message else "")
			+ "<h2>Discord</h2>"
			+ (discord_forms if guilds and discord_forms else "<div class='card'><p>No manageable Discord servers are available.</p></div>")
			+ "<div class='card'><h2>Linked servers</h2><table><thead><tr><th>Server</th><th>Guild ID</th><th>Status</th></tr></thead><tbody>"
			+ discord_rows + "</tbody></table></div>"
			+ "<h2>Twitch</h2>" + twitch_forms
			+ "<div class='card'><h2>Linked channels</h2><table><thead><tr><th>Channel</th><th>Status</th><th>Health</th><th>Recovery</th></tr></thead><tbody>"
			+ twitch_rows + "</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Integrations", session, content))

	def _serve_announcements(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Select a community")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			community = connection.execute(
				"SELECT name,timezone FROM communities WHERE id=? AND status='active'",
				(int(session.community_id),),
			).fetchone()
			installations = connection.execute(
				"""SELECT id,display_name,external_community_id FROM community_installations
				   WHERE community_id=? AND platform='discord' AND status='active'
				   ORDER BY display_name COLLATE NOCASE,id""",
				(int(session.community_id),),
			).fetchall()
			rows = connection.execute(
				"""SELECT id,platform,target_external_id,body,status,scheduled_at,last_error
				   FROM community_announcements WHERE community_id=? ORDER BY created_at DESC,id DESC""",
				(int(session.community_id),),
			).fetchall()
			previews = {
				int(row["id"]): preview_announcement(
					connection, announcement_id=int(row["id"]), community_id=int(session.community_id)
				)
				for row in rows
			}
		finally:
			connection.close()
		if community is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Community not found")
			return
		can_manage = session.role in {"admin", "owner"}
		status_message = (query.get("status") or [""])[0].strip()
		form = ""
		if can_manage:
			installation_options = "".join(
				f"<option value='{int(item['id'])}'>{self._escape(item['display_name'])} "
				f"({self._escape(item['external_community_id'])})</option>"
				for item in installations
			)
			form = (
				"<form method='post' action='/announcements' class='card'>"
				"<h2>New draft</h2><input type='hidden' name='platform' value='discord'>"
				"<label>Discord installation<select name='target_installation_id'>"
				"<option value=''>Auto-select sole installation</option>"
				+ installation_options + "</select></label>"
				"<label>Channel or target ID<input name='target_external_id' required></label>"
				"<label>Message<textarea name='body' required maxlength='2000'></textarea></label>"
				"<button type='submit'>Save draft</button></form>"
			)
		body_rows = "".join(
			"<tr>"
			f"<td>{int(row['id'])}</td><td>{self._escape(row['platform'])}</td>"
			f"<td>{self._escape(row['target_external_id'])}</td><td>{self._escape(row['body'])}</td>"
			f"<td>{self._escape(row['status'])}</td><td>{self._escape(previews[int(row['id'])]['scheduled_local'] or '')}</td>"
			+ f"<td>{self._escape(previews[int(row['id'])]['installation_name'] or 'No active installation')}"
			f"<br><span class='muted'>Attempts: {int(previews[int(row['id'])]['attempt_count'])}</span></td>"
			+ (
				f"<td><form method='post' action='/announcements/{int(row['id'])}/approve'>"
				"<input type='datetime-local' name='scheduled_at' required>"
				"<button type='submit'>Approve &amp; schedule</button></form></td>"
				if can_manage and row["status"] == "draft" else
				f"<td><form method='post' action='/announcements/{int(row['id'])}/retry'>"
				"<button type='submit'>Retry</button></form></td>"
				if can_manage and row["status"] == "failed" else
				f"<td>{self._escape(row['last_error'] or '')}</td>"
			)
			+ (
				f"<td><form method='post' action='/announcements/{int(row['id'])}/cancel'>"
				"<button type='submit'>Cancel</button></form></td>"
				if can_manage and row["status"] in {"draft", "scheduled", "failed"} else "<td></td>"
			)
			+ "</tr>"
			for row in rows
		) or "<tr><td colspan='9'>No announcements yet</td></tr>"
		content = (
			"<section class='hero'><p class='eyebrow'>Community management</p><h1>Announcements</h1>"
			f"<p class='lede'>Draft, approve, and schedule messages for {self._escape(community['name'])}. "
			f"Times use the community timezone: {self._escape(community['timezone'])}.</p>"
			+ (f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else "")
			+ form
			+ "<div class='card'><table><thead><tr><th>ID</th><th>Platform</th><th>Target</th>"
			"<th>Preview</th><th>Status</th><th>Scheduled</th><th>Installation</th>"
			"<th>Action / error</th><th>Disable</th></tr></thead><tbody>"
			+ body_rows + "</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Announcements", session, content))

	def _serve_onboarding(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Select a community")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			installations = connection.execute(
				"""SELECT id,display_name,external_community_id FROM community_installations
				   WHERE community_id=? AND platform='discord' AND status='active'
				   ORDER BY display_name COLLATE NOCASE,id""",
				(int(session.community_id),),
			).fetchall()
			settings_row = connection.execute(
				"""SELECT discord_installation_id,welcome_channel_id,welcome_template,welcome_enabled,
				          newcomer_role_id,newcomer_role_enabled,checkpoint_due_hours,
				          checkpoint_reminder_enabled,checkpoint_reminder_template,
				          verification_resource_enabled,verification_resource_url,
				          verification_resource_template,verification_evidence_required,
				          self_service_verification_enabled
				   FROM community_onboarding_settings WHERE community_id=?""",
				(int(session.community_id),),
			).fetchone()
			members = connection.execute(
				"""SELECT platform_user_id,username,status,role_assignment_status,
				          role_assignment_attempts,joined_at,checkpoint_due_at,reminder_sent_at,
				          verification_evidence,verified_at
				   FROM community_onboarding_members WHERE community_id=?
				   ORDER BY CASE status WHEN 'newcomer' THEN 0 ELSE 1 END,joined_at DESC""",
				(int(session.community_id),),
			).fetchall()
			resources = list_onboarding_resources(
				connection, community_id=int(session.community_id)
			)
		finally:
			connection.close()
		selected_id = int(settings_row["discord_installation_id"]) if settings_row is not None else None
		channel_id = str(settings_row["welcome_channel_id"] or "") if settings_row is not None else ""
		template = (
			str(settings_row["welcome_template"])
			if settings_row is not None else "Welcome {mention} to the community!"
		)
		enabled = bool(settings_row["welcome_enabled"]) if settings_row is not None else False
		newcomer_role_id = str(settings_row["newcomer_role_id"] or "") if settings_row is not None else ""
		newcomer_role_enabled = bool(settings_row["newcomer_role_enabled"]) if settings_row is not None else False
		checkpoint_due_hours = int(settings_row["checkpoint_due_hours"]) if settings_row is not None else 24
		checkpoint_reminder_enabled = bool(settings_row["checkpoint_reminder_enabled"]) if settings_row is not None else False
		checkpoint_reminder_template = (
			str(settings_row["checkpoint_reminder_template"])
			if settings_row is not None else "Reminder {mention}: please complete community verification."
		)
		verification_resource_enabled = bool(settings_row["verification_resource_enabled"]) if settings_row is not None else False
		verification_resource_url = str(settings_row["verification_resource_url"] or "") if settings_row is not None else ""
		verification_resource_template = (
			str(settings_row["verification_resource_template"])
			if settings_row is not None
			else "You are verified, {mention}. Community resources: {resource_url}"
		)
		verification_evidence_required = bool(settings_row["verification_evidence_required"]) if settings_row is not None else False
		self_service_verification_enabled = bool(settings_row["self_service_verification_enabled"]) if settings_row is not None else False
		options = "".join(
			f"<option value='{int(row['id'])}'{' selected' if int(row['id']) == selected_id else ''}>"
			f"{self._escape(row['display_name'])} ({self._escape(row['external_community_id'])})</option>"
			for row in installations
		)
		status_message = (query.get("status") or [""])[0].strip()
		preview = template.replace("{mention}", "@new-member").replace("{username}", "new-member")
		resource_preview = (
			verification_resource_template.replace("{mention}", "@new-member")
			.replace("{username}", "new-member").replace("{resource_url}", verification_resource_url)
		)
		member_rows = "".join(
			f"<tr><td>{self._escape(row['username'])}</td><td>{self._escape(row['platform_user_id'])}</td>"
			f"<td>{self._escape(row['status'])}</td><td>{self._escape(row['role_assignment_status'])}</td>"
			f"<td>{int(row['role_assignment_attempts'])}</td><td>{self._escape(row['joined_at'])}</td>"
			f"<td>{self._escape(row['checkpoint_due_at'] or '')}</td>"
			f"<td>{'Sent' if row['reminder_sent_at'] else 'Pending'}</td>"
			+ (
				"<td><form method='post' action='/onboarding/verify'>"
				f"<input type='hidden' name='platform_user_id' value='{self._escape(row['platform_user_id'])}'>"
				+ (
					"<input name='verification_evidence' maxlength='2000' placeholder='Verification evidence' required>"
					if verification_evidence_required else ""
				)
				+ "<button type='submit'>Verify</button></form></td>"
				if session.role in {"admin", "owner"} and row["status"] == "newcomer" else
				f"<td>{self._escape(row['verification_evidence'] or row['verified_at'] or '')}</td>"
			)
			+ "</tr>"
			for row in members
		) or "<tr><td colspan='9'>No onboarding checkpoints yet</td></tr>"
		resource_rows = "".join(
			"<tr><td colspan='5'><form method='post' action='/onboarding/resources' class='compact-form'>"
			+ f"<input type='hidden' name='resource_id' value='{int(row['id'])}'>"
			+ f"<input name='title' value='{self._escape(row['title'])}' required maxlength='120'>"
			+ f"<input type='url' name='resource_url' value='{self._escape(row['resource_url'])}' required>"
			+ f"<input name='message_template' value='{self._escape(row['message_template'])}' required maxlength='2000'>"
			+ f"<input type='number' name='sort_order' value='{int(row['sort_order'])}' min='-1000' max='1000'>"
			+ f"<label><input type='checkbox' name='enabled' value='1'{' checked' if row['enabled'] else ''}> Enabled</label>"
			+ "<button type='submit'>Save</button></form></td>"
			+ f"<td><form method='post' action='/onboarding/resources/{int(row['id'])}/delete'><button type='submit'>Delete</button></form></td></tr>"
			for row in resources
		) or "<tr><td colspan='6'>No catalog resources configured</td></tr>"
		content = (
			"<section class='hero'><p class='eyebrow'>Community onboarding</p><h1>Welcome automation</h1>"
			"<p class='lede'>Preview and control the message sent when a new Discord member joins.</p>"
			+ (f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else "")
			+ f"<div class='card'><h2>Welcome preview</h2><p>{self._escape(preview)}</p>"
			+ f"<h3>Verified-member resource preview</h3><p>{self._escape(resource_preview)}</p></div>"
			+ (
				"<form method='post' action='/onboarding' class='card'><h2>Configuration</h2>"
				"<label>Discord installation<select name='discord_installation_id' required>"
				+ options + "</select></label>"
				f"<label>Welcome channel ID<input name='welcome_channel_id' value='{self._escape(channel_id)}' required></label>"
				f"<label>Message template<textarea name='welcome_template' required maxlength='2000'>{self._escape(template)}</textarea></label>"
				f"<label><input type='checkbox' name='enabled' value='1'{' checked' if enabled else ''}> Enabled</label>"
				f"<label>Newcomer role ID<input name='newcomer_role_id' value='{self._escape(newcomer_role_id)}'></label>"
				f"<label><input type='checkbox' name='newcomer_role_enabled' value='1'{' checked' if newcomer_role_enabled else ''}> Assign newcomer role</label>"
				f"<label>Checkpoint deadline (hours)<input type='number' name='checkpoint_due_hours' min='1' max='720' value='{checkpoint_due_hours}' required></label>"
				f"<label>Reminder template<textarea name='checkpoint_reminder_template' required maxlength='2000'>{self._escape(checkpoint_reminder_template)}</textarea></label>"
				f"<label><input type='checkbox' name='checkpoint_reminder_enabled' value='1'{' checked' if checkpoint_reminder_enabled else ''}> Send overdue checkpoint reminder</label>"
				f"<label>Community resource URL<input type='url' name='verification_resource_url' value='{self._escape(verification_resource_url)}'></label>"
				f"<label>Verified-member resource template<textarea name='verification_resource_template' required maxlength='2000'>{self._escape(verification_resource_template)}</textarea></label>"
				f"<label><input type='checkbox' name='verification_resource_enabled' value='1'{' checked' if verification_resource_enabled else ''}> Send resources after verification</label>"
				f"<label><input type='checkbox' name='verification_evidence_required' value='1'{' checked' if verification_evidence_required else ''}> Require operator evidence to verify</label>"
				f"<label><input type='checkbox' name='self_service_verification_enabled' value='1'{' checked' if self_service_verification_enabled else ''}> Allow members to verify with !verify</label>"
				"<p class='muted'>Use {mention} for a Discord mention and {username} for the display name.</p>"
				"<button type='submit'>Save automation</button></form>"
				if session.role in {"admin", "owner"} and installations else
				"<div class='card'><p>Link an active Discord installation to configure welcomes.</p></div>"
			)
			+ (
				"<div class='card'><h2>Resource catalog</h2>"
				"<form method='post' action='/onboarding/resources' class='compact-form'>"
				"<input name='title' placeholder='Resource title' required maxlength='120'>"
				"<input type='url' name='resource_url' placeholder='https://example.com/resource' required>"
				"<input name='message_template' value='{mention}: {title} - {resource_url}' required maxlength='2000'>"
				"<input type='number' name='sort_order' value='0' min='-1000' max='1000'>"
				"<label><input type='checkbox' name='enabled' value='1' checked> Enabled</label>"
				"<button type='submit'>Add resource</button></form>"
				"<table><thead><tr><th>Title</th><th>URL</th><th>Message</th><th>Order</th><th>Status</th><th>Actions</th></tr></thead>"
				f"<tbody>{resource_rows}</tbody></table></div>"
				if session.role in {"admin", "owner"} else ""
			)
			+ "<div class='card'><h2>Verification checkpoints</h2><table><thead><tr>"
			"<th>Member</th><th>User ID</th><th>Status</th><th>Role</th><th>Attempts</th>"
			"<th>Joined</th><th>Due</th><th>Reminder</th><th>Action</th></tr></thead><tbody>" + member_rows + "</tbody></table></div>"
			+ "</section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Onboarding", session, content))

	def _serve_onboarding_update(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Onboarding management is not authorized")
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			installation_id = int((form.get("discord_installation_id") or [""])[0])
			checkpoint_due_hours = int((form.get("checkpoint_due_hours") or ["24"])[0])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid Discord installation")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				configure_welcome(
					connection, community_id=int(session.community_id),
					discord_installation_id=installation_id,
					welcome_channel_id=(form.get("welcome_channel_id") or [""])[0],
					welcome_template=(form.get("welcome_template") or [""])[0],
					enabled=(form.get("enabled") or [""])[0] == "1",
					operator_id=int(session.user_id),
					newcomer_role_id=(form.get("newcomer_role_id") or [""])[0],
					newcomer_role_enabled=(form.get("newcomer_role_enabled") or [""])[0] == "1",
					checkpoint_due_hours=checkpoint_due_hours,
					checkpoint_reminder_enabled=(form.get("checkpoint_reminder_enabled") or [""])[0] == "1",
					checkpoint_reminder_template=(form.get("checkpoint_reminder_template") or [""])[0],
					verification_resource_enabled=(form.get("verification_resource_enabled") or [""])[0] == "1",
					verification_resource_url=(form.get("verification_resource_url") or [""])[0],
					verification_resource_template=(form.get("verification_resource_template") or [""])[0],
					verification_evidence_required=(form.get("verification_evidence_required") or [""])[0] == "1",
					self_service_verification_enabled=(form.get("self_service_verification_enabled") or [""])[0] == "1",
				)
			except (LookupError, ValueError) as exc:
				self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
				return
		finally:
			connection.close()
		self._redirect(handler, "/onboarding?status=Welcome%20automation%20saved")

	def _serve_onboarding_resource_save(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Onboarding management is not authorized")
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		resource_id_raw = (form.get("resource_id") or [""])[0].strip()
		try:
			resource_id = int(resource_id_raw) if resource_id_raw else None
			sort_order = int((form.get("sort_order") or ["0"])[0])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid resource")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				save_onboarding_resource(
					connection, community_id=int(session.community_id),
					operator_id=int(session.user_id), resource_id=resource_id,
					title=(form.get("title") or [""])[0],
					resource_url=(form.get("resource_url") or [""])[0],
					message_template=(form.get("message_template") or [""])[0],
					enabled=(form.get("enabled") or [""])[0] == "1",
					sort_order=sort_order,
				)
			except LookupError:
				self._send_text(handler, HTTPStatus.NOT_FOUND, "Onboarding resource not found")
				return
			except ValueError as exc:
				self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
				return
		finally:
			connection.close()
		self._redirect(handler, "/onboarding?status=Resource%20saved")

	def _serve_onboarding_resource_delete(
		self, handler: BaseHTTPRequestHandler, path: str
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Onboarding management is not authorized")
			return
		try:
			resource_id = int(path.split("/")[3])
		except (IndexError, ValueError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid resource")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				delete_onboarding_resource(
					connection, community_id=int(session.community_id),
					operator_id=int(session.user_id), resource_id=resource_id,
				)
			except LookupError:
				self._send_text(handler, HTTPStatus.NOT_FOUND, "Onboarding resource not found")
				return
		finally:
			connection.close()
		self._redirect(handler, "/onboarding?status=Resource%20deleted")

	def _serve_onboarding_verify(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Onboarding management is not authorized")
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		platform_user_id = (form.get("platform_user_id") or [""])[0].strip()
		verification_evidence = (form.get("verification_evidence") or [""])[0]
		if not platform_user_id:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Member is required")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				verify_onboarding_member(
					connection, community_id=int(session.community_id),
					platform_user_id=platform_user_id, operator_id=int(session.user_id),
					evidence=verification_evidence,
				)
			except LookupError:
				self._send_text(handler, HTTPStatus.NOT_FOUND, "Newcomer checkpoint not found")
				return
			except ValueError as exc:
				self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
				return
		finally:
			connection.close()
		self._redirect(handler, "/onboarding?status=Member%20verified")

	def _serve_announcement_create(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Announcement management is not authorized")
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		target_installation_raw = (form.get("target_installation_id") or [""])[0].strip()
		try:
			target_installation_id = int(target_installation_raw) if target_installation_raw else None
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid Discord installation")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				create_announcement(
					connection, community_id=int(session.community_id),
					platform=(form.get("platform") or [""])[0],
					target_external_id=(form.get("target_external_id") or [""])[0],
					body=(form.get("body") or [""])[0],
					created_by_operator_id=int(session.user_id),
					target_installation_id=target_installation_id,
				)
			except (LookupError, ValueError) as exc:
				self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
				return
		finally:
			connection.close()
		self._redirect(handler, "/announcements?status=Draft%20saved")

	def _serve_announcement_approve(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Announcement management is not authorized")
			return
		try:
			announcement_id = int(path.split("/")[2])
		except (IndexError, ValueError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid announcement")
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				approve_announcement(
					connection, announcement_id=announcement_id,
					community_id=int(session.community_id), approved_by_operator_id=int(session.user_id),
					scheduled_at=(form.get("scheduled_at") or [""])[0],
				)
			except ValueError as exc:
				self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
				return
			except LookupError:
				self._send_text(handler, HTTPStatus.NOT_FOUND, "Draft announcement not found")
				return
		finally:
			connection.close()
		self._redirect(handler, "/announcements?status=Announcement%20scheduled")

	def _serve_announcement_cancel(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		self._serve_announcement_transition(handler, path, transition="cancel")

	def _serve_announcement_retry(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		self._serve_announcement_transition(handler, path, transition="retry")

	def _serve_announcement_transition(
		self, handler: BaseHTTPRequestHandler, path: str, *, transition: str
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		if session.community_id is None or session.role not in {"admin", "owner"}:
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Announcement management is not authorized")
			return
		try:
			announcement_id = int(path.split("/")[2])
		except (IndexError, ValueError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid announcement")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				if transition == "cancel":
					cancel_announcement(
						connection, announcement_id=announcement_id,
						community_id=int(session.community_id), operator_id=int(session.user_id),
					)
				else:
					retry_announcement(
						connection, announcement_id=announcement_id,
						community_id=int(session.community_id), operator_id=int(session.user_id),
						scheduled_at=datetime.now(timezone.utc).isoformat(),
					)
			except LookupError:
				self._send_text(handler, HTTPStatus.NOT_FOUND, "Announcement not found")
				return
			except ValueError as exc:
				self._send_text(handler, HTTPStatus.CONFLICT, str(exc))
				return
		finally:
			connection.close()
		status = "Announcement%20cancelled" if transition == "cancel" else "Retry%20scheduled"
		self._redirect(handler, f"/announcements?status={status}")

	def _serve_discord_link(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			community_id = int((form.get("community_id") or [""])[0])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid community")
			return
		guild_id = (form.get("guild_id") or [""])[0].strip()
		pilot_invite_code = (form.get("pilot_invite_code") or [""])[0].strip()
		secret = self.settings.dashboard_session_secret or ""
		client_id = self.settings.discord_oauth_client_id or ""
		if not secret or not client_id or not guild_id or not pilot_invite_code:
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Discord installation is not configured")
			return
		state_token = create_discord_install_state(
			secret, operator_id=session.user_id, community_id=community_id, guild_id=guild_id
		)
		state = parse_discord_install_state(secret, state_token)
		if state is None:
			self._send_text(handler, HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to create installation state")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			try:
				create_discord_install_intent(
					connection, nonce=state.nonce, operator_id=int(session.user_id),
					community_id=community_id, guild_id=guild_id,
					expires_at=state.expires_at,
					pilot_invite_code=pilot_invite_code,
				)
			except PermissionError:
				self._send_text(handler, HTTPStatus.FORBIDDEN, "Discord installation is not authorized")
				return
		finally:
			connection.close()
		self._redirect(handler, build_discord_install_url(
			client_id, self._discord_install_redirect_uri(handler), state_token, guild_id
		))

	def _serve_discord_install_callback(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		code = (query.get("code") or [""])[0].strip()
		guild_id = (query.get("guild_id") or [""])[0].strip()
		state_token = (query.get("state") or [""])[0].strip()
		secret = self.settings.dashboard_session_secret or ""
		state = parse_discord_install_state(secret, state_token)
		if (
			state is None or not code or not guild_id
			or state.operator_id != session.user_id or state.guild_id != guild_id
		):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid Discord installation state")
			return
		if not self.settings.discord_oauth_client_id or not self.settings.discord_oauth_client_secret:
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Discord OAuth is not configured")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not discord_install_intent_is_pending(
				connection, nonce=state.nonce, operator_id=int(session.user_id),
				community_id=state.community_id, guild_id=guild_id,
			):
				self._send_text(handler, HTTPStatus.BAD_REQUEST, "Discord installation state was already used")
				return
		finally:
			connection.close()
		try:
			exchange_discord_code_for_token(
				self.settings.discord_oauth_client_id,
				self.settings.discord_oauth_client_secret,
				self._discord_install_redirect_uri(handler),
				code,
			)
		except Exception as exc:
			self._log_exception("discord installation oauth failed", exc)
			self._send_text(handler, HTTPStatus.BAD_GATEWAY, "Discord installation failed")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not complete_discord_install_intent(
				connection, nonce=state.nonce, operator_id=int(session.user_id),
				community_id=state.community_id, guild_id=guild_id,
			):
				self._send_text(handler, HTTPStatus.BAD_REQUEST, "Discord installation state was already used")
				return
		finally:
			connection.close()
		self._redirect(handler, "/integrations?status=Discord%20installation%20pending")

	def _serve_twitch_link(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="integrations.manage")
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		broadcaster_login = (form.get("broadcaster_login") or [""])[0].strip().casefold()
		scopes = tuple(sorted({
			scope.strip() for scope in form.get("scope", []) if scope.strip()
		}))
		client_id = self.settings.twitch_client_id or ""
		secret = self.settings.dashboard_session_secret or ""
		if not client_id or not self.settings.twitch_client_secret or not secret:
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Twitch OAuth is not configured")
			return
		if not broadcaster_login or session.community_id is None:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Twitch broadcaster is required")
			return
		try:
			state_token = create_twitch_install_state(
				secret, operator_id=session.user_id, community_id=int(session.community_id),
				broadcaster_login=broadcaster_login, scopes=scopes,
			)
			state = parse_twitch_install_state(secret, state_token)
			if state is None:
				raise ValueError("Unable to create Twitch installation state")
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			create_twitch_install_intent(
				connection, nonce=state.nonce, operator_id=int(session.user_id),
				community_id=int(session.community_id), broadcaster_login=broadcaster_login,
				scopes=scopes, expires_at=state.expires_at,
			)
		except PermissionError as exc:
			self._send_text(handler, HTTPStatus.FORBIDDEN, str(exc))
			return
		except ValueError as exc:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc))
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, build_twitch_oauth_url(
			client_id, self._twitch_install_redirect_uri(handler), state_token, scopes
		))

	def _serve_twitch_install_callback(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler, permission="integrations.manage")
		if session is None:
			return
		state_token = (query.get("state") or [""])[0].strip()
		state = parse_twitch_install_state(
			self.settings.dashboard_session_secret or "", state_token
		)
		code = (query.get("code") or [""])[0].strip()
		provider_error = (query.get("error_description") or query.get("error") or [""])[0].strip()
		if provider_error:
			self._redirect(handler, f"/integrations?error={quote(provider_error)}&resume={quote(state_token)}")
			return
		if (
			state is None or not code or state.operator_id != session.user_id
			or state.community_id != session.community_id
		):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid Twitch installation state")
			return
		if not (
			self.settings.twitch_client_id and self.settings.twitch_client_secret
			and self.settings.credential_encryption_key
		):
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Twitch OAuth is not configured")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not twitch_install_intent_is_pending(
				connection, nonce=state.nonce, operator_id=int(session.user_id),
				community_id=state.community_id, broadcaster_login=state.broadcaster_login,
			):
				self._send_text(handler, HTTPStatus.BAD_REQUEST, "Twitch installation state was already used")
				return
		finally:
			connection.close()
		try:
			grant = exchange_twitch_code_for_tokens(
				self.settings.twitch_client_id, self.settings.twitch_client_secret,
				self._twitch_install_redirect_uri(handler), code,
			)
			token_manager = TwitchTokenManager(
				initial_access_token=grant.access_token,
				refresh_token=grant.refresh_token,
				client_id=self.settings.twitch_client_id,
				client_secret=self.settings.twitch_client_secret,
			)
			validation = token_manager.validate_token()
			if validation.login.casefold() != state.broadcaster_login:
				raise PermissionError("Twitch authorization belongs to a different broadcaster")
		except (TwitchAuthError, PermissionError) as exc:
			self._redirect(handler, f"/integrations?error={quote(str(exc))}&resume={quote(state_token)}")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			installation_id = complete_twitch_install_intent(
				connection, nonce=state.nonce, operator_id=int(session.user_id),
				community_id=state.community_id, broadcaster_login=state.broadcaster_login,
				broadcaster_id=validation.user_id, access_token=grant.access_token,
				refresh_token=grant.refresh_token, scopes=grant.scopes,
				encryption_key=self.settings.credential_encryption_key,
			)
			checked_at = datetime.now(timezone.utc).isoformat()
			if not self.settings.twitch_eventsub_secret or not self.settings.twitch_eventsub_callback_url:
				update_installation_health(
					connection, community_id=state.community_id,
					installation_id=installation_id, health_status="degraded",
					checked_at=checked_at,
					error="EventSub callback is not configured; configure it and reconnect Twitch.",
				)
			else:
				try:
					TwitchEventSubControlPlane(
						token_manager=token_manager,
						callback_url=self.settings.twitch_eventsub_callback_url,
						secret=self.settings.twitch_eventsub_secret,
						community_id=state.community_id,
					).reconcile(connection, [
						{
							"type": "stream.online", "version": "1",
							"condition": {"broadcaster_user_id": validation.user_id},
						},
						{
							"type": "stream.offline", "version": "1",
							"condition": {"broadcaster_user_id": validation.user_id},
						},
					])
					update_installation_health(
						connection, community_id=state.community_id,
						installation_id=installation_id, health_status="healthy",
						checked_at=checked_at,
					)
				except (RuntimeError, ValueError) as exc:
					update_installation_health(
						connection, community_id=state.community_id,
						installation_id=installation_id, health_status="degraded",
						checked_at=checked_at, error=f"EventSub setup failed: {exc}",
						reconnect_attempted=True,
					)
		finally:
			connection.close()
		self._redirect(
			handler,
			f"/integrations?status=Twitch%20installation%20pending&installation_id={installation_id}",
		)

	def _serve_logout(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._read_session(handler)
		if session is not None and session.user_id.isdigit():
			self._audit_operator_action(int(session.user_id), "auth.logout", "operator_account", int(session.user_id), {})
		self._redirect(
			handler,
			"/login",
			cookies=(f"qbot4k_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{self._secure_cookie_suffix(handler)}",),
		)

	def _secure_cookie_suffix(self, handler: BaseHTTPRequestHandler) -> str:
		scheme = (handler.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().casefold()
		if scheme == "https" or str(self.settings.discord_oauth_redirect_uri or "").casefold().startswith("https://"):
			return "; Secure"
		return ""

	def _serve_public_home(self, handler: BaseHTTPRequestHandler) -> None:
		body = """<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>QBot4K | Community operations</title>
<style>
:root{color-scheme:dark;--ink:#eef2f3;--muted:#aeb8bc;--line:#344046;--signal:#f0a629;--panel:#171d20}
*{box-sizing:border-box}body{margin:0;background:#0d1113;color:var(--ink);font-family:"IBM Plex Sans", "Segoe UI",sans-serif;letter-spacing:0}
a{color:inherit}.mast{min-height:82vh;padding:28px clamp(22px,6vw,92px);display:grid;align-content:space-between;background:linear-gradient(115deg,rgba(13,17,19,.97),rgba(13,17,19,.72)),url('https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&w=1800&q=85') center/cover}
nav{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:20px}.brand{font-weight:800;font-size:20px}.actions{display:flex;gap:12px;align-items:center}.button{display:inline-flex;align-items:center;min-height:42px;padding:0 18px;border:1px solid var(--signal);background:var(--signal);color:#121619;text-decoration:none;font-weight:750}.button.secondary{background:transparent;color:var(--ink);border-color:var(--line)}
.hero{max-width:850px;padding:72px 0 36px}.eyebrow{text-transform:uppercase;color:var(--signal);font-weight:750;font-size:13px}.hero h1{font-family:Georgia,serif;font-size:clamp(46px,7vw,96px);line-height:.96;margin:20px 0;max-width:12ch}.hero p{font-size:clamp(18px,2vw,24px);line-height:1.5;color:var(--muted);max-width:680px}.grid{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line);border-left:1px solid var(--line);background:#101517}.grid article{padding:36px clamp(22px,4vw,54px);border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.grid h2{font-size:22px;margin:0 0 12px}.grid p{color:var(--muted);line-height:1.65;margin:0}.trust{padding:50px clamp(22px,6vw,92px);background:var(--panel);display:flex;justify-content:space-between;gap:30px;align-items:center}.trust p{color:var(--muted);max-width:720px;line-height:1.6}
@media(max-width:760px){.mast{min-height:88vh}.actions .secondary{display:none}.grid{grid-template-columns:1fr}.trust{align-items:flex-start;flex-direction:column}.hero{padding-top:52px}.hero h1{font-size:48px}}
</style>
</head>
<body>
<main>
<section class='mast'>
<nav><div class='brand'>QBot4K</div><div class='actions'><a class='button secondary' href='/login'>Operator login</a><a class='button' href='/login'>Link Discord</a></div></nav>
<div class='hero'><p class='eyebrow'>Discord + Twitch operations</p><h1>Control for communities in motion.</h1><p>Tenant-isolated moderation, investigations, onboarding, announcements, and live operations for teams that need evidence before action.</p></div>
</section>
<section class='grid' aria-label='Platform capabilities'>
<article><h2>Moderation command</h2><p>Review queues, reversible sanctions, bulk previews, appeals, and provider-confirmed outcomes.</p></article>
<article><h2>Discord and Twitch</h2><p>Installation-bound ingestion and actions with scoped OAuth, EventSub verification, and connector health.</p></article>
<article><h2>Operational intelligence</h2><p>Explainable signals, cases, evidence, campaigns, and shift handoffs in one tenant-safe workspace.</p></article>
</section>
<section class='trust'><div><p class='eyebrow'>Privacy and security</p><h2>Isolation is the default.</h2><p>Community data remains scoped to its installation and authorized operators. Credentials are encrypted at rest, access changes are audited, and automated enforcement begins in shadow mode.</p></div><div class='actions'><a class='button secondary' href='/privacy'>Privacy</a><a class='button' href='/login'>Request invite</a></div></section>
</main>
</body>
</html>"""
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_dashboard(self, handler: BaseHTTPRequestHandler,
	                     query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		status_message = (query.get("status") or [""])[0].strip()
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			overview = load_overview_snapshot(
				connection, community_id=session.community_id
			)
		finally:
			connection.close()
		status_html = f"<p class='status-banner'>{
    self._escape(status_message)}</p>" if status_message else ""
		admin_actions = (
			"<form method='post' action='/dashboard/go-live'>"
			+ "<button type='submit'>Go Live</button>"
			+ "</form>"
			+ "<form method='post' action='/dashboard/restart' "
			+ "onsubmit=\"return window.confirm('Restart the QBot4K service now?');\">"
			+ "<button type='submit'>Restart Bot</button>"
			+ "</form>"
			+ "<form method='post' action='/dashboard/reset-database' "
			+ "onsubmit=\"const value=window.prompt('Type RESET to permanently erase all QBot4K database data.'); if(value !== 'RESET') return false; this.elements.confirmation.value=value; return window.confirm('This cannot be undone. Reset the entire database?');\">"
			+ "<input type='hidden' name='confirmation' value=''>"
			+ "<button class='danger' type='submit'>Reset Database</button>"
			+ "</form>"
			if session.role in {"admin", "owner"}
			else ""
		)
		toolbar_html = f"<div class='toolbar'>{admin_actions}{status_html}</div>"
		connector_status = self._render_overview_connector_status()
		body = self._render_page(
			"Dashboard",
			session,
			f"<section class='hero'><div><p class='eyebrow'>Overview</p><h1>QBot4K dashboard</h1><p class='lede'>Messages processed: {overview.messages_total}. Open reviews: {overview.open_reviews}. Pending actions: {overview.pending_actions}.</p></div></section>"  # noqa: E501
			+ toolbar_html
			+ connector_status
			+ self._render_metric_grid(overview),
		)
		self._send_html(handler, HTTPStatus.OK, body)

	@staticmethod
	def _sigma_rating(history: list[object]) -> float | None:
		if not history:
			return None

		total_minutes = 0
		uptime_minutes = 0

		for bucket in history:
			try:
				status = str(bucket["status"] or "").strip().casefold()
			except (KeyError, IndexError, TypeError):
				continue

			if status in {"down"}:
				continue

			total_minutes += 1

			if status in {"ready"}:
				uptime_minutes += 1

		if total_minutes == 0:
			return None

		yield_rate = uptime_minutes / total_minutes
		yield_rate = min(max(yield_rate, 1e-9), 1 - 1e-9)

		return NormalDist().inv_cdf(yield_rate) + 1.5

	def _serve_system_health(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		database_state = database_health(self.settings.database_path)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			reliability_history = {
				name: list_service_reliability_buckets(connection, service_name=name, limit=1440)
				for name in ("system", "web", "jobs", "twitch", "discord")
			}
		finally:
			connection.close()
		now = datetime.now(timezone.utc)
		services = ("web", "jobs", "twitch", "discord")
		rows = []
		for service_name in services:
			status = self._service_status(service_name)
			started_at = self.service_started_at.get(service_name)
			uptime_seconds = self._uptime_seconds(started_at, now)
			rows.append(
				"<tr>"
				+ f"<td>{self._escape(service_name)}</td>"
				+ f"<td>{self._render_status_pill(status)}</td>"
				+ f"<td>{self._escape(self._format_uptime(uptime_seconds))}</td>"
				+ f"<td>{self._escape(started_at or 'n/a')}</td>"
				+ "</tr>"
			)

		app_uptime_seconds = self._uptime_seconds(self.app_started_at, now)
		overall_status = self._overall_status(database_state, services)
		reliability_sections = []
		for service_name in ("system",) + services:
			status = overall_status if service_name == "system" else self._service_status(service_name)
			history = reliability_history.get(service_name, [])
			outages = self._summarize_outages(history)
			sigma = self._sigma_rating(history)

			sigma_label = (
                f"{sigma:.2f}σ"
                if sigma is not None
                else "n/a"
            )

			reliability_sections.append(
				"<section class='card'>"
				+ f"<h2>{self._escape(service_name.capitalize())} reliability</h2>"
				+ f"<div class='status-row'>{self._render_status_pill(status)}"
                + f"<span class='sigma-rating' title='Calculated from the displayed reliability history'>{self._escape(sigma_label)}</span>"
                + "<span class='muted'>Each bar is 1 minute. Green = uptime, red = downtime.</span></div>"
				+ self._render_reliability_graph(service_name, history)
				+ self._render_outage_table(outages)
				+ "</section>"
			)

		body = self._render_page(
			"Health",
			session,
			"<section class='hero'><div><p class='eyebrow'>Health</p><h1>System health</h1>"
			+ f"<div class='status-row'><span class='muted'>Overall status:</span>{self._render_status_pill(overall_status)}</div>"
			+ f"<p class='lede'>App uptime: {self._escape(self._format_uptime(app_uptime_seconds))}.</p>"
			+ "</div></section>"
			+ "<section class='card'>"
			+ "<h2>Database</h2>"
			+ "<div class='table-scroll'><table class='table'><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>"
			+ f"<tr><td>Status</td><td>{self._render_status_pill(str(database_state.get('status') or 'unknown'))}</td></tr>"
			+ f"<tr><td>Path</td><td>{self._escape(str(database_state.get('path') or ''))}</td></tr>"
			+ f"<tr><td>Table count</td><td>{int(database_state.get('table_count') or 0)}</td></tr>"
			+ f"<tr><td>Journal mode</td><td>{self._escape(str(database_state.get('journal_mode') or 'unknown'))}</td></tr>"
			+ "</tbody></table></div>"
			+ "</section>"
			+ "<section class='card'>"
			+ "<h2>Services</h2>"
			+ "<div class='table-scroll'><table class='table'><thead><tr><th>Service</th><th>Status</th><th>Uptime</th><th>Started at</th></tr></thead><tbody>"
			+ "".join(rows)
			+ "</tbody></table></div>"
			+ "</section>"
			+ "".join(reliability_sections),
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_dashboard_go_live(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		try:
			announcements = send_manual_twitch_live_announcements(self.settings)
		except Exception as exc:
			self._log_exception("manual go-live failed", exc)
			self._redirect(handler, "/dashboard?status=Go%20Live%20failed")
			return
		if announcements <= 0:
			self._redirect(handler, "/dashboard?status=Go%20Live%20sent%200%20pings")
			return
		self._redirect(handler, f"/dashboard?status={quote(f'Go Live sent {announcements} pings')}")

	def _serve_dashboard_restart(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		service_name = self.settings.systemd_service_name
		logging.getLogger("qbot4k.dashboard").warning(
			"systemd restart requested operator_user_id=%s service=%s",
			session.user_id,
			service_name,
		)
		self._schedule_systemd_restart(service_name)
		self._redirect(
			handler,
			f"/dashboard?status={quote(f'Restart requested for {service_name}')}",
		)

	@staticmethod
	def _schedule_systemd_restart(service_name: str) -> None:
		def _delayed_restart() -> None:
			time.sleep(0.5)
			try:
				_restart_systemd_service(service_name)
			except Exception:
				logging.getLogger("qbot4k.dashboard").exception(
					"systemd restart failed service=%s",
					service_name,
				)

		threading.Thread(
			target=_delayed_restart,
			name="dashboard-systemd-restart",
			daemon=True,
		).start()

	def _serve_dashboard_reset_database(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		confirmation = (form.get("confirmation") or [""])[0].strip()
		if not hmac.compare_digest(confirmation, "RESET"):
			self._redirect(handler, "/dashboard?status=Database%20reset%20cancelled")
			return

		connection = connect_database(self.settings.database_path)
		try:
			report = reset_database(connection)
		except Exception as exc:
			self._log_exception("database reset failed", exc)
			self._redirect(handler, "/dashboard?status=Database%20reset%20failed")
			return
		finally:
			connection.close()

		logging.getLogger("qbot4k.dashboard").warning(
			"database reset completed operator_user_id=%s tables=%s rows=%s",
			session.user_id,
			report["tables_cleared"],
			report["rows_deleted"],
		)
		status = quote(f"Database reset complete; deleted {report['rows_deleted']} rows")
		self._redirect(
			handler,
			f"/dashboard?status={status}",
		)

	def _serve_users(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		search = (query.get("q") or [""])[0]
		sort_by, sort_dir = self._normalize_user_sort(
			(query.get("sort") or ["score"])[0],
			(query.get("dir") or [""])[0],
		)
		selected_user_id_raw = (query.get("link_user_id") or [""])[0].strip()
		link_status = (query.get("link_status") or [""])[0].strip()
		selected_user_id: int | None = None
		if selected_user_id_raw:
			try:
				selected_user_id = int(selected_user_id_raw)
			except ValueError:
				selected_user_id = None
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			users = search_users(
				connection, query=search, sort_by=sort_by, sort_dir=sort_dir,
				community_id=session.community_id,
			)
		finally:
			connection.close()
		selected_user = next((item for item in users if item.user_id == selected_user_id), None)

		def _users_query(**overrides: object) -> str:
			params: dict[str, str] = {
				"q": search,
				"sort": sort_by,
				"dir": sort_dir,
			}
			if selected_user_id is not None:
				params["link_user_id"] = str(selected_user_id)
			for key, value in overrides.items():
				if value is None:
					params.pop(key, None)
				else:
					params[key] = str(value)
			return urlencode(params)

		def _sort_header(label: str, key: str) -> str:
			is_current = sort_by == key
			if is_current:
				next_dir = "desc" if sort_dir == "asc" else "asc"
				indicator = " (asc)" if sort_dir == "asc" else " (desc)"
			else:
				next_dir = "asc" if key == "name" else "desc"
				indicator = ""
			return f"<a href='/users?{_users_query(sort=key, dir=next_dir)}'>{self._escape(label + indicator)}</a>"

		sticky_panel = ""
		if selected_user is not None:
			status_html = f"<p class='muted'>{self._escape(link_status)}</p>" if link_status else ""
			sticky_panel = (
				"<section class='card sticky-link-panel'><div><p class='eyebrow'>Link Target</p>"
				+ f"<h2>{self._escape(selected_user.primary_display_name)}</h2>"
				+ "<p class='lede'>Tag other usernames to link their accounts to this user.</p>"
				+ status_html
				+ "<form class='search' method='post' action='/users/link'>"
				+ f"<input type='hidden' name='selected_user_id' value='{selected_user.user_id}'>"
				+ f"<input type='hidden' name='q' value='{self._escape(search)}'>"
				+ f"<input type='hidden' name='sort' value='{self._escape(sort_by)}'>"
				+ f"<input type='hidden' name='dir' value='{self._escape(sort_dir)}'>"
				+ "<input name='usernames' placeholder='username1, username2' required>"
				+ "<select name='platform'><option value='any'>Any platform</option><option value='discord'>Discord</option><option value='twitch'>Twitch</option></select>"
				+ "<button type='submit'>Tag Link</button>"
				+ "</form></div></section>"
			)
		rows = "".join(
			f"<tr><td><a href='/users/{item.user_id}'>{self._escape(item.primary_display_name)}</a></td><td>{item.current_reputation_score}</td><td>{'yes' if item.candidate_flag else 'no'}</td><td>{item.account_count}</td><td>{item.message_count}</td><td><a href='/users?{_users_query(link_user_id=item.user_id)}'>Link</a></td></tr>"
			for item in users
		)
		headers = (
			"<tr>"
			+ f"<th>{_sort_header('Name', 'name')}</th>"
			+ f"<th>{_sort_header('Score', 'score')}</th>"
			+ f"<th>{_sort_header('PowerUser', 'poweruser')}</th>"
			+ f"<th>{_sort_header('Accounts', 'accounts')}</th>"
			+ f"<th>{_sort_header('Messages', 'messages')}</th>"
			+ "<th>Link</th>"
			+ "</tr>"
		)
		body = self._render_page(
			"Users",
			session,
			"<section class='hero'><div><p class='eyebrow'>Users</p><h1>Canonical profiles</h1><p class='lede'>Search linked accounts, score bands, and recent activity.</p></div></section>"  # noqa: E501
			+ sticky_panel
			+ f"<form class='search' method='get'><input type='hidden' name='sort' value='{self._escape(sort_by)}'><input type='hidden' name='dir' value='{self._escape(sort_dir)}'><input name='q' value='{self._escape(search)}' placeholder='Search users'><button type='submit'>Search</button></form>"
			+ f"<div class='table-scroll'><table class='table'><thead>{headers}</thead><tbody>{rows or '<tr><td colspan=6>No users found</td></tr>'}</tbody></table></div>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_users_link(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return

		selected_user_id_raw = (form.get("selected_user_id") or [""])[0].strip()
		search = (form.get("q") or [""])[0].strip()
		sort_by, sort_dir = self._normalize_user_sort(
			(form.get("sort") or ["score"])[0],
			(form.get("dir") or [""])[0],
		)

		def _users_url(*, selected: int | None = None, status: str | None = None) -> str:
			params: dict[str, str] = {
				"q": search,
				"sort": sort_by,
				"dir": sort_dir,
			}
			if selected is not None:
				params["link_user_id"] = str(selected)
			if status:
				params["link_status"] = status
			return f"/users?{urlencode(params)}"

		platform = (form.get("platform") or ["any"])[0].strip().casefold()
		if platform not in {"any", "discord", "twitch"}:
			platform = "any"
		try:
			selected_user_id = int(selected_user_id_raw)
		except ValueError:
			self._redirect(handler, _users_url(status="Invalid selected user"))
			return

		raw_usernames = (form.get("usernames") or [""])[0]
		usernames = []
		for chunk in raw_usernames.replace("\n", ",").split(","):
			cleaned = chunk.strip()
			if not cleaned:
				continue
			if cleaned.casefold().endswith(" (unlinked)"):
				cleaned = cleaned[: -len(" (unlinked)")].strip()
			if cleaned:
				usernames.append(cleaned)
		if not usernames:
			self._redirect(handler, _users_url(selected=selected_user_id, status="No usernames provided"))
			return

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if selected_user_id < 0:
				target_account_id = -selected_user_id
				target_account = connection.execute(
					"""
					SELECT id, platform, platform_user_id, username, user_id
					FROM platform_accounts
					WHERE id = ? AND EXISTS (
						SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
						AND messages.community_id=?
					)
					""",
					(target_account_id, session.community_id),
				).fetchone()
				if target_account is None:
					self._redirect(handler, _users_url(status="Selected user not found"))
					return

				if target_account[4] is None:
					selected_user_id = create_canonical_user(
						connection,
						primary_display_name=str(target_account[3]),
					)
					link_platform_account(
						connection,
						platform=str(target_account[1]),
						platform_user_id=str(target_account[2]),
						user_id=selected_user_id,
						operator_id=int(session.user_id),
					)
				else:
					selected_user_id = int(target_account[4])

			if not user_is_visible(
				connection, selected_user_id, community_id=session.community_id
			):
				self._redirect(handler, _users_url(status="Selected user not found"))
				return

			linked_count = 0
			linked_account_count = 0
			missing_usernames: list[str] = []
			for username in usernames:
				accounts = []
				if platform == "any":
					accounts = connection.execute(
						"""
						SELECT platform, platform_user_id
						FROM platform_accounts
						WHERE username = ? COLLATE NOCASE AND EXISTS (
							SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
							AND messages.community_id=?
						)
						ORDER BY updated_at DESC, id DESC
						""",
						(username, session.community_id),
					).fetchall()
				else:
					accounts = connection.execute(
						"""
						SELECT platform, platform_user_id
						FROM platform_accounts
						WHERE username = ? COLLATE NOCASE AND platform = ? AND EXISTS (
							SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
							AND messages.community_id=?
						)
						ORDER BY updated_at DESC, id DESC
						""",
						(username, platform, session.community_id),
					).fetchall()

				if not accounts:
					accounts = connection.execute(
						"""
						SELECT platform_accounts.platform, platform_accounts.platform_user_id
						FROM users
						INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
						WHERE users.primary_display_name = ? COLLATE NOCASE
						  AND EXISTS (
							  SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
							  AND messages.community_id=?
						  )
						""",
						(username, session.community_id),
					).fetchall()

				if not accounts:
					missing_usernames.append(username)
					continue

				linked_for_username = 0
				for account in accounts:
					try:
						link_platform_account(
							connection,
							platform=str(account[0]),
							platform_user_id=str(account[1]),
							user_id=selected_user_id,
							operator_id=int(session.user_id),
						)
						linked_account_count += 1
						linked_for_username += 1
					except ValueError:
						continue
				if linked_for_username <= 0:
					missing_usernames.append(username)
				else:
					linked_count += 1
		finally:
			connection.close()

		status_message = f"Linked {linked_count} username(s), {linked_account_count} account(s)."
		if missing_usernames:
			status_message += f" Missing: {', '.join(missing_usernames[:3])}"
			if len(missing_usernames) > 3:
				status_message += ", ..."
		self._redirect(handler, _users_url(selected=selected_user_id, status=status_message))

	def _serve_users_unlink(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return

		user_id_raw = (form.get("user_id") or [""])[0].strip()
		platform_account_id_raw = (form.get("platform_account_id") or [""])[0].strip()
		confirmation = (form.get("confirmation") or [""])[0].strip()
		try:
			user_id = int(user_id_raw)
			platform_account_id = int(platform_account_id_raw)
		except ValueError:
			self._redirect(handler, "/users?link_status=" + quote("Invalid unlink request"))
			return

		def _user_url(status: str) -> str:
			return f"/users/{user_id}?account_status={quote(status)}"

		if user_id < 0 or confirmation != "UNLINK":
			self._redirect(handler, _user_url("Unlink confirmation failed"))
			return

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			account = connection.execute(
				"""
				SELECT platform, platform_user_id, username
				FROM platform_accounts
				WHERE id = ? AND user_id = ? AND EXISTS (
					SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
					AND messages.community_id=?
				)
				""",
				(platform_account_id, user_id, session.community_id),
			).fetchone()
			if account is None:
				self._redirect(handler, _user_url("Platform account does not belong to this user"))
				return

			unlink_platform_account(
				connection,
				platform=str(account[0]),
				platform_user_id=str(account[1]),
				operator_id=int(session.user_id),
			)
			username = str(account[2])
			platform = str(account[0])
		finally:
			connection.close()

		self._redirect(handler, _user_url(f"Unlinked {platform} account {username}"))

	def _serve_user_messages(
		self,
		handler: BaseHTTPRequestHandler,
		path: str,
		query: Mapping[str, list[str]],
	) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		moderation_status_message = (query.get("mod_status") or [""])[0].strip()
		account_status_message = (query.get("account_status") or [""])[0].strip()
		parts = [part for part in path.split("/") if part]
		if len(parts) != 2:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")
			return
		try:
			user_id = int(parts[1])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid user id")
			return

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			lifecycle_filter = (query.get("lifecycle") or ["all"])[0].strip().casefold()
			lifecycle_types = self._lifecycle_event_types(lifecycle_filter)
			users = search_users(connection, limit=500, community_id=session.community_id)
			selected_user = next((item for item in users if item.user_id == user_id), None)
			recent_messages = list_recent_user_messages(
				connection, user_id, community_id=session.community_id
			)
			platform_accounts = list_user_platform_accounts(
				connection, user_id, community_id=session.community_id
			)
			moderation_status = get_user_moderation_status(
				connection, user_id, community_id=session.community_id
			)
			recent_moderation_actions = list_recent_user_moderation_actions(
				connection, user_id, community_id=session.community_id
			)
			lifecycle_events = list_user_lifecycle_events(
				connection, user_id, community_id=session.community_id,
				event_types=lifecycle_types,
			)
			derived_signals = list_user_derived_signals(
				connection, user_id, community_id=session.community_id
			) if user_id >= 0 else []
			social_score = get_current_social_score(connection, user_id) if user_id >= 0 else None
		finally:
			connection.close()

		if selected_user is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "User not found")
			return

		message_rows = "".join(
			f"<tr><td>{self._escape(item.sent_at)}</td><td>{self._escape(item.platform)}</td><td>{self._escape(item.channel_id)}</td><td>{self._render_message_with_attachments(item.content_raw, item.attachment_urls)}</td></tr>"
			for item in recent_messages
		)
		action_rows = "".join(
			f"<tr><td>{self._escape(item.created_at)}</td><td>{self._escape(item.platform)}</td><td>{self._escape(item.target_username)}</td><td>{self._escape(item.action_type)}</td><td>{self._escape(item.status)}</td><td>{self._escape(item.reason or '')}</td></tr>"
			for item in recent_moderation_actions
		)
		lifecycle_rows = "".join(
			f"<tr><td>{self._escape(item.occurred_at)}</td>"
			f"<td>{self._escape(item.summary)}</td>"
			f"<td>{self._escape(item.detail or '')}</td></tr>"
			for item in lifecycle_events
		)
		signal_rows = "".join(
			"<tr>"
			+ f"<td>{self._escape(signal.label)}</td>"
			+ f"<td>{self._escape(self._format_signal_value(signal.signal_key, signal.value))}</td>"
			+ f"<td>{signal.confidence * 100:.0f}%</td>"
			+ f"<td>{signal.evidence_count}</td>"
			+ f"<td>{self._escape(signal.window_start or 'n/a')} → {self._escape(signal.window_end or 'n/a')}</td>"
			+ f"<td>v{signal.analyzer_version}</td>"
			+ "</tr>"
			for signal in derived_signals
		)
		score_component_rows = "" if social_score is None else "".join(
			"<tr>"
			+ f"<td>{self._escape(component.label)}</td>"
			+ f"<td>{component.raw_value:.3g}</td>"
			+ f"<td>{component.weight:+.1f}</td>"
			+ f"<td>{component.contribution:+.1f}</td>"
			+ f"<td>{component.confidence * 100:.0f}%</td>"
			+ f"<td>{component.evidence_count}</td>"
			+ "</tr>"
			for component in social_score.components
		)
		account_options = "".join(
			f"<option value='{item.platform_account_id}'>{self._escape(item.platform)} · {self._escape(item.username)} ({self._escape(item.platform_user_id)})</option>"
			for item in platform_accounts
		)
		linked_account_rows = "".join(
			"<tr>"
			+ f"<td>{self._escape(item.platform)}</td>"
			+ f"<td>{self._escape(item.username)}</td>"
			+ f"<td>{self._escape(item.platform_user_id)}</td>"
			+ (
				"<td><form method='post' action='/users/unlink' "
				+ "onsubmit=\"return window.confirm('Unlink this platform account? Its messages and evidence will be preserved.');\">"
				+ f"<input type='hidden' name='user_id' value='{user_id}'>"
				+ f"<input type='hidden' name='platform_account_id' value='{item.platform_account_id}'>"
				+ "<input type='hidden' name='confirmation' value='UNLINK'>"
				+ "<button class='danger' type='submit'>Unlink</button></form></td>"
				if session.role in {"admin", "owner"} and user_id >= 0
				else "<td><span class='muted'>No action</span></td>"
			)
			+ "</tr>"
			for item in platform_accounts
		)
		lifecycle_options = "".join(
			f"<option value='{value}'{' selected' if lifecycle_filter == value else ''}>{label}</option>"
			for value, label in (
				("all", "All events"), ("membership", "Joins and leaves"),
				("roles", "Role changes"), ("verification", "Verification"),
				("notes", "Operator notes"), ("moderation", "Warnings and sanctions"),
			)
		)
		account_notice = (
			f"<p class='status-banner'>{self._escape(account_status_message)}</p>" if account_status_message else ""
		)
		moderation_form = ""
		if platform_accounts:
			moderation_form = (
				f"<form class='search' method='post' action='/users/{user_id}/moderation'>"
				+ "<select name='target_platform_account_id' required>"
				+ account_options
				+ "</select>"
				+ "<select name='action_type'><option value='warn'>warn</option><option value='timeout'>timeout</option><option value='ban'>ban</option><option value='review'>review</option></select>"
				+ "<input name='confirmation' placeholder='Type PERMANENT BAN for bans'>"
				+ "<input name='reason' placeholder='Reason (required)' required>"
				+ "<button type='submit'>Apply Action</button>"
				+ "</form>"
			)
		else:
			moderation_form = "<p class='muted'>No linked platform accounts available for moderation.</p>"
		moderation_notice = (
			f"<p class='status-banner'>{self._escape(moderation_status_message)}</p>" if moderation_status_message else ""
		)
		body = self._render_page(
			"User Messages",
			session,
			"<section class='hero'><div><p class='eyebrow'>Users</p>"
			+ f"<h1>{self._escape(selected_user.primary_display_name)}</h1>"
			+ "<p class='lede'>Recent messages from this profile.</p>"
			+ "</div></section>"
			+ "<p><a href='/users'>&larr; Back to users</a></p>"
			+ "<section class='card'>"
			+ "<h2>Profile summary</h2>"
			+ f"<div class='grid'><div class='metric'><div class='label'>Intelligence score (Reputation)</div><div class='value'>{selected_user.current_reputation_score}</div></div><div class='metric'><div class='label'>Evidence confidence</div><div class='value'>{(social_score.confidence if social_score else 0.0) * 100:.0f}%</div></div><div class='metric'><div class='label'>Score band</div><div class='value'>{self._escape(social_score.band if social_score else 'unscored')}</div></div><div class='metric'><div class='label'>Power User</div><div class='value'>{'yes' if selected_user.candidate_flag else 'no'}</div></div><div class='metric'><div class='label'>Accounts</div><div class='value'>{selected_user.account_count}</div></div><div class='metric'><div class='label'>Messages</div><div class='value'>{selected_user.message_count}</div></div></div>"
			+ "</section>"
			+ "<section class='card'>"
			+ "<h2>Linked accounts</h2>"
			+ account_notice
			+ "<p class='lede'>Unlinking detaches an identity from this profile. Historical messages, evidence, and audit records are preserved.</p>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Platform</th><th>Username</th><th>Platform user ID</th><th>Action</th></tr></thead><tbody>{linked_account_rows or '<tr><td colspan=4>No linked accounts</td></tr>'}</tbody></table></div>"
			+ "</section>"
			+ "<section class='card'><h2>Member lifecycle</h2>"
			+ "<p class='lede'>Community membership, role, verification, notes, warnings, and sanctions in chronological order.</p>"
			+ f"<form method='get' class='compact-form'><select name='lifecycle'>{lifecycle_options}</select><button type='submit'>Filter</button>"
			+ f"<a href='/users/{user_id}/lifecycle.csv?{urlencode({'lifecycle': lifecycle_filter})}'>Export CSV</a></form>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>At</th><th>Event</th><th>Details</th></tr></thead><tbody>{lifecycle_rows or '<tr><td colspan=3>No lifecycle events found</td></tr>'}</tbody></table></div>"
			+ "</section>"
			+ "<section class='card'>"
			+ f"<h2>Score explanation</h2><p class='lede'>Model v{social_score.model_version if social_score else 'n/a'} recalculates the materialized score from versioned evidence. Contributions are bounded and auditable.</p>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Component</th><th>Raw</th><th>Weight</th><th>Contribution</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody>{score_component_rows or '<tr><td colspan=6>No score calculation recorded</td></tr>'}</tbody></table></div>"
			+ "</section>"
			+ "<section class='card'>"
			+ "<h2>Derived signals</h2>"
			+ "<p class='lede'>Persistent, versioned measurements derived from this profile's accumulated evidence.</p>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Signal</th><th>Value</th><th>Confidence</th><th>Evidence</th><th>Window</th><th>Analyzer</th></tr></thead><tbody>{signal_rows or '<tr><td colspan=6>No derived signals calculated</td></tr>'}</tbody></table></div>"
			+ "</section>"
			+ "<section class='card'>"
			+ "<h2>Moderation status</h2>"
			+ moderation_notice
			+ f"<div class='grid'><div class='metric'><div class='label'>Open reviews</div><div class='value'>{moderation_status.open_reviews}</div></div><div class='metric'><div class='label'>Pending actions</div><div class='value'>{moderation_status.pending_actions}</div></div><div class='metric'><div class='label'>Completed actions</div><div class='value'>{moderation_status.completed_actions}</div></div><div class='metric'><div class='label'>Total actions</div><div class='value'>{moderation_status.recent_actions}</div></div></div>"
			+ "<p class='lede'>Operators can record moderation actions directly from this user page.</p>"
			+ moderation_form
			+ "</section>"
			+ f"<table><thead><tr><th>Action At</th><th>Platform</th><th>Target</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>{action_rows or '<tr><td colspan=6>No moderation actions found</td></tr>'}</tbody></table>"
			+ f"<table><thead><tr><th>Sent</th><th>Platform</th><th>Channel</th><th>Message</th></tr></thead><tbody>{message_rows or '<tr><td colspan=4>No messages found</td></tr>'}</tbody></table>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

	@staticmethod
	def _lifecycle_event_types(filter_name: str) -> tuple[str, ...]:
		return {
			"all": (),
			"membership": ("member.joined", "member.left"),
			"roles": ("member.roles_changed",),
			"verification": ("verification",),
			"notes": ("note",),
			"moderation": ("moderation", "moderation.ban_added", "moderation.ban_removed"),
		}.get(filter_name, ())

	def _serve_user_lifecycle_export(
		self, handler: BaseHTTPRequestHandler, path: str, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler, permission="exports.create")
		if session is None:
			return
		parts = [part for part in path.split("/") if part]
		try:
			user_id = int(parts[1])
		except (IndexError, ValueError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid user id")
			return
		filter_name = (query.get("lifecycle") or ["all"])[0].strip().casefold()
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not user_is_visible(connection, user_id, community_id=session.community_id):
				self._send_text(handler, HTTPStatus.NOT_FOUND, "User not found")
				return
			events = list_user_lifecycle_events(
				connection, user_id, community_id=session.community_id, limit=200,
				event_types=self._lifecycle_event_types(filter_name),
			)
		finally:
			connection.close()
		output = io.StringIO()
		writer = csv.DictWriter(output, fieldnames=("occurred_at", "event_type", "summary", "detail"))
		writer.writeheader()
		writer.writerows(asdict(event) for event in events)
		self._audit_operator_action(
			int(session.user_id), "user.lifecycle_exported", "user", user_id,
			{"community_id": session.community_id, "filter": filter_name, "rows": len(events)},
		)
		self._send_bytes(
			handler, HTTPStatus.OK, output.getvalue().encode("utf-8"),
			"text/csv; charset=utf-8", f"qbot4k-user-{user_id}-lifecycle.csv",
		)

	def _serve_signals(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		selected_signals, sort_by, sort_dir = self._normalize_signal_query(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			items = list_signal_overview(
				connection,
				signal_keys=selected_signals,
				sort_by=sort_by,
				sort_dir=sort_dir,
				limit=500,
				community_id=session.community_id,
			)
			total = int(connection.execute(
				"""SELECT COUNT(*) FROM community_derived_signal_windows
				   WHERE community_id=? AND window_name='24h'""",
				(session.community_id,),
			).fetchone()[0])
			profile_count = int(connection.execute(
				"""SELECT COUNT(DISTINCT user_id) FROM community_derived_signal_windows
				   WHERE community_id=? AND window_name='24h'""",
				(session.community_id,),
			).fetchone()[0])
			high_risk_count = int(connection.execute(
				"""SELECT COUNT(*) FROM community_derived_signal_windows
				   WHERE community_id=? AND window_name='24h'
				     AND signal_key='risk.composite' AND value_real>=50""",
				(session.community_id,),
			).fetchone()[0])
		finally:
			connection.close()

		def _signal_query(*, sort: str | None = None, direction: str | None = None) -> str:
			params: list[tuple[str, str]] = [("signal", key) for key in selected_signals]
			params.append(("sort", sort or sort_by))
			params.append(("dir", direction or sort_dir))
			return urlencode(params)

		def _sort_header(label: str, key: str) -> str:
			is_current = sort_by == key
			next_dir = "asc" if not is_current or sort_dir == "desc" else "desc"
			indicator = " ↑" if is_current and sort_dir == "asc" else " ↓" if is_current else ""
			return f"<a href='/signals?{_signal_query(sort=key, direction=next_dir)}'>{self._escape(label + indicator)}</a>"

		options = "".join(
			f"<option value='{self._escape(key)}' {'selected' if key in selected_signals else ''}>{self._escape(label)}</option>"
			for key, label in SIGNAL_LABELS.items()
		)
		filter_form = (
			"<form class='signal-filter' method='get' action='/signals'>"
			+ "<label><h2>Signals</h2><span class='muted'>Ctrl/Cmd-click for multiple signals.</span>"
			+ f"<select name='signal' multiple size='6'>{options}</select></label>"
			+ f"<input type='hidden' name='sort' value='{self._escape(sort_by)}'>"
			+ f"<input type='hidden' name='dir' value='{self._escape(sort_dir)}'>"
			+ "<div class='row-actions'>"
			+ "<div class='signal-filter-actions'>"
			+ "<button type='submit'>Apply</button>"
			+ "<button type='button' onclick=\"window.location.href='/signals'\">Clear</button></div>"
			+ "</div>"
			+ "</form>"
		)

		rows = "".join(
			"<tr>"
			+ f"<td><a href='/users/{signal.user_id}'>{self._escape(display_name)}</a></td>"
			+ f"<td>{self._escape(signal.label)}</td>"
			+ f"<td>{self._escape(self._format_signal_value(signal.signal_key, signal.value))}</td>"
			+ f"<td>{signal.confidence * 100:.0f}%</td>"
			+ f"<td>{signal.evidence_count}</td>"
			+ f"<td>{self._escape(signal.calculated_at or '')}</td>"
			+ "</tr>"
			for display_name, signal in items
		)
		body = self._render_page(
			"Signals",
			session,
			"<section class='hero'><div><p class='eyebrow'>Intelligence</p><h1>Derived signals</h1>"
			+ "<p class='lede'>Explainable behavioral and operational measurements derived from accumulated observations.</p></div></section>"
			+ f"<div class='grid'><div class='metric'><div class='label'>Signals</div><div class='value'>{total}</div></div><div class='metric'><div class='label'>Profiles measured</div><div class='value'>{profile_count}</div></div><div class='metric'><div class='label'>High risk profiles</div><div class='value'>{high_risk_count}</div></div></div>"
			+ filter_form
			+ "<section class='card'><h2>Signal inventory</h2>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Profile</th><th>{_sort_header('Signal', 'signal')}</th><th>{_sort_header('Value', 'value')}</th><th>{_sort_header('Confidence', 'confidence')}</th><th>{_sort_header('Evidence', 'evidence')}</th><th>{_sort_header('Timestamp', 'timestamp')}</th></tr></thead><tbody>{rows or '<tr><td colspan=6>No derived signals calculated</td></tr>'}</tbody></table></div></section>",
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_user_moderation_action(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return

		parts = [part for part in path.split("/") if part]
		if len(parts) != 3 or parts[2] != "moderation":
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")
			return
		try:
			user_id = int(parts[1])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid user id")
			return

		target_platform_account_id_raw = (form.get("target_platform_account_id") or [""])[0].strip()
		action_type = (form.get("action_type") or [""])[0].strip().casefold()
		reason = (form.get("reason") or [""])[0].strip()
		if action_type not in {"warn", "timeout", "ban", "review"}:
			self._redirect(handler, f"/users/{user_id}?mod_status={quote('Invalid action type')}")
			return
		if action_type == "ban" and not hmac.compare_digest(
			(form.get("confirmation") or [""])[0].strip(), "PERMANENT BAN"
		):
			self._redirect(handler, f"/users/{user_id}?mod_status={quote('Permanent ban confirmation required')}")
			return
		if not reason:
			self._redirect(handler, f"/users/{user_id}?mod_status={quote('Reason is required')}")
			return

		try:
			target_platform_account_id = int(target_platform_account_id_raw)
		except ValueError:
			self._redirect(handler, f"/users/{user_id}?mod_status={quote('Invalid platform account')}")
			return

		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			allowed_accounts = {
				account.platform_account_id for account in list_user_platform_accounts(
					connection, user_id, community_id=session.community_id
				)
			}
			if target_platform_account_id not in allowed_accounts:
				self._redirect(handler, f"/users/{user_id}?mod_status={quote('Platform account does not belong to this user')}")
				return
			platform_row = connection.execute(
				"SELECT platform FROM platform_accounts WHERE id = ?",
				(target_platform_account_id,),
			).fetchone()
			if platform_row is None:
				self._redirect(handler, f"/users/{user_id}?mod_status={quote('Platform account not found')}")
				return

			record_moderation_action(
				connection,
				platform=str(platform_row[0]),
				message_id=None,
				target_platform_account_id=target_platform_account_id,
				action_type=action_type,
				reason=reason,
				status="completed",
				actor_type="operator",
				actor_id=int(session.user_id),
				community_id=session.community_id,
			)
		finally:
			connection.close()

		self._redirect(handler, f"/users/{user_id}?mod_status={quote(f'Moderation action {action_type} recorded')}")

	def _serve_moderation(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler, permission="moderation.queues.read")
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			queue = (query.get("queue") or ["unassigned"])[0]
			page = max(1, int((query.get("page") or ["1"])[0]))
			page_size = 25
			work_filters = {
				key: (query.get(key) or [""])[0]
				for key in ("search", "severity", "rule", "platform", "start_at", "end_at", "assignment")
			}
			work_items, work_total = list_moderation_work(
				connection, community_id=int(session.community_id), operator_id=int(session.user_id),
				queue=queue, **work_filters, limit=page_size, offset=(page - 1) * page_size,
			)
			tenant = TenantContext(int(session.community_id))
			actor = ActorAttribution("operator", int(session.user_id))
			saved_filters = list_moderation_filters(connection, tenant=tenant, actor=actor)
			reviews = list_open_reviews(connection, community_id=session.community_id)
			reports = list_member_queue(
				connection, queue_type="reports", community_id=int(session.community_id)
			)
			appeals = list_member_queue(
				connection, queue_type="appeals", community_id=int(session.community_id)
			)
			actions = list_recent_actions(connection, community_id=session.community_id)
			rules = list_moderation_rules(connection, community_id=session.community_id)
			rule_versions = connection.execute(
				"""SELECT v.id,r.name,v.version_number,v.lifecycle_state,v.impact_json,
				          v.created_by_operator_id,v.approved_by_operator_id
				   FROM moderation_rule_versions v
				   JOIN moderation_rules r ON r.id=v.moderation_rule_id
				   WHERE v.community_id=? ORDER BY v.id DESC""",
				(int(session.community_id),),
			).fetchall()
		finally:
			connection.close()
		work_rows = "".join(
			"<tr tabindex='0' class='work-row'>"
			+ f"<td>{self._escape(item.work_type.title())} #{item.item_id}</td>"
			+ f"<td>{self._escape(item.platform)}</td><td>{self._escape(item.username)}</td>"
			+ f"<td>{self._escape(item.severity)}</td><td>{self._escape(item.reason)}</td>"
			+ f"<td>{self._escape(item.summary)}</td><td>{item.sla_age_hours:.1f}h</td>"
			+ f"<td>{item.assigned_operator_id or 'Unassigned'}</td><td>{self._escape(item.status)}</td>"
			+ (f"<td><form method='post' action='/moderation/work/{item.work_type}/{item.item_id}/assign'>"
			   "<button type='submit' aria-label='Assign this work item to me'>Assign to me</button></form></td>"
			   if item.status == "open" and item.assigned_operator_id is None else "<td></td>")
			+ "</tr>" for item in work_items
		)
		filter_values = {"queue": queue, **work_filters}
		filter_query = urlencode({key: value for key, value in filter_values.items() if value})
		saved_filter_links = "".join(
			f"<a href='/moderation?{urlencode(item['filters'])}'>{self._escape(item['name'])}</a>"
			for item in saved_filters
		) or "<span>No saved filters</span>"
		previous_link = ""
		if page > 1:
			previous_link = f"<a rel='prev' href='/moderation?{filter_query}&page={page - 1}'>Previous</a>"
		next_link = ""
		if page * page_size < work_total:
			next_link = f"<a rel='next' href='/moderation?{filter_query}&page={page + 1}'>Next</a>"
		queue_tabs = "".join(
			f"<a href='/moderation?queue={name}'{' aria-current=page' if queue == name else ''}>{label}</a>"
			for name, label in (("unassigned", "Unassigned"), ("mine", "Mine"),
			                    ("escalated", "Escalated"), ("appeals", "Appeals"),
			                    ("resolved", "Resolved"), ("all", "All"))
		)
		workbench = (
			"<section class='card' id='work-queue'><h2>Work queue</h2>"
			f"<nav class='row-actions' aria-label='Work queue views'>{queue_tabs}</nav>"
			"<form method='get' action='/moderation' class='search'>"
			f"<input type='hidden' name='queue' value='{self._escape(queue)}'>"
			f"<input name='search' aria-label='Search moderation work' placeholder='Search' value='{self._escape(work_filters['search'])}'>"
			f"<input name='severity' placeholder='Severity' value='{self._escape(work_filters['severity'])}'>"
			f"<input name='rule' placeholder='Rule or reason' value='{self._escape(work_filters['rule'])}'>"
			f"<input name='platform' placeholder='Platform' value='{self._escape(work_filters['platform'])}'>"
			f"<input type='datetime-local' name='start_at' aria-label='Created after' value='{self._escape(work_filters['start_at'])}'>"
			f"<input type='datetime-local' name='end_at' aria-label='Created before' value='{self._escape(work_filters['end_at'])}'>"
			f"<select name='assignment' aria-label='Assignment'><option value=''>Any assignment</option><option value='unassigned'{' selected' if work_filters['assignment']=='unassigned' else ''}>Unassigned</option><option value='mine'{' selected' if work_filters['assignment']=='mine' else ''}>Mine</option></select>"
			"<button type='submit'>Apply filters</button></form>"
			f"<div class='row-actions' aria-label='Saved filters'>{saved_filter_links}</div>"
			"<form method='post' action='/moderation/filters' class='search'>"
			f"<input type='hidden' name='filters' value='{self._escape(json.dumps(filter_values))}'>"
			"<input name='name' required maxlength='80' placeholder='Saved filter name'>"
			"<button type='submit'>Save current filter</button></form>"
			f"<p>{work_total} work items</p><div class='table-scroll'><table class='table'><thead><tr>"
			"<th>Work</th><th>Platform</th><th>Member</th><th>Severity</th><th>Reason</th>"
			"<th>Summary</th><th>SLA age</th><th>Assignee</th><th>Status</th><th>Action</th>"
			f"</tr></thead><tbody>{work_rows or '<tr><td colspan=10>No matching work</td></tr>'}</tbody></table></div>"
			f"<nav class='row-actions' aria-label='Work queue pages'>{previous_link}<span>Page {page}</span>{next_link}</nav></section>"
			"<script>const rows=[...document.querySelectorAll('.work-row')];rows.forEach((row,index)=>row.addEventListener('keydown',event=>{if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();rows[(index+(event.key==='ArrowDown'?1:-1)+rows.length)%rows.length].focus();}}));</script>"
		)
		review_rows = "".join(
			"<tr>"
			+ f"<td><input class='bulk-target' type='checkbox' value='{item.target_platform_account_id}' aria-label='Select {self._escape(item.target_username)}'></td>"
			+ f"<td>{item.review_id}</td><td>{self._escape(item.platform)}</td><td>{self._escape(item.target_username)}</td>"
			+ f"<td>{self._escape(item.severity)}</td><td>{self._escape(item.reason_code)}</td><td>{self._escape(item.content)}</td>"
			+ f"<td><form method='post' action='/moderation/reviews/{item.review_id}/resolve' class='row-actions'>"
			+ "<select name='resolution'><option value='dismissed'>Dismiss</option><option value='confirmed'>Confirm</option><option value='escalated'>Escalate</option></select>"
			+ "<select name='action_type'><option value=''>No action</option><option value='warn'>Warn</option><option value='timeout'>Timeout</option><option value='ban'>Ban</option></select>"
			+ "<input name='confirmation' placeholder='Type PERMANENT BAN for bans'>"
			+ "<input name='duration_seconds' type='number' min='1' max='2419200' value='600' aria-label='Duration seconds'>"
			+ "<input name='note' placeholder='Analyst note'><button type='submit'>Resolve</button></form></td></tr>"
			for item in reviews
		)
		report_rows = "".join(
			"<tr>" + f"<td>{self._escape(item.username)}</td><td>{self._escape(item.severity)}</td>"
			+ f"<td>{self._escape(item.category_or_reason)}</td><td>{self._escape(item.summary)}</td>"
			+ "<td><form method='post' action='/moderation/reports/"
			+ f"{item.item_id}/resolve' class='row-actions'><select name='resolution'>"
			+ "<option value='substantiated'>Substantiate</option><option value='dismissed'>Dismiss</option>"
			+ "<option value='escalated'>Escalate</option></select><input name='note' required placeholder='Resolution note'>"
			+ "<button type='submit'>Resolve</button></form></td></tr>"
			for item in reports
		)
		appeal_rows = "".join(
			"<tr>" + f"<td>{self._escape(item.username)}</td><td>{self._escape(item.severity)}</td>"
			+ f"<td>{self._escape(item.category_or_reason)}</td><td>{self._escape(item.summary)}</td>"
			+ f"<td>{item.assigned_operator_id or 'Unassigned'}</td>"
			+ "<td><form method='post' action='/moderation/appeals/"
			+ f"{item.item_id}/resolve' class='row-actions'><select name='resolution'>"
			+ "<option value='upheld'>Uphold</option><option value='reversed'>Reverse</option>"
			+ "<option value='modified'>Modify</option></select><input name='note' required placeholder='Independent review note'>"
			+ "<button type='submit'>Resolve</button></form></td></tr>"
			for item in appeals
		)
		action_rows = "".join(
			f"<tr><td>{self._escape(item.platform)}</td><td>{self._escape(item.target_username)}</td><td>{self._escape(item.action_type)}</td><td>{self._escape(item.status)}</td><td>{self._escape(item.reason or '')}</td><td>{self._escape(item.error_message or '')}</td><td>{self._escape(item.created_at)}</td></tr>"
			for item in actions
		)
		rule_rows = "".join(
			f"<tr><td>{self._escape(item.name)}</td><td>{self._escape(item.rule_type)}</td><td><code>{self._escape(item.pattern)}</code></td>"
			f"<td>{self._escape(item.severity)}</td><td>{self._escape(item.enforcement_mode)}</td><td>{self._escape(item.auto_enforce_action or '—')}</td>"
			f"<td>{item.action_duration_seconds}</td><td>{'yes' if item.enabled else 'no'}</td></tr>"
			for item in rules
		)
		version_rows = "".join(
			f"<tr><td>{self._escape(row['name'])}</td><td>{int(row['version_number'])}</td>"
			f"<td>{self._escape(row['lifecycle_state'])}</td><td><code>{self._escape(row['impact_json'])}</code></td>"
			f"<td>{int(row['created_by_operator_id'])}</td><td>{row['approved_by_operator_id'] or 'Pending'}</td>"
			f"<td><form method='post' action='/moderation/rule-versions/{int(row['id'])}/preview' class='row-actions'>"
			"<textarea name='samples' rows='2' required placeholder='One sample message per line'></textarea><button type='submit'>Test samples</button></form>"
			f"<form method='post' action='/moderation/rule-versions/{int(row['id'])}/publish' class='row-actions'>"
			"<select name='lifecycle_state'><option value='shadow'>Shadow</option><option value='enforce'>Enforce</option></select><button type='submit'>Publish</button></form>"
			f"<form method='post' action='/moderation/rule-versions/{int(row['id'])}/rollback' class='row-actions'><button type='submit'>Rollback to version</button></form></td></tr>"
			for row in rule_versions
		)
		status = self._escape((query.get("status") or [""])[0])
		status_html = f"<p class='status-banner'>{status}</p>" if status else ""
		bulk_panel = ""
		if session.role in {"moderator", "admin", "owner"}:
			bulk_panel = (
				"<section class='card'><h2>Bulk action</h2><div class='moderation-bulk'>"
				"<select id='bulk-action'><option value='warn'>Warn</option><option value='timeout'>Timeout</option><option value='ban'>Permanent ban</option></select>"
				"<input id='bulk-duration' type='number' min='1' max='2419200' value='600' aria-label='Duration seconds'>"
				"<input id='bulk-reason' required placeholder='Reason'>"
				"<input id='bulk-confirmation' placeholder='Confirmation phrase'>"
				"<button type='button' onclick='bulkModerate(true)'>Preview</button>"
				"<button type='button' onclick='bulkModerate(false)'>Execute</button></div>"
				"<pre id='bulk-results' aria-live='polite'></pre></section>"
				"<script>async function bulkModerate(dryRun){const targets=[...document.querySelectorAll('.bulk-target:checked')].map(el=>Number(el.value));"
				"const body={target_platform_account_ids:targets,action_type:document.getElementById('bulk-action').value,reason:document.getElementById('bulk-reason').value,duration_seconds:Number(document.getElementById('bulk-duration').value),confirmation:document.getElementById('bulk-confirmation').value,dry_run:dryRun};"
				"const response=await fetch('/api/moderation/bulk',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});"
				"document.getElementById('bulk-results').textContent=JSON.stringify(await response.json(),null,2);}</script>"
			)
		rule_form = ""
		if session.role in {"admin", "owner"}:
			rule_form = (
				"<form method='post' action='/moderation/rules/drafts' class='search'>"
				"<input name='name' placeholder='Rule name' required><select name='rule_type'><option value='exact_term'>Exact term</option><option value='banned_phrase'>Phrase</option><option value='streamboo_viewer_spam'>Streamboo spam</option><option value='link_restriction'>Link restriction</option><option value='duplicate_message'>Duplicate</option><option value='egregious_term'>Egregious</option></select>"
				"<input name='pattern' placeholder='Pattern' required><select name='severity'><option>low</option><option>medium</option><option>high</option><option>critical</option></select>"
				"<select name='auto_enforce_action'><option value=''>No action</option><option value='warn'>Warn</option><option value='timeout'>Timeout</option><option value='ban'>Ban</option></select>"
				"<input name='action_duration_seconds' type='number' min='1' max='2419200' value='600' aria-label='Action duration seconds'>"
				"<label><input type='checkbox' name='platform_scope' value='discord' checked> Discord</label>"
				"<label><input type='checkbox' name='platform_scope' value='twitch' checked> Twitch</label>"
				"<button type='submit'>Create draft</button></form>"
			)
		body = self._render_page(
			"Moderation",
			session,
			"<section class='hero'><div><p class='eyebrow'>Moderation</p><h1>Review and policy operations</h1><p class='lede'>Adjudicate queued findings, execute bounded actions, and manage rule enforcement modes.</p></div></section>"
			+ status_html
			+ workbench
			+ "<nav class='row-actions' aria-label='Moderation queues'><a href='#reviews'>Reviews</a>"
			+ f"<a href='#reports'>Reports ({len(reports)})</a><a href='#appeals'>Appeals ({len(appeals)})</a></nav>"
			+ bulk_panel
			+ f"<section class='card' id='reviews'><h2>Open reviews</h2><div class='table-scroll'><table class='table'><thead><tr><th>Select</th><th>ID</th><th>Platform</th><th>Target</th><th>Severity</th><th>Reason</th><th>Content</th><th>Resolution</th></tr></thead><tbody>{review_rows or '<tr><td colspan=8>No open reviews</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card' id='reports'><h2>Member reports</h2><div class='table-scroll'><table class='table'><thead><tr><th>Subject</th><th>Severity</th><th>Category</th><th>Summary</th><th>Resolution</th></tr></thead><tbody>{report_rows or '<tr><td colspan=5>No open reports</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card' id='appeals'><h2>Sanction appeals</h2><div class='table-scroll'><table class='table'><thead><tr><th>Member</th><th>Severity</th><th>Reason</th><th>Sanction</th><th>Reviewer</th><th>Resolution</th></tr></thead><tbody>{appeal_rows or '<tr><td colspan=6>No open appeals</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Rules</h2>{rule_form}<div class='table-scroll'><table class='table'><thead><tr><th>Name</th><th>Type</th><th>Pattern</th><th>Severity</th><th>Mode</th><th>Action</th><th>Duration</th><th>Enabled</th></tr></thead><tbody>{rule_rows or '<tr><td colspan=8>No rules</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Rule versions</h2><div class='table-scroll'><table class='table'><thead><tr><th>Rule</th><th>Version</th><th>State</th><th>Impact</th><th>Author</th><th>Approver</th><th>Lifecycle actions</th></tr></thead><tbody>{version_rows or '<tr><td colspan=7>No rule versions</td></tr>'}</tbody></table></div>"
			+ "<h3>Add exemption</h3><form method='post' action='/moderation/rules/0/exemptions' class='search' onsubmit=\"this.action='/moderation/rules/'+this.rule_id.value+'/exemptions'\"><input type='number' name='rule_id' min='1' required placeholder='Rule ID'><select name='exemption_type'><option value='channel'>Channel</option><option value='platform_account'>Member account ID</option></select><input name='exemption_value' required placeholder='Channel or account ID'><input name='reason' required placeholder='Reason'><button type='submit'>Add exemption</button></form></section>"
			+ f"<section class='card'><h2>Recent actions</h2><div class='table-scroll'><table class='table'><thead><tr><th>Platform</th><th>Target</th><th>Action</th><th>Status</th><th>Reason</th><th>Error</th><th>Created</th></tr></thead><tbody>{action_rows or '<tr><td colspan=7>No actions yet</td></tr>'}</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_moderation_rule_draft(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="rules.manage")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			create_moderation_rule_draft(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)), config={
					"name": (form.get("name") or [""])[0],
					"rule_type": (form.get("rule_type") or [""])[0],
					"pattern": (form.get("pattern") or [""])[0],
					"severity": (form.get("severity") or [""])[0],
					"auto_enforce_action": (form.get("auto_enforce_action") or [""])[0],
					"action_duration_seconds": int((form.get("action_duration_seconds") or ["600"])[0]),
					"platform_scope": form.get("platform_scope") or [],
				},
			)
		except (TypeError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=Rule+draft+created")

	def _serve_moderation_rule_preview(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler, permission="rules.manage")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			version_id = int([part for part in path.split("/") if part][2])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			impact = preview_moderation_rule_version(
				connection, tenant=TenantContext(int(session.community_id)), version_id=version_id,
				samples=(form.get("samples") or [""])[0].splitlines(),
			)
		except (IndexError, LookupError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=" + quote(
			f"Sample impact: {impact['match_count']} of {impact['sample_count']} matched"
		))

	def _serve_moderation_rule_publish(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		self._serve_moderation_rule_transition(handler, path, rollback=False)

	def _serve_moderation_rule_rollback(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		self._serve_moderation_rule_transition(handler, path, rollback=True)

	def _serve_moderation_rule_transition(
		self, handler: BaseHTTPRequestHandler, path: str, *, rollback: bool,
	) -> None:
		session = self._require_session(handler, permission="rules.manage")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			version_id = int([part for part in path.split("/") if part][2])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			kwargs = {
				"tenant": TenantContext(int(session.community_id)),
				"actor": ActorAttribution("operator", int(session.user_id)),
				"version_id": version_id,
			}
			if rollback:
				rollback_moderation_rule(connection, **kwargs)
			else:
				publish_moderation_rule_version(
					connection, **kwargs,
					lifecycle_state=(form.get("lifecycle_state") or ["shadow"])[0],
				)
		except (IndexError, LookupError, PermissionError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=" + ("Rule+rolled+back" if rollback else "Rule+published"))

	def _serve_moderation_rule_exemption(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler, permission="rules.manage")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			rule_id = int([part for part in path.split("/") if part][2])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			add_moderation_rule_exemption(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)), rule_id=rule_id,
				exemption_type=(form.get("exemption_type") or [""])[0],
				exemption_value=(form.get("exemption_value") or [""])[0],
				reason=(form.get("reason") or [""])[0],
			)
		except (IndexError, LookupError, ValueError, sqlite3.IntegrityError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=Rule+exemption+added")

	def _serve_moderation_filter_save(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="moderation.queues.read")
		if session is None or session.community_id is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			filters = json.loads((form.get("filters") or ["{}"]) [0])
			if not isinstance(filters, dict):
				raise ValueError("filters must be an object")
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			save_moderation_filter(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)),
				name=(form.get("name") or [""])[0], filters=filters,
			)
		except (json.JSONDecodeError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=" + quote("Filter saved"))

	def _serve_moderation_work_assign(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler, permission="moderation.manage")
		if session is None or session.community_id is None:
			return
		try:
			parts = [part for part in path.split("/") if part]
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			assign_moderation_work(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)),
				work_type=parts[2], item_id=int(parts[3]),
			)
		except (IndexError, LookupError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?queue=mine&status=" + quote("Work assigned"))

	def _serve_member_queue_resolve(
		self, handler: BaseHTTPRequestHandler, path: str, queue_type: str,
	) -> None:
		session = self._require_session(handler, permission="appeals.manage")
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			item_id = int([part for part in path.split("/") if part][2])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			resolve_member_queue_item(
				connection, tenant=TenantContext(int(session.community_id)),
				actor=ActorAttribution("operator", int(session.user_id)), queue_type=queue_type,
				item_id=item_id, resolution=(form.get("resolution") or [""])[0],
				note=(form.get("note") or [""])[0],
			)
		except (IndexError, LookupError, PermissionError, ValueError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, f"/moderation?status={queue_type.title()}+resolved")

	def _serve_moderation_review_resolve(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			action_type = (form.get("action_type") or [""])[0]
			if action_type == "ban" and not hmac.compare_digest(
				(form.get("confirmation") or [""])[0].strip(), "PERMANENT BAN"
			):
				raise ValueError("Permanent ban confirmation required")
			review_id = int(path.split("/")[3])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				resolve_review(connection, review_id, resolution=(form.get("resolution") or [""])[0],
					tenant=TenantContext(session.community_id),
					actor=ActorAttribution("operator", int(session.user_id)),
					note=(form.get("note") or [""])[0],
					action_type=action_type or None,
					duration_seconds=int((form.get("duration_seconds") or ["600"])[0]))
		except (ValueError, IndexError) as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=Review+resolved")

	def _serve_moderation_rule_save(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				save_moderation_rule(connection, name=(form.get("name") or [""])[0],
					rule_type=(form.get("rule_type") or [""])[0], pattern=(form.get("pattern") or [""])[0],
					severity=(form.get("severity") or [""])[0],
					auto_enforce_action=(form.get("auto_enforce_action") or [""])[0] or None,
					enabled=(form.get("enabled") or [""])[0] == "1",
					enforcement_mode=(form.get("enforcement_mode") or ["shadow"])[0],
					action_duration_seconds=int((form.get("action_duration_seconds") or ["600"])[0]),
					operator_id=int(session.user_id), community_id=session.community_id)
		except ValueError as exc:
			self._redirect(handler, f"/moderation?status={quote(str(exc))}")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/moderation?status=Rule+saved")

	def _serve_commands(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		status_message = (query.get("status") or [""])[0].strip()
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			command_rows = list_command_definitions(connection)
			simple_rows = list_simple_command_definitions(connection)
		finally:
			connection.close()
		builtin_rows = []
		for row in command_rows:
			command_name = self._escape(row[0])
			enabled_checked = "checked" if row[4] else ""
			builtin_rows.append(
				"<tr>"
				+ f"<td><code>!{command_name}</code><input form='builtin-{command_name}' type='hidden' name='record_type' value='builtin'><input form='builtin-{command_name}' type='hidden' name='command_name' value='{command_name}'></td>"
				+ f"<td><input class='builtin-title-input' form='builtin-{command_name}' name='title' value='{self._escape(row[1])}' required></td>"
				+ f"<td><textarea form='builtin-{command_name}' name='description_template' rows='3' required>{self._escape(row[2])}</textarea></td>"
				+ f"<td><textarea form='builtin-{command_name}' name='footer_template' rows='3' placeholder='{{platform}} user: {{author_username}}'>{self._escape(row[3] or '')}</textarea></td>"
				+ f"<td><label class='checkbox'><input form='builtin-{command_name}' type='checkbox' name='enabled' value='1' {enabled_checked}> Enabled</label></td>"
				+ f"<td><form id='builtin-{command_name}' method='post' action='/commands'><button type='submit'>Save</button></form></td>"
				+ "</tr>"
			)
		simple_rows_html = []
		for row in simple_rows:
			command_name = self._escape(row[0])
			enabled_checked = "checked" if row[2] else ""
			simple_rows_html.append(
				"<tr>"
				+ f"<td><code>!{command_name}</code><input form='simple-{command_name}' type='hidden' name='record_type' value='simple'><input form='simple-{command_name}' type='hidden' name='command_name' value='{command_name}'></td>"
				+ f"<td><input form='simple-{command_name}' name='response_template' value='{self._escape(row[1])}' required></td>"
				+ f"<td><label class='checkbox'><input form='simple-{command_name}' type='checkbox' name='enabled' value='1' {enabled_checked}> Enabled</label></td>"
				+ f"<td><div class='row-actions'><form id='simple-{command_name}' method='post' action='/commands'><button type='submit'>Save</button></form><form id='simple-delete-{command_name}' method='post' action='/commands'><input type='hidden' name='record_type' value='simple'><input type='hidden' name='action' value='delete'><input type='hidden' name='command_name' value='{command_name}'><button type='submit'>Delete</button></form></div></td>"
				+ "</tr>"
			)
		status_html = f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else ""
		body = self._render_page(
			"Commands",
			session,
			"<section class='hero'><div><p class='eyebrow'>Commands</p><h1>Command menu</h1><p class='lede'>Edit builtin command templates, add new simple commands, and keep Discord and Twitch output in sync.</p></div>"
			+ "<div class='toolbar'>"
			+ "<button type='button' onclick=\"document.getElementById('templating-information').showModal()\">Templating Information</button>"
			+ status_html
			+ "</div></section>"
			+ self._render_template_info_dialog()
			+ "<section class='card'><h2>Built-Ins</h2><p class='muted'>Builtins are the commands that ship with the bot.</p><div class='table-scroll'><table class='table'><thead><tr><th>Command</th><th>Title</th><th>Description template</th><th>Footer template</th><th>Status</th><th>Action</th></tr></thead><tbody>"
			+ ("".join(builtin_rows) or "<tr><td colspan='6'>No builtin commands found</td></tr>")
			+ "</tbody></table></div></section>"
			+ "<section class='card'><h2>Plaintext Commands</h2><p class='muted'>Insert quick text replies or update existing simple commands.</p><form id='simple-new' method='post' action='/commands' class='new-command-form'><input type='hidden' name='record_type' value='simple'><input class='new-command-name' name='command_name' placeholder='Command name (without !) e.g. website' required><input class='new-command-response' name='response_template' placeholder='Plain text response with {display_name}' required><label class='checkbox new-command-enabled'><input type='checkbox' name='enabled' value='1' checked> Enabled</label><button type='submit'>New Command</button></form><div class='table-scroll'><table class='table'><thead><tr><th>Command</th><th>Response template</th><th>Status</th><th>Action</th></tr></thead><tbody>"
			+ "".join(simple_rows_html)
			+ "</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_commands_update(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		command_name = next((value.strip().casefold() for value in (form.get("command_name") or []) if value.strip()), "")
		record_type = (form.get("record_type") or ["builtin"])[0].strip().casefold() or "builtin"
		action = (form.get("action") or ["save"])[0].strip().casefold() or "save"
		title = (form.get("title") or [""])[0].strip()
		description_template = (form.get("description_template") or [""])[0].strip()
		footer_template = (form.get("footer_template") or [""])[0].strip()
		response_template = (form.get("response_template") or [""])[0].strip()
		enabled = (form.get("enabled") or [""])[0].strip() == "1"
		if not command_name:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Missing command name")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if record_type == "simple":
				if action == "delete":
					delete_simple_command_definition(connection, command_name)
				else:
					upsert_simple_command_definition(
						connection,
						command_name=command_name,
						response_template=response_template,
						enabled=enabled,
					)
			else:
				upsert_command_definition(
					connection,
					command_name=command_name,
					title=title,
					description_template=description_template,
					footer_template=footer_template or None,
					enabled=enabled,
				)
		finally:
			connection.close()
		if record_type == "simple" and action == "delete":
			self._redirect(handler, f"/commands?status={quote(f'Deleted simple command {command_name}')}")
			return
		label = "simple command" if record_type == "simple" else "builtin command"
		self._redirect(handler, f"/commands?status={quote(f'Saved {label} {command_name}')}")

	def _render_template_info_dialog(self) -> str:
		items = [
			("display_name", "Linked canonical display name for the invoking user."),
			("author_username", "The platform username that invoked the command."),
			("platform", "The source platform name such as discord or twitch."),
			("score", "Current reputation score for the linked profile."),
			("power_user", "Yes or No depending on whether the profile is flagged as a power user."),
			("linked_accounts", "A newline-separated list of linked platform accounts."),
			("latest_note", "The most recent operator note, if one exists."),
			("command_name", "The normalized command name that triggered the response."),
			("query", "Everything after the command name, useful for API query parameters."),
			("0..49", "Generates a random integer using an inclusive range, e.g. {0..49}."),
			("0..{query}", "Uses a sanitized integer parsed from {query} as a range bound, e.g. {0..{query}}."),
			(
				"{METHOD}(url)[selectors]",
				(
					"Performs one HTTP request (GET/POST/PUT/DELETE) and supports either a single JSON path "
					"or an alias map. Single-path example: {GET}(url)[totals.posts]. Alias-map examples: "
					"{GET}(url)[posts:totals.posts,threads:totals.threads] or {GET}(url)[posts=totals.posts;threads=totals.threads]. "
					"Aliased values are reused later in the template with {posts} and {threads}. Duplicate calls with "
					"the same method+url are cached during one render. If the request or path lookup fails, aliased placeholders "
					"remain unchanged so output degrades safely."
				),
			),
		]
		rows = "".join(
			f"<tr><td><code>{self._escape(name if name.startswith('{') else '{' + name + '}')}</code></td><td>{self._escape(description)}</td></tr>"
			for name, description in items
		)
		return (
			"<dialog id='templating-information' class='template-dialog'>"
			+ "<form method='dialog' class='template-dialog-inner'>"
			+ "<div class='template-dialog-header'><h2>Templating Information</h2><button value='cancel' aria-label='Close dialogue'>Close</button></div>"
			+ "<p class='lede'>Templates use Python-style replacement fields. If a value is unavailable, it falls back to an empty string or a safe default.</p>"
			+ "<table><thead><tr><th>Value</th><th>Meaning</th></tr></thead><tbody>"
			+ rows
			+ "</tbody></table>"
			+ "</form></dialog>"
		)

	def _serve_api_overview(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			overview = load_overview_snapshot(
				connection, community_id=session.community_id
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"overview": overview.__dict__, "services": dict(self.service_states)})

	def _serve_search(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			items = self._search_items(
				connection, query, community_id=session.community_id
			)
			saved = list_saved_queries(connection, operator_id=int(session.user_id))
		finally:
			connection.close()
		rows = "".join(
			f"<tr><td>{item['id']}</td><td>{self._escape(item['occurred_at'])}</td><td>{self._escape(item['platform'])}</td>"
			f"<td>{self._escape(item['event_type'])}</td><td>{self._escape(item.get('text_raw') or '')}</td>"
			f"<td><a href='/api/observations/{item['id']}/pivots'>Pivots</a></td></tr>" for item in items
		) or "<tr><td colspan='6'>No matching observations.</td></tr>"
		def _saved_href(item: Mapping[str, object]) -> str:
			params = {key: str(value) for key, value in dict(item.get("filters") or {}).items() if value not in (None, "")}
			params["q"] = str(item["query_text"])
			return "/search?" + urlencode(params)
		saved_options = "".join(f"<li><a href='{self._escape(_saved_href(item))}'>{self._escape(item['name'])}</a></li>" for item in saved) or "<li>No saved queries.</li>"
		filter_fields = ("start_at", "end_at", "platform", "event_type", "user_id", "container_id", "context_id", "entity_type", "entity_value")
		hidden_filters = "".join(
			f"<input type='hidden' name='{key}' value='{self._escape((query.get(key) or [''])[0])}'>"
			for key in filter_fields
		)
		limit_raw = (query.get("limit") or ["100"])[0]
		offset_raw = (query.get("offset") or ["0"])[0]
		page_limit = max(1, min(int(limit_raw) if limit_raw.isdigit() else 100, 500))
		page_offset = max(0, int(offset_raw) if offset_raw.isdigit() else 0)
		base_params = {key: values[0] for key, values in query.items() if values and key not in {"offset", "limit"}}
		base_params["limit"] = str(page_limit)
		pagination = ""
		if page_offset > 0:
			pagination += f"<a href='/search?{urlencode({**base_params, 'offset': str(max(0, page_offset-page_limit))})}'>Previous</a> "
		if len(items) == page_limit:
			pagination += f"<a href='/search?{urlencode({**base_params, 'offset': str(page_offset+page_limit)})}'>Next</a>"
		content = (
			"<section class='hero'><div><p class='eyebrow'>Investigation</p><h1>Observation search</h1>"
			"<p class='lede'>Full-text search with temporal, entity, user, platform, and event filters.</p></div></section>"
			"<section class='card'><form class='search' method='get' action='/search'>"
			f"<input name='q' placeholder='Terms or phrases' value='{self._escape((query.get('q') or [''])[0])}'>"
			f"<input name='start_at' value='{self._escape((query.get('start_at') or [''])[0])}' placeholder='Start ISO timestamp'><input name='end_at' value='{self._escape((query.get('end_at') or [''])[0])}' placeholder='End ISO timestamp'>"
			f"<input name='platform' value='{self._escape((query.get('platform') or [''])[0])}' placeholder='Platform'><input name='event_type' value='{self._escape((query.get('event_type') or [''])[0])}' placeholder='Event type'>"
			f"<input name='user_id' value='{self._escape((query.get('user_id') or [''])[0])}' placeholder='User ID'><input name='context_id' value='{self._escape((query.get('context_id') or [''])[0])}' placeholder='Context'>"
			f"<input name='entity_type' value='{self._escape((query.get('entity_type') or [''])[0])}' placeholder='Entity type'><input name='entity_value' value='{self._escape((query.get('entity_value') or [''])[0])}' placeholder='Entity value'>"
			"<button type='submit'>Search</button></form></section>"
			f"<section class='card'><h2>Results</h2><div class='toolbar'><a href='/search/export.csv?{urlencode({key: values[0] for key, values in query.items() if values})}'>Export CSV</a>{pagination}</div><table><thead><tr><th>ID</th><th>Time</th><th>Platform</th><th>Event</th><th>Content</th><th>Investigate</th></tr></thead><tbody>{rows}</tbody></table></section>"
			f"<section class='card'><h2>Saved queries</h2><form class='toolbar' method='post' action='/search/saved'><input name='name' placeholder='Query name' required><input name='q' value='{self._escape((query.get('q') or [''])[0])}' placeholder='Full-text query'>{hidden_filters}<button type='submit'>Save query</button></form><ul>{saved_options}</ul></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Search", session, content))

	def _serve_search_save(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None: return
		name = (form.get("name") or [""])[0]; query_text = (form.get("q") or [""])[0]
		filters = {
			key: (form.get(key) or [""])[0]
			for key in ("start_at", "end_at", "platform", "event_type", "user_id", "container_id", "context_id", "entity_type", "entity_value")
			if (form.get(key) or [""])[0]
		}
		try:
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection: save_query(connection, name, query_text, filters, operator_id=int(session.user_id))
		except ValueError as exc:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc)); return
		finally:
			if 'connection' in locals(): connection.close()
		self._redirect(handler, f"/search?q={quote(query_text)}")

	def _serve_search_export(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler, permission="exports.create")
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			export_query = dict(query)
			export_query["limit"] = ["500"]
			export_query["offset"] = ["0"]
			items = self._search_items(
				connection, export_query, community_id=session.community_id
			)
		finally:
			connection.close()
		output = io.StringIO()
		fields = ("id", "occurred_at", "platform", "event_type", "external_event_id", "container_id",
			"context_id", "actor_user_id", "target_user_id", "language_code", "sentiment_label",
			"intent_label", "threat_level", "text_raw")
		writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
		writer.writeheader()
		writer.writerows(items)
		self._audit_operator_action(int(session.user_id), "search.exported", "observation", None,
			{"rows": len(items), "filters": {key: values[0] for key, values in query.items() if values}})
		self._send_bytes(handler, HTTPStatus.OK, output.getvalue().encode("utf-8"),
			"text/csv; charset=utf-8", "qbot4k-observations.csv")

	def _serve_audit(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		items = self._audit_items(query)
		rows = "".join(
			f"<tr><td>{self._escape(item['created_at'])}</td><td>{self._escape(item['actor_type'])}:{self._escape(item['actor_id'] or 'system')}</td>"
			f"<td>{self._escape(item['action_type'])}</td><td>{self._escape(item['entity_type'])}</td><td>{self._escape(item['entity_id'] or '—')}</td>"
			f"<td><code>{self._escape(item['payload_json'])}</code></td></tr>" for item in items
		) or "<tr><td colspan='6'>No audit events.</td></tr>"
		content = (
			"<section class='hero'><p class='eyebrow'>Governance</p><h1>Audit trail</h1><p class='lede'>Review authentication, analyst decisions, policy changes, account links, exports, and administrative actions.</p></section>"
			+ "<section class='card'><form method='get' action='/audit' class='search'>"
			+ f"<input name='action_type' value='{self._escape((query.get('action_type') or [''])[0])}' placeholder='Action type'>"
			+ f"<input name='actor_id' value='{self._escape((query.get('actor_id') or [''])[0])}' placeholder='Operator ID'>"
			+ f"<input name='entity_type' value='{self._escape((query.get('entity_type') or [''])[0])}' placeholder='Entity type'>"
			+ f"<input name='start_at' value='{self._escape((query.get('start_at') or [''])[0])}' placeholder='Start ISO time'>"
			+ "<button type='submit'>Filter</button><a href='/audit'>Clear</a></form>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Entity</th><th>ID</th><th>Payload</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Audit", session, content))

	def _audit_items(self, query: Mapping[str, list[str]]) -> list[dict[str, object]]:
		where: list[str] = []
		params: list[object] = []
		for key in ("action_type", "entity_type"):
			value = (query.get(key) or [""])[0].strip()
			if value:
				where.append(f"{key}=?")
				params.append(value)
		actor_id = (query.get("actor_id") or [""])[0].strip()
		if actor_id.isdigit():
			where.append("actor_id=?")
			params.append(int(actor_id))
		start_at = (query.get("start_at") or [""])[0].strip()
		if start_at:
			where.append("created_at>=?")
			params.append(start_at)
		limit_raw = (query.get("limit") or ["200"])[0]
		offset_raw = (query.get("offset") or ["0"])[0]
		limit = max(1, min(int(limit_raw) if limit_raw.isdigit() else 200, 500))
		offset = max(0, int(offset_raw) if offset_raw.isdigit() else 0)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
			params.extend((limit, offset))
			return [dict(row) for row in connection.execute(sql, params).fetchall()]
		finally:
			connection.close()

	def _serve_analytics(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		sorts = self._normalize_analytics_sorts(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			snapshot = analytics_snapshot(
				connection, sorts=sorts, community_id=session.community_id
			)
		finally:
			connection.close()

		def analytics_query(table_name: str, sort: str, direction: str) -> str:
			params: dict[str, str] = {}
			for key, (sort_by, sort_dir) in sorts.items():
				params[f"{key}_sort"] = sort_by
				params[f"{key}_dir"] = sort_dir
			params[f"{table_name}_sort"] = sort
			params[f"{table_name}_dir"] = direction
			return urlencode(params)

		def table(table_name, items, columns):
			current_sort, current_dir = sorts[table_name]
			head_parts = []
			for column in columns:
				is_current = current_sort == column
				default_dir = "asc" if column in self._analytics_text_columns(table_name) else "desc"
				next_dir = ("asc" if current_dir == "desc" else "desc") if is_current else default_dir
				indicator = " ↑" if is_current and current_dir == "asc" else " ↓" if is_current else ""
				label = column.replace("_", " ").title() + indicator
				head_parts.append(f"<th><a href='/analytics?{analytics_query(table_name, column, next_dir)}'>{self._escape(label)}</a></th>")
			head = "".join(head_parts)
			body = "".join("<tr>" + "".join(f"<td>{self._escape(item.get(column, ''))}</td>" for column in columns) + "</tr>" for item in items)
			return f"<table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td>No data yet.</td></tr>'}</tbody></table>"
		def summary_table(items, columns):
			head = "".join(f"<th>{self._escape(column.replace('_', ' ').title())}</th>" for column in columns)
			body = "".join(
				"<tr>" + "".join(f"<td>{self._escape(item.get(column, ''))}</td>" for column in columns) + "</tr>"
				for item in items
			)
			return f"<table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td>No data yet.</td></tr>'}</tbody></table>"
		content = "<section class='hero'><div><p class='eyebrow'>Analytical breadth</p><h1>Intelligence analytics</h1><p class='lede'>Community growth, moderation outcomes, networks, cohort deviations, and model quality.</p><a href='/analytics/export.json'>Export analytics</a></div></section>"
		content += "<section class='card'><h2>Community growth</h2>" + summary_table(snapshot["growth"], ["metric_date", "joins", "leaves", "net_growth"]) + "</section>"
		content += "<section class='card'><h2>Repeat offenses</h2>" + summary_table(snapshot["repeat_offenses"], ["username", "action_count", "sanction_types", "first_action_at", "latest_action_at"]) + "</section>"
		content += "<section class='card'><h2>Report outcomes</h2>" + summary_table(snapshot["report_outcomes"], ["outcome", "report_count", "avg_resolution_seconds"]) + "</section>"
		content += "<section class='card'><h2>Appeal outcomes</h2>" + summary_table(snapshot["appeal_outcomes"], ["outcome", "appeal_count", "avg_resolution_seconds"]) + "</section>"
		content += "<section class='card'><h2>Rule precision</h2>" + summary_table(snapshot["rule_precision"], ["name", "matches", "reviewed", "confirmed", "false_positives", "precision"]) + "</section>"
		content += "<section class='card'><h2>Emerging topics</h2>" + table("topics", snapshot["topics"], ["topic_kind", "label", "velocity", "community_count", "unusualness"]) + "</section>"
		content += "<section class='card'><h2>Graph influence</h2>" + table("graph", snapshot["graph"], ["user_id", "pagerank", "betweenness", "is_bridge", "cluster_id", "influence_score"]) + "</section>"
		content += "<section class='card'><h2>Identity suggestions</h2>" + table("identity_suggestions", snapshot["identity_suggestions"], ["id", "left_platform_account_id", "right_platform_account_id", "confidence", "status"]) + "</section>"
		content += "<section class='card'><h2>Cohort anomalies</h2>" + table("cohort_anomalies", snapshot["cohort_anomalies"], ["user_id", "cohort_key", "signal_key", "z_score", "direction", "confidence"]) + "</section>"
		content += "<section class='card'><h2>Model evaluation</h2>" + table("evaluation", snapshot["evaluation"], ["model_key", "model_version", "sample_size", "calculated_at"]) + "</section>"
		self._send_html(handler, HTTPStatus.OK, self._render_page("Analytics", session, content))

	def _serve_analytics_export(
		self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]
	) -> None:
		session = self._require_session(handler, permission="analytics.export")
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			payload = analytics_snapshot(
				connection, sorts=self._normalize_analytics_sorts(query),
				community_id=session.community_id,
			)
		finally:
			connection.close()
		payload["exported_at"] = datetime.now(timezone.utc).isoformat()
		payload["community_id"] = session.community_id
		self._audit_operator_action(
			int(session.user_id), "analytics.exported", "community", session.community_id,
			{"sections": sorted(key for key in payload if isinstance(payload[key], list))},
		)
		self._send_bytes(
			handler, HTTPStatus.OK, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
			"application/json", f"qbot4k-community-{session.community_id}-analytics.json",
		)

	def _serve_api_search(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			items = self._search_items(
				connection, query, community_id=session.community_id
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": items})

	def _search_items(
		self, connection, query: Mapping[str, list[str]], *, community_id: int
	) -> list[dict[str, object]]:
		value = lambda key: (query.get(key) or [""])[0].strip() or None
		user_raw = value("user_id")
		limit_raw = value("limit") or "100"
		offset_raw = value("offset") or "0"
		limit = max(1, min(int(limit_raw) if limit_raw.isdigit() else 100, 500))
		offset = max(0, int(offset_raw) if offset_raw.isdigit() else 0)
		return search_observations(
			connection, query=value("q") or "", start_at=value("start_at"), end_at=value("end_at"),
			platform=value("platform"), event_type=value("event_type"), user_id=int(user_raw) if user_raw and user_raw.isdigit() else None,
			container_id=value("container_id"), context_id=value("context_id"), entity_type=value("entity_type"),
			entity_value=value("entity_value"), limit=limit, offset=offset,
			community_id=community_id,
		)

	def _serve_api_save_query(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection:
				query_id = save_query(connection, str(payload.get("name") or ""), str(payload.get("query") or ""), payload.get("filters") if isinstance(payload.get("filters"), dict) else {}, operator_id=int(session.user_id))
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"id": query_id, "status": "saved"})

	def _serve_api_observation_pivots(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		try:
			observation_id = int(path.split("/")[3])
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			payload = observation_pivots(
				connection, observation_id, community_id=session.community_id
			)
		except (ValueError, IndexError):
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "observation_not_found"}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_analytics(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		sorts = self._normalize_analytics_sorts(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection); payload = analytics_snapshot(
				connection, sorts=sorts, community_id=session.community_id
			)
		finally:
			connection.close()
		payload["sort"] = {key: {"by": value[0], "dir": value[1]} for key, value in sorts.items()}
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_identity_review(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None: return
		try:
			suggestion_id = int(path.rstrip('/').split('/')[-1]); connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection:
				review_identity_suggestion(
					connection, suggestion_id, str(payload.get("decision") or ""),
					operator_id=int(session.user_id), community_id=session.community_id,
				)
		except ValueError as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "reviewed"})

	def _serve_api_external_observation(self, handler: BaseHTTPRequestHandler) -> None:
		if not self._require_ingest_auth(handler):
			return
		payload = self._read_json_body(handler)
		if payload is None: return
		try:
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			result = collect_external_feed_item(
				connection, source_key=str(payload.get("source_key") or ""), external_event_id=str(payload.get("external_event_id") or ""),
				text=str(payload.get("text") or ""), occurred_at=str(payload.get("occurred_at") or "") or None,
				display_name=str(payload.get("display_name") or "") or None, source_type=str(payload.get("source_type") or "api"),
				actor_id=str(payload.get("actor_id") or "") or None, context_id=str(payload.get("context_id") or "") or None,
				attributes=payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}, trust_weight=float(payload.get("trust_weight") or 0.5),
			)
		except (ValueError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.CREATED, {"status": result.status, "observation_id": result.observation_id, "analysis_job_id": result.analysis_job_id})

	def _serve_api_event(self, handler: BaseHTTPRequestHandler) -> None:
		if not self._require_ingest_auth(handler):
			return
		payload = self._read_json_body(handler)
		if payload is None: return
		try:
			event_type = str(payload.get("event_type") or "").strip().casefold()
			if event_type not in SUPPORTED_EVENT_TYPES:
				raise ValueError("unsupported event_type")
			observation = Observation(
				platform=str(payload.get("platform") or "").strip(), event_type=event_type,
				external_event_id=str(payload.get("external_event_id") or "").strip() or None,
				actor_platform_user_id=str(payload.get("actor_platform_user_id") or "").strip() or None,
				actor_username=str(payload.get("actor_username") or "").strip() or None,
				target_platform_user_id=str(payload.get("target_platform_user_id") or "").strip() or None,
				container_id=str(payload.get("container_id") or "").strip() or None,
				context_id=str(payload.get("context_id") or "").strip() or None,
				text=str(payload.get("text") or "") or None, occurred_at=coerce_timestamp(str(payload.get("occurred_at") or "") or None),
				attributes=payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {},
			)
			if not observation.platform:
				raise ValueError("platform is required")
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			result = collect_observation(connection, observation)
		except (ValueError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.CREATED, {"status": result.status, "observation_id": result.observation_id, "analysis_job_id": result.analysis_job_id})

	def _require_ingest_auth(self, handler: BaseHTTPRequestHandler) -> bool:
		authorization = (handler.headers.get("Authorization") or "").strip()
		if authorization.casefold().startswith("bearer "):
			provided = authorization[7:].strip()
			expected = self.settings.ingest_api_token or ""
			if expected and hmac.compare_digest(provided, expected):
				return True
			self._send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "invalid_bearer_token"})
			return False
		return self._require_session(handler, admin_only=True) is not None

	def _serve_api_users(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		search = (query.get("q") or [""])[0]
		sort_by, sort_dir = self._normalize_user_sort(
			(query.get("sort") or ["score"])[0],
			(query.get("dir") or [""])[0],
		)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			users = search_users(
				connection, query=search, sort_by=sort_by, sort_dir=sort_dir,
				community_id=session.community_id,
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [item.__dict__ for item in users]})

	def _serve_intelligence(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		alert_sort, alert_dir = self._normalize_alert_sort(query)
		case_sort, case_dir = self._normalize_case_sort(query)
		relationship_sort, relationship_dir = self._normalize_relationship_sort(query)
		alert_order = self._alert_order_clause(alert_sort, alert_dir)
		case_order = self._case_order_clause(case_sort, case_dir)
		relationship_order = self._relationship_order_clause(relationship_sort, relationship_dir)
		alert_severity = (query.get("severity") or [""])[0].strip().casefold()
		alert_status_raw = (query.get("alert_status") or ["open"])[0].strip().casefold()
		alert_status = "" if alert_status_raw == "all" else alert_status_raw
		alert_assignee = (query.get("assignee") or [""])[0].strip()
		alert_query = (query.get("alert_q") or [""])[0].strip()
		alert_where: list[str] = ["intelligence_alerts.community_id=?"]
		alert_params: list[object] = [session.community_id]
		if alert_severity in {"low", "medium", "high", "critical"}:
			alert_where.append("lower(intelligence_alerts.severity)=?")
			alert_params.append(alert_severity)
		if alert_status in {"open", "acknowledged", "in_case", "resolved", "suppressed"}:
			alert_where.append("lower(intelligence_alerts.status)=?")
			alert_params.append(alert_status)
		if alert_assignee.isdigit():
			alert_where.append("intelligence_alerts.assigned_operator_id=?")
			alert_params.append(int(alert_assignee))
		if alert_query:
			like = f"%{alert_query}%"
			alert_where.append("(intelligence_alerts.title LIKE ? OR intelligence_alerts.summary LIKE ? OR users.primary_display_name LIKE ?)")
			alert_params.extend((like, like, like))
		alert_where_sql = " WHERE " + " AND ".join(alert_where) if alert_where else ""
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			summary = intelligence_summary(connection, tenant=TenantContext(session.community_id))
			alerts = connection.execute(
				"""
				SELECT intelligence_alerts.*, users.primary_display_name
				FROM intelligence_alerts
				LEFT JOIN users ON users.id = intelligence_alerts.user_id
				""" + alert_where_sql + """
				ORDER BY """ + alert_order + """
				LIMIT 100
				"""
			, alert_params).fetchall()
			cases = connection.execute(
				"""
				SELECT investigation_cases.*, COUNT(DISTINCT case_entities.user_id) AS entity_count,
				       COUNT(DISTINCT case_evidence.id) AS evidence_count
				FROM investigation_cases
				LEFT JOIN case_entities ON case_entities.case_id = investigation_cases.id
				LEFT JOIN case_evidence ON case_evidence.case_id = investigation_cases.id
				WHERE investigation_cases.community_id=?
				GROUP BY investigation_cases.id
				ORDER BY """ + case_order + """ LIMIT 50
				"""
			, (session.community_id,)).fetchall()
			relationships = connection.execute(
				"""
				SELECT entity_relationships.*, source.primary_display_name AS source_name,
				       target.primary_display_name AS target_name
				FROM entity_relationships
				INNER JOIN users source ON source.id = entity_relationships.source_user_id
				INNER JOIN users target ON target.id = entity_relationships.target_user_id
				WHERE entity_relationships.community_id=?
				ORDER BY """ + relationship_order + """
				LIMIT 100
				"""
			, (session.community_id,)).fetchall()
			reports = connection.execute(
				"""SELECT id,report_type,title,summary,generated_at FROM intelligence_reports
				   WHERE community_id=? ORDER BY generated_at DESC LIMIT 50""",
				(session.community_id,),
			).fetchall()
		finally:
			connection.close()

		def _intelligence_query(**overrides: object) -> str:
			params: dict[str, str] = {
				"alert_sort": alert_sort, "alert_dir": alert_dir,
				"case_sort": case_sort, "case_dir": case_dir,
				"relationship_sort": relationship_sort, "relationship_dir": relationship_dir,
				"severity": alert_severity, "alert_status": alert_status_raw,
				"assignee": alert_assignee, "alert_q": alert_query,
			}
			params.update({key: str(value) for key, value in overrides.items()})
			return urlencode(params)

		def _sort_header(label: str, key: str, *, table: str, current_sort: str, current_dir: str, ascending_keys: set[str]) -> str:
			is_current = current_sort == key
			default_dir = "asc" if key in ascending_keys else "desc"
			next_dir = ("asc" if current_dir == "desc" else "desc") if is_current else default_dir
			indicator = " ↑" if is_current and current_dir == "asc" else " ↓" if is_current else ""
			return f"<a href='/intelligence?{_intelligence_query(**{table + '_sort': key, table + '_dir': next_dir})}'>{self._escape(label + indicator)}</a>"

		def _alert_header(label: str, key: str) -> str:
			return _sort_header(label, key, table="alert", current_sort=alert_sort, current_dir=alert_dir, ascending_keys={"subject", "finding", "status"})

		def _case_header(label: str, key: str) -> str:
			return _sort_header(label, key, table="case", current_sort=case_sort, current_dir=case_dir, ascending_keys={"case", "status"})

		def _relationship_header(label: str, key: str) -> str:
			return _sort_header(label, key, table="relationship", current_sort=relationship_sort, current_dir=relationship_dir, ascending_keys={"source", "relationship", "target"})

		severity_options = "".join(
			f"<option value='{value}'{' selected' if alert_severity == value else ''}>{value}</option>"
			for value in ("critical", "high", "medium", "low")
		)
		alert_status_options = "".join(
			f"<option value='{value}'{' selected' if alert_status == value else ''}>{value.replace('_', ' ')}</option>"
			for value in ("open", "acknowledged", "in_case", "suppressed", "resolved")
		)
		alert_filters = (
			"<form method='get' action='/intelligence' class='search'>"
			+ f"<input name='alert_q' value='{self._escape(alert_query)}' placeholder='Subject or finding'>"
			+ f"<select name='severity'><option value=''>All severities</option>{severity_options}</select>"
			+ f"<select name='alert_status'><option value='all'{' selected' if not alert_status else ''}>All statuses</option>{alert_status_options}</select>"
			+ f"<input name='assignee' value='{self._escape(alert_assignee)}' placeholder='Operator ID'>"
			+ "<button type='submit'>Filter alerts</button><a href='/intelligence'>Clear</a></form>"
		)

		alert_rows = "".join(
			"<tr>"
			+ f"<td>{self._escape(row['severity'])}</td>"
			+ f"<td><a href='/users/{row['user_id']}'>{self._escape(row['primary_display_name'] or 'Unknown')}</a></td>"
			+ f"<td><strong>{self._escape(row['title'])}</strong><br><span class='muted'>{self._escape(row['summary'])}</span></td>"
			+ f"<td>{float(row['confidence']) * 100:.0f}%</td><td>{self._escape(row['status'])}</td>"
			+ (f"<td><div class='row-actions'><form method='post' action='/intelligence/alerts/{row['id']}/case'><button type='submit'>Open case</button></form>"
			   + (f"<form method='post' action='/intelligence/alerts/{row['id']}/workflow'><input type='hidden' name='status' value='acknowledged'><input type='hidden' name='assigned_operator_id' value='{session.user_id}'><button type='submit'>Acknowledge</button></form>" if row['status'] == 'open' else "")
			   + f"<form method='post' action='/intelligence/alerts/{row['id']}/disposition'><select name='disposition'><option value='confirmed'>Confirmed</option><option value='benign'>Benign</option><option value='unresolved'>Unresolved</option><option value='escalated'>Escalated</option></select><button type='submit'>Resolve</button></form></div></td>"
			   if row['status'] != 'resolved' else f"<td>{self._escape(row['disposition'] or '—')}</td>")
			+ "</tr>"
			for row in alerts
		)
		case_rows = "".join(
			f"<tr><td><a href='/intelligence/cases/{row['id']}'>Case {row['id']}: {self._escape(row['title'])}</a></td><td>{self._escape(row['priority'])}</td><td>{self._escape(row['status'])}</td><td>{row['entity_count']}</td><td>{row['evidence_count']}</td><td>{self._escape(row['updated_at'])}</td></tr>"
			for row in cases
		)
		relationship_rows = "".join(
			f"<tr><td><a href='/users/{row['source_user_id']}'>{self._escape(row['source_name'])}</a></td><td>{self._escape(row['relationship_type'].replace('_', ' '))}</td><td><a href='/users/{row['target_user_id']}'>{self._escape(row['target_name'])}</a></td><td>{float(row['strength']):.1f}</td><td>{row['evidence_count']}</td><td>{self._escape(row['last_observed_at'])}</td></tr>"
			for row in relationships
		)
		report_rows = "".join(
			f"<tr><td>{self._escape(row['report_type'].replace('_', ' '))}</td><td>{self._escape(row['title'])}<br><span class='muted'>{self._escape(row['summary'])}</span></td><td>{self._escape(row['generated_at'])}</td><td><a href='/api/intelligence/reports/{row['id']}'>Export JSON</a></td></tr>"
			for row in reports
		)
		status = self._escape((query.get("status") or [""])[0])
		status_html = f"<p class='status-banner'>{status}</p>" if status else ""
		body = self._render_page(
			"Intelligence",
			session,
			"<section class='hero'><p class='eyebrow'>Operations</p><h1>Intelligence workspace</h1><p class='lede'>Temporal signals, evidence-backed alerts, investigations, entity relationships, and reproducible reports.</p></section>"
			+ status_html
			+ f"<div class='grid'><div class='metric'><div class='label'>Untriaged alerts</div><div class='value'>{summary.open_alerts}</div></div><div class='metric'><div class='label'>Open cases</div><div class='value'>{summary.open_cases}</div></div><div class='metric'><div class='label'>Relationships</div><div class='value'>{summary.relationships}</div></div><div class='metric'><div class='label'>Reports</div><div class='value'>{summary.reports}</div></div></div>"
			+ f"<section class='card'><h2>Alerts</h2>{alert_filters}<div class='table-scroll'><table class='table'><thead><tr><th>{_alert_header('Severity', 'severity')}</th><th>{_alert_header('Subject', 'subject')}</th><th>{_alert_header('Finding', 'finding')}</th><th>{_alert_header('Confidence', 'confidence')}</th><th>{_alert_header('Status', 'status')}</th><th>Disposition</th></tr></thead><tbody>{alert_rows or '<tr><td colspan=6>No alerts</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Cases</h2><div class='table-scroll'><table class='table'><thead><tr><th>{_case_header('Case', 'case')}</th><th>{_case_header('Priority', 'priority')}</th><th>{_case_header('Status', 'status')}</th><th>{_case_header('Entities', 'entities')}</th><th>{_case_header('Evidence', 'evidence')}</th><th>{_case_header('Updated', 'updated')}</th></tr></thead><tbody>{case_rows or '<tr><td colspan=6>No cases</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Relationships</h2><div class='table-scroll'><table class='table'><thead><tr><th>{_relationship_header('Source', 'source')}</th><th>{_relationship_header('Relationship', 'relationship')}</th><th>{_relationship_header('Target', 'target')}</th><th>{_relationship_header('Strength', 'strength')}</th><th>{_relationship_header('Evidence', 'evidence')}</th><th>{_relationship_header('Last observed', 'last_observed')}</th></tr></thead><tbody>{relationship_rows or '<tr><td colspan=6>No relationships</td></tr>'}</tbody></table></div></section>"
			+ "<section class='card'><h2>Reports</h2><form class='toolbar' method='post' action='/intelligence/reports/generate'><select name='report_type'><option value='daily_summary'>Daily summary</option><option value='entity_profile'>Entity profile</option></select><input name='user_id' type='number' min='1' placeholder='User ID (entity only)'><button type='submit'>Generate report</button></form>"
			+ f"<div class='table-scroll'><table class='table'><thead><tr><th>Type</th><th>Report</th><th>Generated</th><th>Export</th></tr></thead><tbody>{report_rows or '<tr><td colspan=4>No reports</td></tr>'}</tbody></table></div></section>",
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_intelligence_case(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		try:
			case_id = int(path.rstrip("/").split("/")[-1])
		except ValueError:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid case")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			case = connection.execute(
				"SELECT * FROM investigation_cases WHERE id=? AND community_id=?",
				(case_id, session.community_id),
			).fetchone()
			entities = connection.execute("SELECT case_entities.*, users.primary_display_name FROM case_entities INNER JOIN users ON users.id=case_entities.user_id WHERE case_id=?", (case_id,)).fetchall()
			evidence = connection.execute("SELECT * FROM case_evidence WHERE case_id=? ORDER BY added_at, id", (case_id,)).fetchall()
			activity = connection.execute("SELECT * FROM case_activity WHERE case_id=? ORDER BY created_at DESC,id DESC", (case_id,)).fetchall()
		finally:
			connection.close()
		if case is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Case not found")
			return
		entity_rows = "".join(f"<tr><td><a href='/users/{row['user_id']}'>{self._escape(row['primary_display_name'])}</a></td><td>{self._escape(row['role'])}</td><td>{self._escape(row['added_at'])}</td></tr>" for row in entities)
		evidence_rows = "".join(f"<tr><td>{self._escape(row['added_at'])}</td><td>{self._escape(row['note'])}</td><td>{self._escape(row['alert_id'] or '—')}</td><td>{self._escape(row['observation_id'] or '—')}</td><td>{self._escape(row['message_id'] or '—')}</td></tr>" for row in evidence)
		activity_rows = "".join(f"<tr><td>{self._escape(row['created_at'])}</td><td>{self._escape(row['operator_id'] or 'system')}</td><td>{self._escape(row['activity_type'])}</td><td>{self._escape(row['body'])}</td></tr>" for row in activity)
		priority_options = "".join(
			f"<option value='{value}'{' selected' if case['priority'] == value else ''}>{value}</option>"
			for value in ("low", "medium", "high", "critical")
		)
		case_status_options = "".join(
			f"<option value='{value}'{' selected' if case['status'] == value else ''}>{value}</option>"
			for value in ("open", "active", "pending", "closed")
		)
		body = self._render_page(
			f"Case {case_id}", session,
			f"<section class='hero'><p class='eyebrow'>Investigation case {case_id}</p><h1>{self._escape(case['title'])}</h1><p class='lede'>{self._escape(case['summary'])}</p><div class='status-row'><span class='status-pill'>{self._escape(case['priority'])}</span><span class='status-pill'>{self._escape(case['status'])}</span><a href='/api/intelligence/cases/{case_id}/export'>Export case JSON</a></div></section>"
			+ f"<section class='card'><h2>Case controls</h2><form method='post' action='/intelligence/cases/{case_id}/action' class='search'><input type='hidden' name='action' value='update'><input name='title' value='{self._escape(case['title'])}' required><input name='summary' value='{self._escape(case['summary'])}' placeholder='Summary'><select name='priority'>{priority_options}</select><select name='status'>{case_status_options}</select><input name='owner_operator_id' type='number' min='1' value='{self._escape(case['owner_operator_id'] or '')}' placeholder='Owner operator ID'><button type='submit'>Update case</button></form><form method='post' action='/intelligence/cases/{case_id}/action' class='search'><input type='hidden' name='action' value='add_note'><input name='body' placeholder='Analyst note' required><button type='submit'>Add note</button></form><form method='post' action='/intelligence/cases/{case_id}/action' class='search'><input type='hidden' name='action' value='add_entity'><input name='user_id' type='number' min='1' placeholder='User ID' required><input name='role' value='subject' placeholder='Role'><button type='submit'>Add entity</button></form><form method='post' action='/intelligence/cases/{case_id}/action' class='search'><input type='hidden' name='action' value='add_evidence'><input name='observation_id' type='number' min='1' placeholder='Observation ID'><input name='message_id' type='number' min='1' placeholder='Message ID'><input name='alert_id' type='number' min='1' placeholder='Alert ID'><input name='note' placeholder='Evidence note'><button type='submit'>Add evidence</button></form></section>"
			+ f"<section class='card'><h2>Entities</h2><div class='table-scroll'><table class='table'><thead><tr><th>Entity</th><th>Role</th><th>Added</th></tr></thead><tbody>{entity_rows or '<tr><td colspan=3>No entities</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Evidence timeline</h2><div class='table-scroll'><table class='table'><thead><tr><th>Added</th><th>Note</th><th>Alert</th><th>Observation</th><th>Message</th></tr></thead><tbody>{evidence_rows or '<tr><td colspan=5>No evidence</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Activity</h2><div class='table-scroll'><table class='table'><thead><tr><th>Time</th><th>Operator</th><th>Activity</th><th>Note</th></tr></thead><tbody>{activity_rows or '<tr><td colspan=4>No activity</td></tr>'}</tbody></table></div></section>",
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_intelligence_case_action(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			case_id = int(path.split("/")[3])
			action = (form.get("action") or [""])[0]
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				if action == "update":
					update_case(connection, case_id, title=(form.get("title") or [""])[0],
						summary=(form.get("summary") or [""])[0], priority=(form.get("priority") or [""])[0],
						status=(form.get("status") or [""])[0],
						owner_operator_id=_optional_int((form.get("owner_operator_id") or [""])[0]),
						operator_id=int(session.user_id), community_id=session.community_id)
				elif action == "add_note":
					add_case_note(connection, case_id, (form.get("body") or [""])[0],
						operator_id=int(session.user_id), community_id=session.community_id)
				elif action == "add_entity":
					add_case_entity(connection, case_id, int((form.get("user_id") or [""])[0]),
						role=(form.get("role") or ["subject"])[0], operator_id=int(session.user_id),
						community_id=session.community_id)
				elif action == "add_evidence":
					add_case_evidence(connection, case_id,
						observation_id=_optional_int((form.get("observation_id") or [""])[0]),
						message_id=_optional_int((form.get("message_id") or [""])[0]),
						alert_id=_optional_int((form.get("alert_id") or [""])[0]),
						note=(form.get("note") or [""])[0], operator_id=int(session.user_id),
						community_id=session.community_id)
				else:
					raise ValueError("unsupported case action")
		except (ValueError, IndexError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid case update")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, f"/intelligence/cases/{case_id}")

	def _serve_intelligence_alert_case(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		try:
			alert_id = int(path.split("/")[3])
		except (ValueError, IndexError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid alert")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			case_id = create_case_from_alert(
				connection, alert_id, operator_id=int(session.user_id),
				community_id=session.community_id,
			)
			connection.commit()
		except ValueError as exc:
			self._send_text(handler, HTTPStatus.NOT_FOUND, str(exc))
			return
		finally:
			connection.close()
		self._redirect(handler, f"/intelligence/cases/{case_id}")

	def _serve_intelligence_alert_disposition(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			alert_id = int(path.split("/")[3])
			disposition = (form.get("disposition") or [""])[0]
			connection = connect_database(self.settings.database_path)
			try:
				initialize_database(connection)
				dispose_alert(
					connection, alert_id, disposition, operator_id=int(session.user_id),
					community_id=session.community_id,
				)
				connection.commit()
			finally:
				connection.close()
		except (ValueError, IndexError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid disposition")
			return
		self._redirect(handler, "/intelligence?status=Alert+resolved")

	def _serve_intelligence_alert_workflow(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		try:
			alert_id = int(path.split("/")[3])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				update_alert_workflow(connection, alert_id,
					status=(form.get("status") or [""])[0] or None,
					assigned_operator_id=_optional_int((form.get("assigned_operator_id") or [""])[0]),
					suppress_until=(form.get("suppress_until") or [""])[0] or None,
					operator_id=int(session.user_id), community_id=session.community_id)
		except (ValueError, IndexError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid alert update")
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._redirect(handler, "/intelligence?status=Alert+updated")

	def _serve_intelligence_report_generate(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		form = self._read_form_body(handler)
		if form is None:
			return
		report_type = (form.get("report_type") or ["daily_summary"])[0]
		user_raw = (form.get("user_id") or [""])[0].strip()
		try:
			user_id = int(user_raw) if user_raw else None
		except ValueError:
			self._redirect(handler, "/intelligence?status=Invalid+user+ID")
			return
		if report_type == "entity_profile" and user_id is None:
			self._redirect(handler, "/intelligence?status=Entity+profile+requires+a+user+ID")
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			report_id = generate_intelligence_report(
				connection, user_id=user_id, report_type=report_type,
				community_id=session.community_id,
			)
			connection.commit()
		except ValueError as exc:
			self._redirect(handler, f"/intelligence?status={quote(str(exc))}")
			return
		finally:
			connection.close()
		self._redirect(handler, f"/api/intelligence/reports/{report_id}")

	def _serve_api_intelligence(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		alert_sort, alert_dir = self._normalize_alert_sort(query)
		case_sort, case_dir = self._normalize_case_sort(query)
		relationship_sort, relationship_dir = self._normalize_relationship_sort(query)
		alert_order = self._alert_order_clause(alert_sort, alert_dir)
		case_order = self._case_order_clause(case_sort, case_dir)
		relationship_order = self._relationship_order_clause(relationship_sort, relationship_dir)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			summary = intelligence_summary(connection, tenant=TenantContext(session.community_id))
			alerts = [dict(row) for row in connection.execute(
				"""SELECT intelligence_alerts.*, users.primary_display_name
				   FROM intelligence_alerts
				   LEFT JOIN users ON users.id=intelligence_alerts.user_id
				   WHERE intelligence_alerts.community_id=?
				   ORDER BY """ + alert_order + " LIMIT 500"
			, (session.community_id,)).fetchall()]
			cases = [dict(row) for row in connection.execute(
				"""SELECT investigation_cases.*, COUNT(DISTINCT case_entities.user_id) AS entity_count,
				          COUNT(DISTINCT case_evidence.id) AS evidence_count
				   FROM investigation_cases
				   LEFT JOIN case_entities ON case_entities.case_id=investigation_cases.id
				   LEFT JOIN case_evidence ON case_evidence.case_id=investigation_cases.id
				   WHERE investigation_cases.community_id=?
				   GROUP BY investigation_cases.id ORDER BY """ + case_order + " LIMIT 500"
			, (session.community_id,)).fetchall()]
			relationships = [dict(row) for row in connection.execute(
				"""SELECT entity_relationships.*, source.primary_display_name AS source_name,
				          target.primary_display_name AS target_name
				   FROM entity_relationships
				   INNER JOIN users source ON source.id=entity_relationships.source_user_id
				   INNER JOIN users target ON target.id=entity_relationships.target_user_id
				   WHERE entity_relationships.community_id=?
				   ORDER BY """ + relationship_order + " LIMIT 500"
			, (session.community_id,)).fetchall()]
			reports = [dict(row) for row in connection.execute(
				"""SELECT id,report_type,subject_user_id,title,summary,generated_at,generator_version
				   FROM intelligence_reports WHERE community_id=? ORDER BY generated_at DESC LIMIT 500""",
				(session.community_id,),
			).fetchall()]
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {
			"summary": asdict(summary),
			"sort": {
				"alerts": {"by": alert_sort, "dir": alert_dir},
				"cases": {"by": case_sort, "dir": case_dir},
				"relationships": {"by": relationship_sort, "dir": relationship_dir},
			},
			"alerts": alerts, "cases": cases, "relationships": relationships, "reports": reports,
		})

	def _serve_api_intelligence_report(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		try:
			report_id = int(path.rstrip("/").split("/")[-1])
		except ValueError:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_report_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			row = connection.execute(
				"SELECT * FROM intelligence_reports WHERE id=? AND community_id=?",
				(report_id, session.community_id),
			).fetchone()
		finally:
			connection.close()
		if row is None:
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "report_not_found"})
			return
		payload = dict(row)
		payload["content"] = json.loads(str(payload.pop("content_json")))
		payload["evidence"] = json.loads(str(payload.pop("evidence_json")))
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_case(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		try:
			case_id = int(path.rstrip("/").split("/")[-1])
		except ValueError:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_case_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if handler.command == "POST":
				payload = self._read_json_body(handler)
				if payload is None:
					return
				action = str(payload.get("action") or "update").strip().casefold()
				with connection:
					if action == "update":
						update_case(connection, case_id, title=_optional_string(payload.get("title")),
							summary=_optional_string(payload.get("summary")), priority=_optional_string(payload.get("priority")),
							status=_optional_string(payload.get("status")), owner_operator_id=_optional_int(payload.get("owner_operator_id")),
							operator_id=int(session.user_id), community_id=session.community_id)
					elif action == "add_entity":
						add_case_entity(connection, case_id, int(payload["user_id"]),
							role=str(payload.get("role") or "subject"), operator_id=int(session.user_id),
							community_id=session.community_id)
					elif action == "add_evidence":
						add_case_evidence(connection, case_id, observation_id=_optional_int(payload.get("observation_id")),
							message_id=_optional_int(payload.get("message_id")), alert_id=_optional_int(payload.get("alert_id")),
							signal_history_id=_optional_int(payload.get("signal_history_id")), note=str(payload.get("note") or ""),
							operator_id=int(session.user_id), community_id=session.community_id)
					elif action == "add_note":
						add_case_note(connection, case_id, str(payload.get("body") or ""),
							operator_id=int(session.user_id), community_id=session.community_id)
					else:
						raise ValueError("unsupported case action")
			case = connection.execute(
				"SELECT * FROM investigation_cases WHERE id=? AND community_id=?",
				(case_id, session.community_id),
			).fetchone()
			if case is None:
				raise ValueError("case not found")
			entities = [dict(row) for row in connection.execute("SELECT * FROM case_entities WHERE case_id=? ORDER BY added_at", (case_id,))]
			evidence = [dict(row) for row in connection.execute("SELECT * FROM case_evidence WHERE case_id=? ORDER BY added_at,id", (case_id,))]
			activity = [dict(row) for row in connection.execute("SELECT * FROM case_activity WHERE case_id=? ORDER BY created_at,id", (case_id,))]
		except (ValueError, KeyError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"case": dict(case), "entities": entities, "evidence": evidence, "activity": activity})

	def _serve_api_case_export(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler, permission="exports.create")
		if session is None:
			return
		try:
			case_id = int(path.rstrip("/").split("/")[-2])
		except ValueError:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_case_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			case = connection.execute(
				"SELECT * FROM investigation_cases WHERE id=? AND community_id=?",
				(case_id, session.community_id),
			).fetchone()
			if case is None:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "case_not_found"})
				return
			payload = {
				"case": dict(case),
				"entities": [dict(row) for row in connection.execute(
					"""SELECT ce.*,u.primary_display_name FROM case_entities ce JOIN users u ON u.id=ce.user_id
					   WHERE ce.case_id=? ORDER BY ce.added_at""", (case_id,))],
				"evidence": [dict(row) for row in connection.execute(
					"SELECT * FROM case_evidence WHERE case_id=? ORDER BY added_at,id", (case_id,))],
				"activity": [dict(row) for row in connection.execute(
					"SELECT * FROM case_activity WHERE case_id=? ORDER BY created_at,id", (case_id,))],
				"exported_at": datetime.now(timezone.utc).isoformat(),
				"exported_by_operator_id": int(session.user_id),
			}
		finally:
			connection.close()
		self._audit_operator_action(int(session.user_id), "case.exported", "investigation_case", case_id, {})
		self._send_bytes(handler, HTTPStatus.OK, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
			"application/json", f"qbot4k-case-{case_id}.json")

	def _serve_api_alert_workflow(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			alert_id = int(path.rstrip("/").split("/")[-1])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				update_alert_workflow(connection, alert_id, status=_optional_string(payload.get("status")),
					assigned_operator_id=_optional_int(payload.get("assigned_operator_id")),
					suppress_until=_optional_string(payload.get("suppress_until")), operator_id=int(session.user_id),
					community_id=session.community_id)
		except (ValueError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "updated", "alert_id": alert_id})

	def _serve_api_signals(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		selected_signals, sort_by, sort_dir = self._normalize_signal_query(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			items = list_signal_overview(
				connection,
				signal_keys=selected_signals,
				sort_by=sort_by,
				sort_dir=sort_dir,
				limit=500,
				community_id=session.community_id,
			)
		finally:
			connection.close()
		self._send_json(
			handler,
			HTTPStatus.OK,
			{
				"filters": {"signals": list(selected_signals)},
				"sort": {"by": sort_by, "dir": sort_dir},
				"items": [
					{
						"display_name": display_name,
						**asdict(signal),
						"label": signal.label,
					}
					for display_name, signal in items
				]
			},
		)

	@staticmethod
	def _normalize_signal_query(query: Mapping[str, list[str]]) -> tuple[tuple[str, ...], str, str]:
		selected = tuple(
			dict.fromkeys(
				key.strip()
				for key in query.get("signal", [])
				if key.strip() in SIGNAL_LABELS
			)
		)
		sort_by = (query.get("sort") or ["default"])[0].strip().casefold()
		if sort_by not in {"default", "signal", "value", "confidence", "evidence", "timestamp"}:
			sort_by = "default"
		sort_dir = (query.get("dir") or ["desc"])[0].strip().casefold()
		if sort_dir not in {"asc", "desc"}:
			sort_dir = "desc"
		return selected, sort_by, sort_dir

	def _normalize_user_sort(self, sort_by_raw: str, sort_dir_raw: str) -> tuple[str, str]:
		sort_by = (sort_by_raw or "score").strip().casefold()
		if sort_by not in {"score", "messages", "poweruser", "accounts", "name"}:
			sort_by = "score"
		sort_dir = (sort_dir_raw or "").strip().casefold()
		default_dir = "asc" if sort_by == "name" else "desc"
		if sort_dir not in {"asc", "desc"}:
			sort_dir = default_dir
		return sort_by, sort_dir

	def _normalize_alert_sort(self, query: Mapping[str, list[str]]) -> tuple[str, str]:
		sort_by = (query.get("alert_sort") or query.get("sort") or ["default"])[0].strip().casefold()
		if sort_by not in {"default", "severity", "subject", "finding", "confidence", "status"}:
			sort_by = "default"
		sort_dir = (query.get("alert_dir") or query.get("dir") or [""])[0].strip().casefold()
		default_dir = "asc" if sort_by in {"subject", "finding", "status"} else "desc"
		if sort_dir not in {"asc", "desc"}:
			sort_dir = default_dir
		if sort_by == "default":
			sort_dir = "desc"
		return sort_by, sort_dir

	def _normalize_case_sort(self, query: Mapping[str, list[str]]) -> tuple[str, str]:
		sort_by = (query.get("case_sort") or ["default"])[0].strip().casefold()
		if sort_by not in {"default", "case", "priority", "status", "entities", "evidence", "updated"}:
			sort_by = "default"
		sort_dir = (query.get("case_dir") or [""])[0].strip().casefold()
		default_dir = "asc" if sort_by in {"case", "status"} else "desc"
		if sort_dir not in {"asc", "desc"}:
			sort_dir = default_dir
		if sort_by == "default":
			sort_dir = "desc"
		return sort_by, sort_dir

	def _normalize_relationship_sort(self, query: Mapping[str, list[str]]) -> tuple[str, str]:
		sort_by = (query.get("relationship_sort") or ["default"])[0].strip().casefold()
		if sort_by not in {"default", "source", "relationship", "target", "strength", "evidence", "last_observed"}:
			sort_by = "default"
		sort_dir = (query.get("relationship_dir") or [""])[0].strip().casefold()
		default_dir = "asc" if sort_by in {"source", "relationship", "target"} else "desc"
		if sort_dir not in {"asc", "desc"}:
			sort_dir = default_dir
		if sort_by == "default":
			sort_dir = "desc"
		return sort_by, sort_dir

	def _alert_order_clause(self, sort_by: str, sort_dir: str) -> str:
		direction = "ASC" if sort_dir == "asc" else "DESC"
		severity_rank = "CASE lower(intelligence_alerts.severity) WHEN 'critical' THEN 5 WHEN 'high' THEN 4 WHEN 'medium' THEN 3 WHEN 'low' THEN 2 WHEN 'info' THEN 1 ELSE 0 END"
		status_rank = "CASE lower(intelligence_alerts.status) WHEN 'open' THEN 1 WHEN 'in_case' THEN 2 WHEN 'resolved' THEN 3 ELSE 4 END"
		columns = {
			"severity": severity_rank,
			"subject": "COALESCE(users.primary_display_name, '') COLLATE NOCASE",
			"finding": "intelligence_alerts.title COLLATE NOCASE",
			"confidence": "intelligence_alerts.confidence",
			"status": status_rank,
		}
		if sort_by == "default":
			return status_rank + " ASC, intelligence_alerts.created_at DESC, intelligence_alerts.id DESC"
		return columns[sort_by] + f" {direction}, intelligence_alerts.created_at DESC, intelligence_alerts.id DESC"

	def _case_order_clause(self, sort_by: str, sort_dir: str) -> str:
		direction = "ASC" if sort_dir == "asc" else "DESC"
		priority_rank = "CASE lower(investigation_cases.priority) WHEN 'critical' THEN 5 WHEN 'high' THEN 4 WHEN 'medium' THEN 3 WHEN 'low' THEN 2 WHEN 'info' THEN 1 ELSE 0 END"
		status_rank = "CASE lower(investigation_cases.status) WHEN 'open' THEN 1 WHEN 'active' THEN 2 WHEN 'pending' THEN 3 WHEN 'closed' THEN 4 ELSE 5 END"
		columns = {
			"case": "investigation_cases.title COLLATE NOCASE",
			"priority": priority_rank,
			"status": status_rank,
			"entities": "entity_count",
			"evidence": "evidence_count",
			"updated": "investigation_cases.updated_at",
		}
		if sort_by == "default":
			return "investigation_cases.updated_at DESC, investigation_cases.id DESC"
		return columns[sort_by] + f" {direction}, investigation_cases.updated_at DESC, investigation_cases.id DESC"

	def _relationship_order_clause(self, sort_by: str, sort_dir: str) -> str:
		direction = "ASC" if sort_dir == "asc" else "DESC"
		columns = {
			"source": "source.primary_display_name COLLATE NOCASE",
			"relationship": "entity_relationships.relationship_type COLLATE NOCASE",
			"target": "target.primary_display_name COLLATE NOCASE",
			"strength": "entity_relationships.strength",
			"evidence": "entity_relationships.evidence_count",
			"last_observed": "entity_relationships.last_observed_at",
		}
		if sort_by == "default":
			return "entity_relationships.strength DESC, entity_relationships.last_observed_at DESC, entity_relationships.id DESC"
		return columns[sort_by] + f" {direction}, entity_relationships.last_observed_at DESC, entity_relationships.id DESC"

	def _normalize_analytics_sorts(self, query: Mapping[str, list[str]]) -> dict[str, tuple[str, str]]:
		specs = {
			"topics": ({"topic_kind", "label", "velocity", "community_count", "unusualness"}, "unusualness"),
			"graph": ({"user_id", "pagerank", "betweenness", "is_bridge", "cluster_id", "influence_score"}, "influence_score"),
			"identity_suggestions": ({"id", "left_platform_account_id", "right_platform_account_id", "confidence", "status"}, "confidence"),
			"cohort_anomalies": ({"user_id", "cohort_key", "signal_key", "z_score", "direction", "confidence"}, "z_score"),
			"evaluation": ({"model_key", "model_version", "sample_size", "calculated_at"}, "calculated_at"),
		}
		result: dict[str, tuple[str, str]] = {}
		for table_name, (allowed, default_sort) in specs.items():
			sort_by = (query.get(f"{table_name}_sort") or [default_sort])[0].strip().casefold()
			if sort_by not in allowed:
				sort_by = default_sort
			sort_dir = (query.get(f"{table_name}_dir") or [""])[0].strip().casefold()
			default_dir = "asc" if sort_by in self._analytics_text_columns(table_name) else "desc"
			if sort_dir not in {"asc", "desc"}:
				sort_dir = default_dir
			result[table_name] = (sort_by, sort_dir)
		return result

	def _analytics_text_columns(self, table_name: str) -> set[str]:
		return {
			"topics": {"topic_kind", "label"},
			"graph": set(),
			"identity_suggestions": {"status"},
			"cohort_anomalies": {"cohort_key", "signal_key", "direction"},
			"evaluation": {"model_key"},
		}.get(table_name, set())

	def _serve_api_user_detail(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		parts = [part for part in path.split("/") if part]
		if len(parts) != 3:
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
			return
		try:
			user_id = int(parts[2])
		except ValueError:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_user_id"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not user_is_visible(connection, user_id, community_id=session.community_id):
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "user_not_found"})
				return
			profile = get_canonical_user_profile(
				connection, user_id, community_id=session.community_id
			)
			signals = list_user_derived_signals(
				connection, user_id, community_id=session.community_id
			)
			lifecycle_events = list_user_lifecycle_events(
				connection, user_id, community_id=session.community_id
			)
		finally:
			connection.close()
		self._send_json(
			handler,
			HTTPStatus.OK,
			{
				"user": {
					"user_id": profile.user_id,
					"primary_display_name": profile.primary_display_name,
					"current_reputation_score": profile.current_reputation_score,
					"candidate_flag": profile.candidate_flag,
					"score_confidence": profile.score_confidence,
					"score_band": profile.score_band,
					"score_model_version": profile.score_model_version,
					"linked_accounts": [asdict(account) for account in profile.linked_accounts],
					"notes": [dict(note) for note in profile.notes],
				},
				"signals": [{**asdict(signal), "label": signal.label} for signal in signals],
				"lifecycle": [asdict(event) for event in lifecycle_events],
			},
		)

	def _serve_api_link_user(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			user_id = int(payload["user_id"])
			discord_user_id = str(payload["discord_user_id"])
		except (KeyError, ValueError, TypeError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
			return
		platform = str(payload.get("platform") or "discord")
		platform_user_id = str(payload.get("platform_user_id") or discord_user_id)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not user_is_visible(connection, user_id, community_id=session.community_id):
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "user_not_found"})
				return
			account = connection.execute(
				"""SELECT id FROM platform_accounts
				   WHERE platform=? AND platform_user_id=? AND EXISTS (
				       SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
				       AND messages.community_id=?
				   )""",
				(platform, platform_user_id, session.community_id),
			).fetchone()
			if account is None:
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "platform_account_not_found"})
				return
			link_platform_account(
				connection,
				platform=platform,
				platform_user_id=platform_user_id,
				user_id=user_id,
				operator_id=int(session.user_id),
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "linked"})

	def _serve_api_add_note(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		parts = [part for part in path.split("/") if part]
		if len(parts) != 4:
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
			return
		try:
			user_id = int(parts[2])
		except ValueError:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_user_id"})
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		body = str(payload.get("body") or "")
		if not body.strip():
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			if not user_is_visible(connection, user_id, community_id=session.community_id):
				self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "user_not_found"})
				return
			add_user_note(
				connection,
				user_id=user_id,
				operator_id=int(session.user_id),
				body=body,
				community_id=session.community_id,
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "noted"})

	def _serve_api_actions(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			actions = list_recent_actions(connection, community_id=session.community_id)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [action.__dict__ for action in actions]})

	def _serve_api_slo(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, permission="analytics.read")
		if session is None or session.community_id is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			samples = collect_tenant_slo_snapshot(
				connection, tenant=TenantContext(int(session.community_id)),
			)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {
			"community_id": int(session.community_id),
			"items": [asdict(sample) for sample in samples],
		})

	def _serve_api_reviews(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			reviews = list_open_reviews(connection, community_id=session.community_id)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [review.__dict__ for review in reviews]})

	def _serve_api_review_resolve(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			review_id = int(path.split("/")[4])
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				action_id = resolve_review(connection, review_id,
					resolution=str(payload.get("resolution") or ""),
					tenant=TenantContext(session.community_id),
					actor=ActorAttribution("operator", int(session.user_id)),
					note=str(payload.get("note") or ""), action_type=_optional_string(payload.get("action_type")),
					duration_seconds=int(payload.get("duration_seconds") or 600))
		except (ValueError, IndexError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "resolved", "review_id": review_id, "action_id": action_id})

	def _serve_api_rules(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			rules = list_moderation_rules(connection, community_id=session.community_id)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [item.__dict__ for item in rules]})

	def _serve_api_rule_save(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler, admin_only=True)
		if session is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			connection = connect_database(self.settings.database_path)
			initialize_database(connection)
			with connection:
				rule_id = save_moderation_rule(connection, name=str(payload.get("name") or ""),
					rule_type=str(payload.get("rule_type") or ""), pattern=str(payload.get("pattern") or ""),
					severity=str(payload.get("severity") or ""),
					auto_enforce_action=_optional_string(payload.get("auto_enforce_action")),
					enabled=bool(payload.get("enabled", True)),
					enforcement_mode=str(payload.get("enforcement_mode") or "shadow"),
					action_duration_seconds=int(payload.get("action_duration_seconds") or 600),
					operator_id=int(session.user_id), community_id=session.community_id)
		except (ValueError, TypeError) as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
			return
		finally:
			if "connection" in locals():
				connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "saved", "rule_id": rule_id})

	def _serve_api_health(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		database_state = database_health(self.settings.database_path)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			operations = operational_readiness_snapshot(connection)
		finally:
			connection.close()
		now = datetime.now(timezone.utc)
		services = ("web", "jobs", "twitch", "discord")
		services_detail = {
			service_name: {
				"status": self._service_status(service_name),
				"started_at": self.service_started_at.get(service_name),
				"uptime_seconds": self._uptime_seconds(self.service_started_at.get(service_name), now),
			}
			for service_name in services
		}
		payload = {
			"status": self._overall_status(database_state, services),
			"table_count": int(database_state.get("table_count") or 0),
			"database": database_state,
			"operations": operations,
			"services": {service_name: details["status"] for service_name, details in services_detail.items()},
			"services_detail": services_detail,
			"uptime": {
				"app_started_at": self.app_started_at,
				"app_uptime_seconds": self._uptime_seconds(self.app_started_at, now),
			},
		}
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_audit(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		if self._require_session(handler, admin_only=True) is None:
			return
		self._send_json(handler, HTTPStatus.OK, {"items": self._audit_items(query)})

	def _audit_operator_action(self, operator_id: int, action_type: str, entity_type: str,
		entity_id: int | None, payload: Mapping[str, object]) -> None:
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"""INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
					   VALUES ('operator',?,?,?,?,?)""",
					(operator_id, action_type, entity_type, entity_id, json.dumps(dict(payload), sort_keys=True)),
				)
		finally:
			connection.close()

	def _service_status(self, service_name: str) -> str:
		if service_name not in self.settings.enabled_services:
			return "disabled"
		status = str(self.service_states.get(service_name) or "down").strip().casefold()
		return status or "down"

	def _overall_status(self, database_state: Mapping[str, object], services: tuple[str, ...]) -> str:
		if str(database_state.get("status") or "").casefold() != "ready":
			return "degraded"
		for service_name in services:
			if self._service_status(service_name) not in {"ready", "disabled"}:
				return "degraded"
		return "ready"

	def _render_overview_connector_status(self) -> str:
		discord_status = self._service_status("discord")
		twitch_status = self._service_status("twitch")
		discord_text = self._status_description("discord", discord_status)
		twitch_text = self._status_description("twitch", twitch_status)
		return (
			"<section class='card'>"
			+ "<h2>Connector Status</h2>"
			+ "<p class='lede'>Live indicators for Discord and Twitch connectivity/authentication.</p>"
			+ "<div class='grid'>"
			+ f"<div class='metric'><div class='label'>Discord</div>{self._render_status_pill(discord_status)}<div class='muted'>{self._escape(discord_text)}</div></div>"
			+ f"<div class='metric'><div class='label'>Twitch</div>{self._render_status_pill(twitch_status)}<div class='muted'>{self._escape(twitch_text)}</div></div>"
			+ "</div></section>"
		)

	def _status_description(self, service_name: str, status: str) -> str:
		if status == "ready":
			return "Connected and authenticated"
		if status == "disabled":
			return f"{service_name.capitalize()} service is disabled"
		if status == "auth_failed":
			return "Down: authentication failed"
		if status in {"connecting", "reconnecting"}:
			return "Connecting"
		if status == "idle":
			return "Idle"
		return "Down"

	def _render_status_pill(self, status: str) -> str:
		normalized = status.strip().casefold()
		if normalized == "ready":
			css_class = "status-up"
			label = "ready"
		elif normalized == "disabled":
			css_class = "status-disabled"
			label = "disabled"
		elif normalized in {"connecting", "reconnecting", "idle"}:
			css_class = "status-warn"
			label = normalized
		else:
			css_class = "status-down"
			label = normalized or "down"
		return f"<span class='status-pill {css_class}'>{self._escape(label)}</span>"

	def _uptime_seconds(self, started_at: str | None, now: datetime) -> int | None:
		if not started_at:
			return None
		try:
			parsed = datetime.fromisoformat(started_at)
		except ValueError:
			return None
		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=timezone.utc)
		seconds = int((now - parsed.astimezone(timezone.utc)).total_seconds())
		return max(seconds, 0)

	def _format_uptime(self, seconds: int | None) -> str:
		if seconds is None:
			return "n/a"
		days, rem = divmod(seconds, 86400)
		hours, rem = divmod(rem, 3600)
		minutes, secs = divmod(rem, 60)
		if days > 0:
			return f"{days}d {hours}h {minutes}m"
		if hours > 0:
			return f"{hours}h {minutes}m {secs}s"
		if minutes > 0:
			return f"{minutes}m {secs}s"
		return f"{secs}s"

	def _render_reliability_graph(self, service_name: str, history: list[object]) -> str:
		if not history:
			return "<p class='muted'>No reliability samples yet.</p>"
		bars = []
		for row in history:
			bucket_start = str(row["bucket_start"])
			is_up = int(row["is_up"]) == 1
			status = str(row["status"])
			bar_class = "up" if is_up else "down"
			title = f"{service_name} {bucket_start} {status}"
			bars.append(
				f"<span class='reliability-bar {bar_class}' title='{self._escape(title)}' aria-label='{self._escape(title)}'></span>"
			)
		return "<div class='reliability-track'>" + "".join(bars) + "</div>"

	def _summarize_outages(self, history: list[object]) -> list[dict[str, object]]:
		outages: list[dict[str, object]] = []
		active_start: str | None = None
		active_status = "down"
		active_buckets = 0
		last_bucket: str | None = None

		for row in history:
			bucket_start = str(row["bucket_start"])
			is_up = int(row["is_up"]) == 1
			status = str(row["status"])
			if not is_up:
				if active_start is None:
					active_start = bucket_start
					active_status = status
					active_buckets = 0
				active_buckets += 1
				last_bucket = bucket_start
				continue

			if active_start is None:
				last_bucket = bucket_start
				continue

			outages.append(
				{
					"started_at": active_start,
					"ended_at": self._bucket_end_iso(last_bucket or active_start),
					"duration_minutes": active_buckets,
					"status": active_status,
				}
			)
			active_start = None
			active_buckets = 0
			last_bucket = bucket_start

		if active_start is not None:
			outages.append(
				{
					"started_at": active_start,
					"ended_at": "ongoing",
					"duration_minutes": active_buckets,
					"status": active_status,
				}
			)

		outages.reverse()
		return outages[:8]

	def _render_outage_table(self, outages: list[dict[str, object]]) -> str:
		if not outages:
			return "<p class='muted'>No outages in the sampled window.</p>"
		rows = "".join(
			"<tr>"
			+ f"<td>{self._escape(str(item['started_at']))}</td>"
			+ f"<td>{self._escape(str(item['ended_at']))}</td>"
			+ f"<td>{self._escape(str(item['duration_minutes']))}m</td>"
			+ f"<td>{self._escape(str(item['status']))}</td>"
			+ "</tr>"
			for item in outages
		)
		return (
			"<div class='table-scroll'><table class='table'><thead><tr><th>Outage start</th><th>Outage end</th><th>Duration</th><th>Status</th></tr></thead><tbody>"
			+ rows
			+ "</tbody></table></div>"
		)

	def _bucket_end_iso(self, bucket_start: str) -> str:
		try:
			parsed = datetime.fromisoformat(bucket_start)
		except ValueError:
			return bucket_start
		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=timezone.utc)
		return (parsed + timedelta(minutes=1)).isoformat()

	def _render_community_switcher(self, session: DashboardSession) -> str:
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			communities = list_operator_communities(connection, int(session.user_id))
		finally:
			connection.close()
		options = "".join(
			f"<option value='{int(row['id'])}'"
			f"{' selected' if int(row['id']) == session.community_id else ''}>"
			f"{self._escape(row['name'])}</option>"
			for row in communities
		)
		return (
			"<form method='post' action='/community/switch' class='community-switcher'>"
			"<label for='activeCommunity'>Community</label>"
			f"<select id='activeCommunity' name='community_id' onchange='this.form.submit()'>{options}</select>"
			"<noscript><button type='submit'>Switch</button></noscript></form>"
		)

	def _render_page(self, title: str, session: DashboardSession, content: str) -> str:
		page_class = " command-page" if title.casefold() == "commands" else ""
		navigation = (
			("/dashboard", "Overview", "_serve_dashboard"),
			("/system-health", "Health", "_serve_system_health"),
			("/live-ops", "Live Ops", "_serve_live_ops"),
			("/signals", "Signals", "_serve_signals"),
			("/intelligence", "Intelligence", "_serve_intelligence"),
			("/search", "Search", "_serve_search"),
			("/analytics", "Analytics", "_serve_analytics"),
			("/users", "Users", "_serve_users"),
			("/moderation", "Moderation", "_serve_moderation"),
			("/audit", "Audit", "_serve_audit"),
			("/commands", "Commands", "_serve_commands"),
			("/announcements", "Announcements", "_serve_announcements"),
			("/onboarding", "Onboarding", "_serve_onboarding"),
			("/integrations", "Integrations", "_serve_integrations"),
			("/settings", "Settings", "_serve_settings"),
		)
		allowed_links: list[tuple[str, str]] = []
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			for path, label, handler_name in navigation:
				capability = DASHBOARD_SURFACE_POLICIES[handler_name].capability
				if capability == "public.access" or operator_has_permission(
					connection, operator_id=int(session.user_id),
					community_id=int(session.community_id), permission=capability,
				):
					allowed_links.append((path, label))
		finally:
			connection.close()
		navigation_html = "".join(
			f"<a href='{path}'>{self._escape(label)}</a>" for path, label in allowed_links
		)
		breadcrumb = (
			"<nav class='breadcrumb' aria-label='Breadcrumb'><a href='/dashboard'>Overview</a>"
			+ ("" if title == "Overview" else
			   f"<span aria-hidden='true'>/</span><span aria-current='page'>{self._escape(title)}</span>")
			+ "</nav>"
		)
		return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{self._escape(title)} · QBot4K</title>
<style>
:root {{ color-scheme: dark; --bg: #090b0c; --panel: #111416; --panel-2: #191d20; --panel-raised: #202529; --text: #f1f0ea; --muted: #9da3a3; --accent: #f0a629; --accent-strong: #ffc14d; --border: #343a3d; --border-strong: #596064; --danger: #d9534f; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 15px/1.5 "IBM Plex Sans", "Helvetica Neue", sans-serif; background-color: var(--bg); background-image: linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.018) 1px, transparent 1px); background-size: 32px 32px; color: var(--text); }}
a {{ color: var(--accent); text-decoration: none; }}
.shell {{ display: grid; grid-template-columns: 252px 1fr; min-height: 100vh; }}
.nav {{ padding: 26px 20px; border-right: 1px solid var(--border); background: rgba(9,11,12,.96); }}
.brand {{ display: flex; align-items: center; gap: 10px; padding-bottom: 20px; border-bottom: 1px solid var(--border); font-family: "Arial Narrow", "IBM Plex Sans Condensed", sans-serif; font-size: 22px; font-weight: 800; letter-spacing: 0; margin-bottom: 18px; text-transform: uppercase; }}
.brand::before {{ content: "Q"; display: grid; place-items: center; width: 32px; height: 32px; border: 1px solid var(--accent); color: var(--accent); font-size: 17px; }}
.nav > .muted {{ font-family: "IBM Plex Mono", "Cascadia Mono", monospace; font-size: 11px; text-transform: uppercase; }}
.nav nav {{ display: grid; gap: 2px; }}
.nav a {{ display: block; padding: 8px 10px; border-left: 2px solid transparent; color: #c8cccb; font-size: 13px; font-weight: 650; text-transform: uppercase; }}
.nav a:hover {{ background: var(--panel-2); color: var(--text); border-left-color: var(--border-strong); }}
.nav a[aria-current='page'] {{ background: var(--panel-2); color: var(--accent-strong); border-left-color: var(--accent); }}
.community-switcher {{ display: grid; gap: 6px; margin: 18px 0; }}
.community-switcher label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
.community-switcher select {{ width: 100%; }}
.breadcrumb {{ display: flex; gap: 8px; align-items: center; margin-bottom: 16px; color: var(--muted); font-size: 12px; text-transform: uppercase; }}
.breadcrumb span[aria-current='page'] {{ color: var(--text); }}
.main {{ padding: 34px clamp(20px, 4vw, 56px) 56px; min-width: 0; overflow-x: hidden; }}
.hero, .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 2px; padding: 22px; box-shadow: none; }}
.hero {{ position: relative; margin-bottom: 18px; border-top: 3px solid var(--accent); background: linear-gradient(110deg, #171b1d 0%, #101315 72%); }}
.hero::after {{ content: "Q4K / OPS"; position: absolute; top: 18px; right: 20px; color: #6f7678; font: 10px/1 "IBM Plex Mono", "Cascadia Mono", monospace; }}
.card + .card {{ margin-top: 18px; }}
.eyebrow {{ text-transform: uppercase; letter-spacing: .12em; color: var(--accent); font: 11px/1.3 "IBM Plex Mono", "Cascadia Mono", monospace; margin: 0 0 12px; }}
h1, h2, h3 {{ font-family: "Arial Narrow", "IBM Plex Sans Condensed", sans-serif; letter-spacing: 0; text-transform: uppercase; }}
h1 {{ max-width: 18ch; font-size: clamp(30px, 4vw, 52px); line-height: .98; margin: 0 0 14px; }}
h2 {{ font-size: 18px; margin: 0 0 12px; }}
h3 {{ font-size: 14px; }}
.lede {{ color: var(--muted); max-width: 62ch; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 18px 0; }}
.metric {{ position: relative; background: var(--panel); border: 1px solid var(--border); border-radius: 2px; padding: 16px 18px 18px; }}
.metric::before {{ content: ""; position: absolute; top: -1px; left: -1px; width: 28px; border-top: 2px solid var(--accent); }}
.metric .label {{ color: var(--muted); font: 10px/1.2 "IBM Plex Mono", "Cascadia Mono", monospace; text-transform: uppercase; }}
.metric .value {{ margin-top: 10px; font: 700 30px/1 "Arial Narrow", "IBM Plex Sans Condensed", sans-serif; font-variant-numeric: tabular-nums; }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0 20px; }}
.status-banner {{ margin: 0; padding: 10px 14px; border-radius: 2px; background: rgba(240,166,41,.1); color: var(--accent-strong); border: 1px solid rgba(240,166,41,.4); }}
.status-row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
.status-pill {{ display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 4px 10px; border: 1px solid transparent; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; font-weight: 700; }}
.status-up {{ background: rgba(76, 201, 142, .18); color: #8cf0c2; border-color: rgba(76, 201, 142, .45); }}
.status-warn {{ background: rgba(255, 200, 87, .16); color: #ffd98a; border-color: rgba(255, 200, 87, .45); }}
.status-down {{ background: rgba(255, 107, 107, .16); color: #ff9f9f; border-color: rgba(255, 107, 107, .45); }}
.status-disabled {{ background: rgba(154, 167, 189, .15); color: #c8d1df; border-color: rgba(154, 167, 189, .4); }}
.sigma-rating {{
	display: inline-flex;
	align-items: center;
	padding: 0.3rem 0.65rem;
	border: 1px solid var(--border);
	border-radius: 999px;
	font-weight: 700;
	font-variant-numeric: tabular-nums;
}}
.reliability-track {{ display: flex; align-items: flex-end; gap: 1px; padding: 12px; margin-top: 12px; border: 1px solid var(--border); border-radius: 14px; background: rgba(0, 0, 0, .18); overflow-x: auto; }}
.reliability-bar {{ width: 2px; min-width: 2px; height: 26px; border-radius: 1px; }}
.reliability-bar.up {{ background: #44d27f; }}
.reliability-bar.down {{ background: #ff6b6b; }}
.outage-table {{ margin-top: 14px; }}
table {{ width: 100%; max-width: 100%; border-collapse: collapse; margin-top: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: 0; overflow: hidden; display: block; overflow-x: auto; }}
th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ background: #0d1011; color: var(--muted); font: 10px/1.2 "IBM Plex Mono", "Cascadia Mono", monospace; text-transform: uppercase; letter-spacing: .08em; }}
tbody tr:hover {{ background: rgba(255,255,255,.025); }}
form.search {{ display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }}
.row-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.row-actions form {{ margin: 0; }}
.moderation-bulk {{ display: grid; grid-template-columns: minmax(110px, .8fr) minmax(90px, .6fr) minmax(140px, 1fr) minmax(160px, 1fr) auto auto; gap: 0; align-items: stretch; }}
.moderation-bulk > * {{ min-width: 0; }}
.signal-filter {{ display: flex; align-items: flex-end; gap: 14px; margin: 18px 0; padding: 18px; border: 1px solid var(--border); border-radius: 18px; background: var(--panel); flex-wrap: wrap; }}
.signal-filter label {{ display: grid; gap: 8px; min-width: min(100%, 360px); flex: 1; }}
.signal-filter select[multiple] {{ width: 100%; min-height: 150px; }}
.signal-filter-actions {{
    display: grid;
    grid-template-columns: 1fr;
    align-items: stretch;
	gap: 0.5rem;
}}
input {{ flex: 1; min-width: 0; padding: 11px 12px; border-radius: 2px; border: 1px solid var(--border-strong); background: #0d1011; color: var(--text); max-width: 100%; }}
textarea {{ width: 100%; min-width: 0; max-width: 100%; padding: 11px 12px; border-radius: 2px; border: 1px solid var(--border-strong); background: #0d1011; color: var(--text); resize: vertical; }}
select {{ min-width: 0; max-width: 100%; padding: 11px 12px; border-radius: 2px; border: 1px solid var(--border-strong); background: #0d1011; color: var(--text); }}
input:focus, textarea:focus, select:focus, button:focus-visible, a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
button {{ padding: 11px 16px; border: 1px solid var(--accent); border-radius: 2px; background: var(--accent); color: #111; font: 700 12px/1.2 "IBM Plex Sans", "Helvetica Neue", sans-serif; text-transform: uppercase; cursor: pointer; }}
button:hover {{ background: var(--accent-strong); border-color: var(--accent-strong); }}
button.danger {{ background: transparent; border-color: var(--danger); color: #ff8b87; }}
.columns {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
.sticky-link-panel {{ position: sticky; top: 12px; z-index: 5; margin-bottom: 18px; }}
.checkbox {{ display: inline-flex; align-items: center; gap: 8px; }}
.checkbox input {{ width: auto; flex: none; }}
.template-dialog {{ width: min(920px, calc(100vw - 32px)); border: 1px solid var(--border); border-radius: 24px; background: var(--panel); color: var(--text); padding: 0; box-shadow: 0 30px 80px rgba(0,0,0,.45); }}
.template-dialog::backdrop {{ background: rgba(0,0,0,.6); backdrop-filter: blur(6px); }}
.template-dialog-inner {{ padding: 22px; }}
.template-dialog-header {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; }}
.template-dialog-header h2 {{ margin: 0; }}
.template-dialog table {{ margin-top: 16px; }}
.template-dialog code {{ color: var(--accent); }}
table input:not([type='checkbox']),
table textarea {{ 
  width: 100%;
  min-width: 0;                 /* allow the cell to control size */
  box-sizing: border-box;
}}

.table-scroll {{
  display: block;                 /* explicit – do NOT use table here */
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}}

.table,
.card table {{
  display: table;
  width: 100%;
  table-layout: auto;
  border-collapse: collapse;
}}

.command-page .builtin-title-input {{
  width: 100%;
  min-width: 140px;
  box-sizing: border-box;
}}

/* Keep the rest of your form / row-actions rules – they are fine */
.command-page .new-command-form {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 14px 0 10px;
  flex-wrap: wrap;
}}
.command-page .new-command-form .new-command-name {{
  flex: 1 1 240px;
  min-width: 210px;
}}
.command-page .new-command-form .new-command-response {{
  flex: 2 1 380px;
  min-width: 260px;
}}
.command-page .new-command-form .new-command-enabled {{
  margin-left: 8px;
  margin-right: 2px;
  white-space: nowrap;
}}
.command-page .new-command-form button {{
  white-space: nowrap;
}}

.command-page table td .checkbox {{
  display: inline-flex;
  justify-content: flex-start;
  margin: 0;
}}
.command-page table td .checkbox input {{
  margin: 0;
}}
.command-page .row-actions {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}}
.command-page .row-actions form {{
  margin: 0;
}}
.command-page .insert-row td {{
  background: rgba(120, 220, 202, .04);
}}
.command-page .insert-row code {{ color: var(--accent); }}
@media (max-width: 1100px) {{ .command-page .new-command-form {{ display: grid; grid-template-columns: 1fr; align-items: stretch; }} .command-page .new-command-form > * {{ width: 100%; min-width: 0; }} .command-page .new-command-form .new-command-enabled {{ margin-left: 0; margin-right: 0; }} .command-page .new-command-form button {{ width: 100%; }} }}
@media (max-width: 900px) {{ .shell {{ grid-template-columns: minmax(0, 1fr); }} .nav {{ width: 100%; min-width: 0; padding: 18px; border-right: 0; border-bottom: 1px solid var(--border); }} .brand {{ margin-bottom: 12px; padding-bottom: 12px; }} .community-switcher {{ grid-template-columns: auto minmax(180px, 320px); align-items: center; margin: 12px 0; }} .nav nav {{ display: flex; width: 100%; max-width: 100%; min-width: 0; gap: 2px; overflow-x: auto; padding-bottom: 5px; scrollbar-width: thin; }} .nav nav a, .nav nav form {{ flex: 0 0 auto; }} .nav nav a {{ border-left: 0; border-bottom: 2px solid transparent; }} .nav nav a:hover, .nav nav a[aria-current='page'] {{ border-left-color: transparent; border-bottom-color: var(--accent); }} .columns {{ grid-template-columns: 1fr; }} .sticky-link-panel {{ position: static; }} }}
@media (max-width: 700px) {{ body {{ font-size: 14px; }} .main {{ padding: 18px 14px 36px; }} .nav {{ padding: 18px 14px; }} .hero, .card {{ padding: 16px; border-radius: 2px; }} .hero::after {{ display: none; }} .grid {{ grid-template-columns: 1fr; }} .toolbar, .status-row, form.search, .command-page .new-command-form, .command-page .row-actions {{ align-items: stretch; }} form.search > *, .toolbar > *, .command-page .new-command-form > *, .command-page .row-actions > * {{ width: 100%; }} .moderation-bulk {{ grid-template-columns: 1fr; gap: 8px; }} button {{ width: 100%; }} .metric .value {{ font-size: 24px; }} th, td {{ padding: 10px 12px; white-space: nowrap; }} .command-page .new-command-form .new-command-name, .command-page .new-command-form .new-command-response {{ min-width: 0; }} .command-page table input:not([type='checkbox']), .command-page table textarea {{ width: 100%; }} .template-dialog {{ width: calc(100vw - 16px); }} .template-dialog-inner {{ padding: 16px; }} }}
</style>
</head>
<body>
<div class='shell'>
<aside class='nav'>
<div class='brand'>QBot4K</div>
<div class='muted'>{self._escape(session.username)} · {self._escape(session.role)}</div>
{self._render_community_switcher(session)}
<nav>
{navigation_html}
<form method='post' action='/logout'><button type='submit'>Logout</button></form>
</nav>
</aside>
<main class='main{page_class}'>{breadcrumb}{content}</main>
</div>
<script>
document.addEventListener("DOMContentLoaded", () => {{
	const currentPath = window.location.pathname.replace(/\\/$/, "") || "/";
	document.querySelectorAll(".nav nav a").forEach(link => {{
		const targetPath = new URL(link.href, window.location.origin).pathname.replace(/\\/$/, "") || "/";
		if (targetPath === currentPath || (targetPath !== "/dashboard" && currentPath.startsWith(targetPath + "/"))) {{
			link.setAttribute("aria-current", "page");
		}}
	}});
  document.querySelectorAll(".reliability-track").forEach((track) => {{
    track.scrollLeft = track.scrollWidth;
  }});
}});
</script>
</body>
</html>"""

	def _render_metric_grid(self, overview) -> str:
		platform_cards = "".join(
			f"<div class='metric'><div class='label'>{self._escape(platform)}</div><div class='value'>{count}</div></div>"
			for platform, count in overview.top_platforms
		)
		channel_cards = "".join(
			f"<div class='metric'><div class='label'>{self._escape(channel)}</div><div class='value'>{count}</div></div>"
			for channel, count in overview.top_channels
		)
		return f"<div class='grid'><div class='metric'><div class='label'>Messages</div><div class='value'>{overview.messages_total}</div></div><div class='metric'><div class='label'>Derived signals</div><div class='value'>{overview.derived_signals}</div></div><div class='metric'><div class='label'>Open reviews</div><div class='value'>{overview.open_reviews}</div></div><div class='metric'><div class='label'>Pending actions</div><div class='value'>{overview.pending_actions}</div></div></div><div class='grid'>{platform_cards}{channel_cards}</div>"

	@staticmethod
	def _format_signal_value(signal_key: str, value: float) -> str:
		if signal_key.endswith("_ratio"):
			return f"{value * 100:.1f}%"
		if signal_key == "risk.composite":
			return f"{value:.1f} / 100"
		if float(value).is_integer():
			return str(int(value))
		return f"{value:.2f}"

	def _send_json(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Mapping[str, object]) -> None:
		response = json.dumps(payload, sort_keys=True).encode("utf-8")
		handler.send_response(status)
		handler.send_header("Content-Type", "application/json")
		self._send_security_headers(handler)
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_quota_exceeded(
		self, handler: BaseHTTPRequestHandler, exc: TenantQuotaExceededError,
	) -> None:
		response = json.dumps({
			"error": "tenant_quota_exceeded", "quota_type": exc.quota_type,
			"retry_after_seconds": exc.retry_after_seconds,
		}, sort_keys=True).encode("utf-8")
		handler.send_response(HTTPStatus.TOO_MANY_REQUESTS)
		handler.send_header("Content-Type", "application/json")
		handler.send_header("Retry-After", str(exc.retry_after_seconds))
		self._send_security_headers(handler)
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_bytes(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, response: bytes,
		content_type: str, filename: str) -> None:
		handler.send_response(status)
		handler.send_header("Content-Type", content_type)
		self._send_security_headers(handler)
		handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_html(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, body: str) -> None:
		response = body.encode("utf-8")
		handler.send_response(status)
		handler.send_header("Content-Type", "text/html; charset=utf-8")
		self._send_security_headers(handler)
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_text(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, text: str) -> None:
		response = text.encode("utf-8")
		handler.send_response(status)
		handler.send_header("Content-Type", "text/plain; charset=utf-8")
		self._send_security_headers(handler)
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _redirect(
		self,
		handler: BaseHTTPRequestHandler,
		location: str,
		extra_headers: Mapping[str, str] | None = None,
		cookies: tuple[str, ...] = (),
	) -> None:
		handler.send_response(HTTPStatus.FOUND)
		handler.send_header("Location", location)
		self._send_security_headers(handler)
		for key, value in (extra_headers or {}).items():
			handler.send_header(key, value)
		for cookie in cookies:
			handler.send_header("Set-Cookie", cookie)
		handler.end_headers()

	@staticmethod
	def _send_security_headers(handler: BaseHTTPRequestHandler) -> None:
		handler.send_header("X-Content-Type-Options", "nosniff")
		handler.send_header("X-Frame-Options", "DENY")
		handler.send_header("Referrer-Policy", "same-origin")
		handler.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'")
		handler.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		if (handler.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().casefold() == "https":
			handler.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

	def _read_json_body(self, handler: BaseHTTPRequestHandler) -> dict[str, object] | None:
		try:
			length = int(handler.headers.get("Content-Length", "0") or 0)
		except (TypeError, ValueError):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
			return None
		if length <= 0:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_body"})
			return None
		if length > 1_048_576:
			self._send_json(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body_too_large"})
			return None
		try:
			payload = json.loads(handler.rfile.read(length).decode("utf-8"))
		except Exception:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
			return None
		if not isinstance(payload, dict):
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
			return None
		return payload

	def _read_form_body(self, handler: BaseHTTPRequestHandler) -> Mapping[str, list[str]] | None:
		try:
			length = int(handler.headers.get("Content-Length", "0") or 0)
		except (TypeError, ValueError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
			return None
		if length <= 0:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Missing form body")
			return None
		if length > 1_048_576:
			self._send_text(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form body too large")
			return None
		try:
			raw_body = handler.rfile.read(length).decode("utf-8")
		except Exception:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid form body")
			return None
		return parse_qs(raw_body, keep_blank_values=False)

	def _escape(self, value: object) -> str:
		text = str(value)
		return (
			text.replace("&", "&amp;")
			.replace("<", "&lt;")
			.replace(">", "&gt;")
			.replace('"', "&quot;")
		)

	def _render_message_with_attachments(self, content_raw: str, attachment_urls: tuple[str, ...]) -> str:
		rendered_content = self._escape(content_raw)
		if not attachment_urls:
			return rendered_content
		links = " ".join(
			f"<a href='{self._escape(url)}' target='_blank' rel='noopener noreferrer'>[{index}]</a>"
			for index, url in enumerate(attachment_urls, start=1)
		)
		if rendered_content:
			return f"{rendered_content} <span class='muted'>{links}</span>"
		return f"<span class='muted'>{links}</span>"
