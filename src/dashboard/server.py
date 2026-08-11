from __future__ import annotations

import logging
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Mapping
from urllib.parse import parse_qs, quote, urlencode, urlparse
from statistics import NormalDist

from ..config import AppSettings
from ..db import (
    connect_database,
	collect_observation,
	database_health,
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
    derived_signal_count,
    list_signal_overview,
    list_user_derived_signals,
)
from ..intelligence.scoring import get_current_social_score
from ..intelligence.workflows import (
	create_case_from_alert,
	dispose_alert,
	generate_intelligence_report,
	intelligence_summary,
)
from ..intelligence.search import list_saved_queries, observation_pivots, save_query, search_observations
from ..intelligence.analytics import analytics_snapshot, review_identity_suggestion
from ..intelligence.events import SUPPORTED_EVENT_TYPES, collect_external_feed_item
from ..jobs import send_manual_twitch_live_announcements
from .auth import (
    DashboardSession,
    build_discord_oauth_url,
    build_oauth_state,
    build_session,
    create_session_cookie,
    determine_operator_role,
    exchange_discord_code_for_token,
    fetch_discord_identity,
    parse_session_cookie,
)
from .moderation import list_open_reviews, list_recent_actions
from .overview import load_overview_snapshot
from .users import (
	get_user_moderation_status,
	list_recent_user_messages,
	list_recent_user_moderation_actions,
	list_user_platform_accounts,
	search_users,
)


def _restart_systemd_service(service_name: str) -> None:
	command = ["systemctl", "restart", service_name]
	if os.geteuid() != 0:
		command = ["sudo", "-n", *command]
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

		if handler.command == "GET" and path in {"/", "/dashboard"}:
			self._serve_dashboard(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/system-health":
			self._serve_system_health(handler)
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
		if handler.command == "POST" and path == "/search/saved":
			self._serve_search_save(handler)
			return True
		if handler.command == "GET" and path == "/analytics":
			self._serve_analytics(handler, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path.startswith("/intelligence/cases/"):
			self._serve_intelligence_case(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/intelligence/alerts/") and path.endswith("/case"):
			self._serve_intelligence_alert_case(handler, path)
			return True
		if handler.command == "POST" and path.startswith("/intelligence/alerts/") and path.endswith("/disposition"):
			self._serve_intelligence_alert_disposition(handler, path)
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
		if handler.command == "GET" and path.startswith("/users/"):
			self._serve_user_messages(handler, path, parse_qs(parsed.query))
			return True
		if handler.command == "GET" and path == "/moderation":
			self._serve_moderation(handler)
			return True
		if handler.command == "GET" and path == "/commands":
			self._serve_commands(handler, parse_qs(parsed.query))
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
		if handler.command == "POST" and path == "/api/external/observations":
			self._serve_api_external_observation(handler)
			return True
		if handler.command == "POST" and path == "/api/events":
			self._serve_api_event(handler)
			return True
		if handler.command == "GET" and path.startswith("/api/intelligence/reports/"):
			self._serve_api_intelligence_report(handler, path)
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
		if handler.command == "GET" and path == "/api/moderation/reviews":
			self._serve_api_reviews(handler)
			return True
		if handler.command == "GET" and path == "/api/health":
			self._serve_api_health(handler)
			return True
		return False

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

	def _request_origin(self, handler: BaseHTTPRequestHandler) -> str | None:
		scheme = (handler.headers.get("X-Forwarded-Proto")
		          or "").split(",", 1)[0].strip() or "http"
		host = (handler.headers.get("X-Forwarded-Host")
		        or handler.headers.get("Host") or "").split(",", 1)[0].strip()
		if not host:
			return None
		return f"{scheme}://{host}"

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

	def _require_session(self, handler: BaseHTTPRequestHandler,
	                     admin_only: bool = False) -> DashboardSession | None:
		session = self._read_session(handler)
		if session is None:
			self._redirect(handler, "/login")
			return None
		if admin_only and session.role != "admin":
			self._send_text(handler, HTTPStatus.FORBIDDEN, "Forbidden")
			return None
		return session

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
		    f"qbot4k_oauth_state={state}; Path=/; HttpOnly; SameSite=Lax",))

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
		finally:
			connection.close()

		session = build_session(str(operator_id), identity.username, role)
		cookie_value = create_session_cookie(
    self.settings.dashboard_session_secret, session)
		self._redirect(
			handler,
			"/dashboard",
			cookies=(
				f"qbot4k_session={cookie_value}; Path=/; HttpOnly; SameSite=Lax",
				"qbot4k_oauth_state=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
			),
		)

	def _serve_logout(self, handler: BaseHTTPRequestHandler) -> None:
		self._redirect(
			handler,
			"/login",
			cookies=("qbot4k_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",),
		)

	def _serve_dashboard(self, handler: BaseHTTPRequestHandler,
	                     query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		status_message = (query.get("status") or [""])[0].strip()
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			overview = load_overview_snapshot(connection)
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
			if session.role == "admin"
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
		self._redirect(
			handler,
			f"/dashboard?status={quote(f'Restart requested for {service_name}')}",
		)
		self._schedule_systemd_restart(service_name)

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
			users = search_users(connection, query=search, sort_by=sort_by, sort_dir=sort_dir)
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
					WHERE id = ?
					""",
					(target_account_id,),
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

			selected_user = connection.execute(
				"SELECT id FROM users WHERE id = ?",
				(selected_user_id,),
			).fetchone()
			if selected_user is None:
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
						WHERE username = ? COLLATE NOCASE
						ORDER BY updated_at DESC, id DESC
						""",
						(username,),
					).fetchall()
				else:
					accounts = connection.execute(
						"""
						SELECT platform, platform_user_id
						FROM platform_accounts
						WHERE username = ? COLLATE NOCASE AND platform = ?
						ORDER BY updated_at DESC, id DESC
						""",
						(username, platform),
					).fetchall()

				if not accounts:
					accounts = connection.execute(
						"""
						SELECT platform_accounts.platform, platform_accounts.platform_user_id
						FROM users
						INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
						WHERE users.primary_display_name = ? COLLATE NOCASE
						""",
						(username,),
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
				WHERE id = ? AND user_id = ?
				""",
				(platform_account_id, user_id),
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
			users = search_users(connection, limit=500)
			selected_user = next((item for item in users if item.user_id == user_id), None)
			recent_messages = list_recent_user_messages(connection, user_id)
			platform_accounts = list_user_platform_accounts(connection, user_id)
			moderation_status = get_user_moderation_status(connection, user_id)
			recent_moderation_actions = list_recent_user_moderation_actions(connection, user_id)
			derived_signals = list_user_derived_signals(connection, user_id) if user_id >= 0 else []
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
				if session.role == "admin" and user_id >= 0
				else "<td><span class='muted'>No action</span></td>"
			)
			+ "</tr>"
			for item in platform_accounts
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
			)
			total = derived_signal_count(connection)
			profile_count = int(connection.execute("SELECT COUNT(DISTINCT user_id) FROM derived_signals").fetchone()[0])
			high_risk_count = int(connection.execute("SELECT COUNT(*) FROM derived_signals WHERE signal_key = 'risk.composite' AND value_real >= 50").fetchone()[0])
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
			allowed_accounts = {account.platform_account_id for account in list_user_platform_accounts(connection, user_id)}
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
			)
		finally:
			connection.close()

		self._redirect(handler, f"/users/{user_id}?mod_status={quote(f'Moderation action {action_type} recorded')}")

	def _serve_moderation(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			reviews = list_open_reviews(connection)
			actions = list_recent_actions(connection)
		finally:
			connection.close()
		review_rows = "".join(
			f"<tr><td>{item.review_id}</td><td>{self._escape(item.target_username)}</td><td>{self._escape(item.severity)}</td><td>{self._escape(item.reason_code)}</td><td>{self._escape(item.status)}</td></tr>"
			for item in reviews
		)
		action_rows = "".join(
			f"<tr><td>{self._escape(item.platform)}</td><td>{self._escape(item.target_username)}</td><td>{self._escape(item.action_type)}</td><td>{self._escape(item.status)}</td><td>{self._escape(item.reason or '')}</td></tr>"
			for item in actions
		)
		body = self._render_page(
			"Moderation",
			session,
			"<section class='hero'><div><p class='eyebrow'>Moderation</p><h1>Review and action queue</h1><p class='lede'>Open cases and recent actions are surfaced here for operators.</p></div></section>"  # noqa: E501
			+ f"<section class='card'><h2>Open reviews</h2><div class='table-scroll'><table class='table'><thead><tr><th>ID</th><th>Target</th><th>Severity</th><th>Reason</th><th>Status</th></tr></thead><tbody>{review_rows or '<tr><td colspan=5>No open reviews</td></tr>'}</tbody></table></div></section><section class='card'><h2>Recent actions</h2><div class='table-scroll'><table class='table'><thead><tr><th>Platform</th><th>Target</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>{action_rows or '<tr><td colspan=5>No actions yet</td></tr>'}</tbody></table></div></section>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

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
			overview = load_overview_snapshot(connection)
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
			items = self._search_items(connection, query)
			saved = list_saved_queries(connection)
		finally:
			connection.close()
		rows = "".join(
			f"<tr><td>{item['id']}</td><td>{self._escape(item['occurred_at'])}</td><td>{self._escape(item['platform'])}</td>"
			f"<td>{self._escape(item['event_type'])}</td><td>{self._escape(item.get('text_raw') or '')}</td>"
			f"<td><a href='/api/observations/{item['id']}/pivots'>Pivots</a></td></tr>" for item in items
		) or "<tr><td colspan='6'>No matching observations.</td></tr>"
		saved_options = "".join(f"<li><a href='/search?q={quote(str(item['query_text']))}'>{self._escape(item['name'])}</a></li>" for item in saved) or "<li>No saved queries.</li>"
		content = (
			"<section class='hero'><div><p class='eyebrow'>Investigation</p><h1>Observation search</h1>"
			"<p class='lede'>Full-text search with temporal, entity, user, platform, and event filters.</p></div></section>"
			"<section class='card'><form class='search' method='get' action='/search'>"
			f"<input name='q' placeholder='Terms or phrases' value='{self._escape((query.get('q') or [''])[0])}'>"
			"<input name='start_at' placeholder='Start ISO timestamp'><input name='end_at' placeholder='End ISO timestamp'>"
			"<input name='platform' placeholder='Platform'><input name='event_type' placeholder='Event type'>"
			"<input name='user_id' placeholder='User ID'><input name='context_id' placeholder='Context'>"
			"<input name='entity_type' placeholder='Entity type'><input name='entity_value' placeholder='Entity value'>"
			"<button type='submit'>Search</button></form></section>"
			f"<section class='card'><h2>Results</h2><div class='table-scroll'><table class='table'><thead><tr><th>ID</th><th>Time</th><th>Platform</th><th>Event</th><th>Content</th><th>Investigate</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
			f"<section class='card'><h2>Saved queries</h2><form class='toolbar' method='post' action='/search/saved'><input name='name' placeholder='Query name' required><input name='q' value='{self._escape((query.get('q') or [''])[0])}' placeholder='Full-text query'><button type='submit'>Save query</button></form><ul>{saved_options}</ul></section>"
		)
		self._send_html(handler, HTTPStatus.OK, self._render_page("Search", session, content))

	def _serve_search_save(self, handler: BaseHTTPRequestHandler) -> None:
		if self._require_session(handler) is None:
			return
		form = self._read_form_body(handler)
		if form is None: return
		name = (form.get("name") or [""])[0]; query_text = (form.get("q") or [""])[0]
		try:
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection: save_query(connection, name, query_text, {})
		except ValueError as exc:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, str(exc)); return
		finally:
			if 'connection' in locals(): connection.close()
		self._redirect(handler, f"/search?q={quote(query_text)}")

	def _serve_analytics(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		sorts = self._normalize_analytics_sorts(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			snapshot = analytics_snapshot(connection, sorts=sorts)
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
		content = "<section class='hero'><div><p class='eyebrow'>Analytical breadth</p><h1>Intelligence analytics</h1><p class='lede'>Emergence, networks, identity hypotheses, cohort deviations, and model quality.</p></div></section>"
		content += "<section class='card'><h2>Emerging topics</h2>" + table("topics", snapshot["topics"], ["topic_kind", "label", "velocity", "community_count", "unusualness"]) + "</section>"
		content += "<section class='card'><h2>Graph influence</h2>" + table("graph", snapshot["graph"], ["user_id", "pagerank", "betweenness", "is_bridge", "cluster_id", "influence_score"]) + "</section>"
		content += "<section class='card'><h2>Identity suggestions</h2>" + table("identity_suggestions", snapshot["identity_suggestions"], ["id", "left_platform_account_id", "right_platform_account_id", "confidence", "status"]) + "</section>"
		content += "<section class='card'><h2>Cohort anomalies</h2>" + table("cohort_anomalies", snapshot["cohort_anomalies"], ["user_id", "cohort_key", "signal_key", "z_score", "direction", "confidence"]) + "</section>"
		content += "<section class='card'><h2>Model evaluation</h2>" + table("evaluation", snapshot["evaluation"], ["model_key", "model_version", "sample_size", "calculated_at"]) + "</section>"
		self._send_html(handler, HTTPStatus.OK, self._render_page("Analytics", session, content))

	def _serve_api_search(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		if self._require_session(handler) is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			items = self._search_items(connection, query)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": items})

	def _search_items(self, connection, query: Mapping[str, list[str]]) -> list[dict[str, object]]:
		value = lambda key: (query.get(key) or [""])[0].strip() or None
		user_raw = value("user_id")
		return search_observations(
			connection, query=value("q") or "", start_at=value("start_at"), end_at=value("end_at"),
			platform=value("platform"), event_type=value("event_type"), user_id=int(user_raw) if user_raw and user_raw.isdigit() else None,
			container_id=value("container_id"), context_id=value("context_id"), entity_type=value("entity_type"),
			entity_value=value("entity_value"), limit=int(value("limit") or 100), offset=int(value("offset") or 0),
		)

	def _serve_api_save_query(self, handler: BaseHTTPRequestHandler) -> None:
		if self._require_session(handler) is None:
			return
		payload = self._read_json_body(handler)
		if payload is None:
			return
		try:
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection:
				query_id = save_query(connection, str(payload.get("name") or ""), str(payload.get("query") or ""), payload.get("filters") if isinstance(payload.get("filters"), dict) else {})
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"id": query_id, "status": "saved"})

	def _serve_api_observation_pivots(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		if self._require_session(handler) is None:
			return
		try:
			observation_id = int(path.split("/")[3])
			connection = connect_database(self.settings.database_path); initialize_database(connection)
			payload = observation_pivots(connection, observation_id)
		except (ValueError, IndexError):
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "observation_not_found"}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_analytics(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		if self._require_session(handler) is None:
			return
		sorts = self._normalize_analytics_sorts(query)
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection); payload = analytics_snapshot(connection, sorts=sorts)
		finally:
			connection.close()
		payload["sort"] = {key: {"by": value[0], "dir": value[1]} for key, value in sorts.items()}
		self._send_json(handler, HTTPStatus.OK, payload)

	def _serve_api_identity_review(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		if self._require_session(handler, admin_only=True) is None:
			return
		payload = self._read_json_body(handler)
		if payload is None: return
		try:
			suggestion_id = int(path.rstrip('/').split('/')[-1]); connection = connect_database(self.settings.database_path); initialize_database(connection)
			with connection: review_identity_suggestion(connection, suggestion_id, str(payload.get("decision") or ""))
		except ValueError as exc:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)}); return
		finally:
			if 'connection' in locals(): connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "reviewed"})

	def _serve_api_external_observation(self, handler: BaseHTTPRequestHandler) -> None:
		if self._require_session(handler, admin_only=True) is None:
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
		if self._require_session(handler, admin_only=True) is None:
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
			users = search_users(connection, query=search, sort_by=sort_by, sort_dir=sort_dir)
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
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			summary = intelligence_summary(connection)
			alerts = connection.execute(
				"""
				SELECT intelligence_alerts.*, users.primary_display_name
				FROM intelligence_alerts
				LEFT JOIN users ON users.id = intelligence_alerts.user_id
				ORDER BY """ + alert_order + """
				LIMIT 100
				"""
			).fetchall()
			cases = connection.execute(
				"""
				SELECT investigation_cases.*, COUNT(DISTINCT case_entities.user_id) AS entity_count,
				       COUNT(DISTINCT case_evidence.id) AS evidence_count
				FROM investigation_cases
				LEFT JOIN case_entities ON case_entities.case_id = investigation_cases.id
				LEFT JOIN case_evidence ON case_evidence.case_id = investigation_cases.id
				GROUP BY investigation_cases.id
				ORDER BY """ + case_order + """ LIMIT 50
				"""
			).fetchall()
			relationships = connection.execute(
				"""
				SELECT entity_relationships.*, source.primary_display_name AS source_name,
				       target.primary_display_name AS target_name
				FROM entity_relationships
				INNER JOIN users source ON source.id = entity_relationships.source_user_id
				INNER JOIN users target ON target.id = entity_relationships.target_user_id
				ORDER BY """ + relationship_order + """
				LIMIT 100
				"""
			).fetchall()
			reports = connection.execute(
				"SELECT id, report_type, title, summary, generated_at FROM intelligence_reports ORDER BY generated_at DESC LIMIT 50"
			).fetchall()
		finally:
			connection.close()

		def _intelligence_query(**overrides: object) -> str:
			params: dict[str, str] = {
				"alert_sort": alert_sort, "alert_dir": alert_dir,
				"case_sort": case_sort, "case_dir": case_dir,
				"relationship_sort": relationship_sort, "relationship_dir": relationship_dir,
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

		alert_rows = "".join(
			"<tr>"
			+ f"<td>{self._escape(row['severity'])}</td>"
			+ f"<td><a href='/users/{row['user_id']}'>{self._escape(row['primary_display_name'] or 'Unknown')}</a></td>"
			+ f"<td><strong>{self._escape(row['title'])}</strong><br><span class='muted'>{self._escape(row['summary'])}</span></td>"
			+ f"<td>{float(row['confidence']) * 100:.0f}%</td><td>{self._escape(row['status'])}</td>"
			+ (f"<td><div class='row-actions'><form method='post' action='/intelligence/alerts/{row['id']}/case'><button type='submit'>Open case</button></form>"
			   f"<form method='post' action='/intelligence/alerts/{row['id']}/disposition'><select name='disposition'><option value='confirmed'>Confirmed</option><option value='benign'>Benign</option><option value='unresolved'>Unresolved</option><option value='escalated'>Escalated</option></select><button type='submit'>Resolve</button></form></div></td>"
			   if row['status'] == 'open' else f"<td>{self._escape(row['disposition'] or '—')}</td>")
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
			+ f"<div class='grid'><div class='metric'><div class='label'>Active alerts</div><div class='value'>{summary.open_alerts}</div></div><div class='metric'><div class='label'>Open cases</div><div class='value'>{summary.open_cases}</div></div><div class='metric'><div class='label'>Relationships</div><div class='value'>{summary.relationships}</div></div><div class='metric'><div class='label'>Reports</div><div class='value'>{summary.reports}</div></div></div>"
			+ f"<section class='card'><h2>Alerts</h2><div class='table-scroll'><table class='table'><thead><tr><th>{_alert_header('Severity', 'severity')}</th><th>{_alert_header('Subject', 'subject')}</th><th>{_alert_header('Finding', 'finding')}</th><th>{_alert_header('Confidence', 'confidence')}</th><th>{_alert_header('Status', 'status')}</th><th>Disposition</th></tr></thead><tbody>{alert_rows or '<tr><td colspan=6>No alerts</td></tr>'}</tbody></table></div></section>"
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
			case = connection.execute("SELECT * FROM investigation_cases WHERE id=?", (case_id,)).fetchone()
			entities = connection.execute("SELECT case_entities.*, users.primary_display_name FROM case_entities INNER JOIN users ON users.id=case_entities.user_id WHERE case_id=?", (case_id,)).fetchall()
			evidence = connection.execute("SELECT * FROM case_evidence WHERE case_id=? ORDER BY added_at, id", (case_id,)).fetchall()
		finally:
			connection.close()
		if case is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "Case not found")
			return
		entity_rows = "".join(f"<tr><td><a href='/users/{row['user_id']}'>{self._escape(row['primary_display_name'])}</a></td><td>{self._escape(row['role'])}</td><td>{self._escape(row['added_at'])}</td></tr>" for row in entities)
		evidence_rows = "".join(f"<tr><td>{self._escape(row['added_at'])}</td><td>{self._escape(row['note'])}</td><td>{self._escape(row['alert_id'] or '—')}</td><td>{self._escape(row['observation_id'] or '—')}</td><td>{self._escape(row['message_id'] or '—')}</td></tr>" for row in evidence)
		body = self._render_page(
			f"Case {case_id}", session,
			f"<section class='hero'><p class='eyebrow'>Investigation case {case_id}</p><h1>{self._escape(case['title'])}</h1><p class='lede'>{self._escape(case['summary'])}</p><div class='status-row'><span class='status-pill'>{self._escape(case['priority'])}</span><span class='status-pill'>{self._escape(case['status'])}</span></div></section>"
			+ f"<section class='card'><h2>Entities</h2><div class='table-scroll'><table class='table'><thead><tr><th>Entity</th><th>Role</th><th>Added</th></tr></thead><tbody>{entity_rows or '<tr><td colspan=3>No entities</td></tr>'}</tbody></table></div></section>"
			+ f"<section class='card'><h2>Evidence timeline</h2><div class='table-scroll'><table class='table'><thead><tr><th>Added</th><th>Note</th><th>Alert</th><th>Observation</th><th>Message</th></tr></thead><tbody>{evidence_rows or '<tr><td colspan=5>No evidence</td></tr>'}</tbody></table></div></section>",
		)
		self._send_html(handler, HTTPStatus.OK, body)

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
			case_id = create_case_from_alert(connection, alert_id, operator_id=int(session.user_id))
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
				dispose_alert(connection, alert_id, disposition, operator_id=int(session.user_id))
				connection.commit()
			finally:
				connection.close()
		except (ValueError, IndexError):
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid disposition")
			return
		self._redirect(handler, "/intelligence?status=Alert+resolved")

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
			report_id = generate_intelligence_report(connection, user_id=user_id, report_type=report_type)
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
			summary = intelligence_summary(connection)
			alerts = [dict(row) for row in connection.execute(
				"""SELECT intelligence_alerts.*, users.primary_display_name
				   FROM intelligence_alerts
				   LEFT JOIN users ON users.id=intelligence_alerts.user_id
				   ORDER BY """ + alert_order + " LIMIT 500"
			).fetchall()]
			cases = [dict(row) for row in connection.execute(
				"""SELECT investigation_cases.*, COUNT(DISTINCT case_entities.user_id) AS entity_count,
				          COUNT(DISTINCT case_evidence.id) AS evidence_count
				   FROM investigation_cases
				   LEFT JOIN case_entities ON case_entities.case_id=investigation_cases.id
				   LEFT JOIN case_evidence ON case_evidence.case_id=investigation_cases.id
				   GROUP BY investigation_cases.id ORDER BY """ + case_order + " LIMIT 500"
			).fetchall()]
			relationships = [dict(row) for row in connection.execute(
				"""SELECT entity_relationships.*, source.primary_display_name AS source_name,
				          target.primary_display_name AS target_name
				   FROM entity_relationships
				   INNER JOIN users source ON source.id=entity_relationships.source_user_id
				   INNER JOIN users target ON target.id=entity_relationships.target_user_id
				   ORDER BY """ + relationship_order + " LIMIT 500"
			).fetchall()]
			reports = [dict(row) for row in connection.execute("SELECT id, report_type, subject_user_id, title, summary, generated_at, generator_version FROM intelligence_reports ORDER BY generated_at DESC LIMIT 500").fetchall()]
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
			row = connection.execute("SELECT * FROM intelligence_reports WHERE id=?", (report_id,)).fetchone()
		finally:
			connection.close()
		if row is None:
			self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "report_not_found"})
			return
		payload = dict(row)
		payload["content"] = json.loads(str(payload.pop("content_json")))
		payload["evidence"] = json.loads(str(payload.pop("evidence_json")))
		self._send_json(handler, HTTPStatus.OK, payload)

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
			profile = get_canonical_user_profile(connection, user_id)
			signals = list_user_derived_signals(connection, user_id)
		finally:
			connection.close()
		self._send_json(
			handler,
			HTTPStatus.OK,
			{
				"user": asdict(profile),
				"signals": [{**asdict(signal), "label": signal.label} for signal in signals],
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
			add_user_note(connection, user_id=user_id, operator_id=int(session.user_id), body=body)
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
			actions = list_recent_actions(connection)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [action.__dict__ for action in actions]})

	def _serve_api_reviews(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			reviews = list_open_reviews(connection)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [review.__dict__ for review in reviews]})

	def _serve_api_health(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		database_state = database_health(self.settings.database_path)
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
			"services": {service_name: details["status"] for service_name, details in services_detail.items()},
			"services_detail": services_detail,
			"uptime": {
				"app_started_at": self.app_started_at,
				"app_uptime_seconds": self._uptime_seconds(self.app_started_at, now),
			},
		}
		self._send_json(handler, HTTPStatus.OK, payload)

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

	def _render_page(self, title: str, session: DashboardSession, content: str) -> str:
		page_class = " command-page" if title.casefold() == "commands" else ""
		return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{self._escape(title)} · QBot4K</title>
<style>
:root {{ color-scheme: dark; --bg: #0f1117; --panel: #171b26; --panel-2: #1f2533; --text: #f4f7fb; --muted: #9aa7bd; --accent: #78dcca; --border: rgba(255,255,255,.08); }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 15px/1.5 Inter, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(120,220,202,.18), transparent 30%), var(--bg); color: var(--text); }}
a {{ color: var(--accent); text-decoration: none; }}
.shell {{ display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }}
.nav {{ padding: 28px 18px; border-right: 1px solid var(--border); background: rgba(10,13,19,.7); backdrop-filter: blur(10px); }}
.brand {{ font-size: 20px; font-weight: 800; letter-spacing: .04em; margin-bottom: 24px; }}
.nav a {{ display: block; padding: 10px 12px; border-radius: 12px; color: var(--text); margin-bottom: 6px; background: transparent; }}
.nav a:hover {{ background: var(--panel-2); }}
.main {{ padding: 28px; min-width: 0; overflow-x: hidden; }}
.hero, .card {{ background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,0)), var(--panel); border: 1px solid var(--border); border-radius: 20px; padding: 22px; box-shadow: 0 20px 60px rgba(0,0,0,.25); }}
.hero {{ margin-bottom: 18px; }}
.card + .card {{ margin-top: 18px; }}
.eyebrow {{ text-transform: uppercase; letter-spacing: .18em; color: var(--accent); font-size: 11px; margin: 0 0 10px; }}
h1, h2 {{ margin: 0 0 10px; }}
.lede {{ color: var(--muted); max-width: 62ch; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 18px 0; }}
.metric {{ background: var(--panel); border: 1px solid var(--border); border-radius: 18px; padding: 18px; }}
.metric .value {{ font-size: 30px; font-weight: 800; }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0 20px; }}
.status-banner {{ margin: 0; padding: 10px 14px; border-radius: 999px; background: rgba(120,220,202,.12); color: var(--accent); border: 1px solid rgba(120,220,202,.25); }}
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
table {{ width: 100%; max-width: 100%; border-collapse: collapse; margin-top: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; display: block; overflow-x: auto; }}
th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
form.search {{ display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }}
.signal-filter {{ display: flex; align-items: flex-end; gap: 14px; margin: 18px 0; padding: 18px; border: 1px solid var(--border); border-radius: 18px; background: var(--panel); flex-wrap: wrap; }}
.signal-filter label {{ display: grid; gap: 8px; min-width: min(100%, 360px); flex: 1; }}
.signal-filter select[multiple] {{ width: 100%; min-height: 150px; }}
.signal-filter-actions {{
    display: grid;
    grid-template-columns: 1fr;
    align-items: stretch;
	gap: 0.5rem;
}}
input {{ flex: 1; min-width: 0; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); max-width: 100%; }}
textarea {{ width: 100%; min-width: 0; max-width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); resize: vertical; }}
select {{ min-width: 0; max-width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
button {{ padding: 12px 16px; border: 0; border-radius: 12px; background: var(--accent); color: #041014; font-weight: 700; }}
button.danger {{ background: #d95c5c; color: #fff; }}
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
@media (max-width: 900px) {{ .shell {{ grid-template-columns: 1fr; }} .nav {{ border-right: 0; border-bottom: 1px solid var(--border); }} .columns {{ grid-template-columns: 1fr; }} .sticky-link-panel {{ position: static; }} }}
@media (max-width: 700px) {{ body {{ font-size: 14px; }} .main {{ padding: 16px; }} .nav {{ padding: 18px 14px; }} .hero, .card {{ padding: 16px; border-radius: 16px; }} .grid {{ grid-template-columns: 1fr; }} .toolbar, .status-row, form.search, .command-page .new-command-form, .command-page .row-actions {{ align-items: stretch; }} form.search > *, .toolbar > *, .command-page .new-command-form > *, .command-page .row-actions > * {{ width: 100%; }} button {{ width: 100%; }} .metric .value {{ font-size: 24px; }} th, td {{ padding: 10px 12px; white-space: nowrap; }} .command-page .new-command-form .new-command-name, .command-page .new-command-form .new-command-response {{ min-width: 0; }} .command-page table input:not([type='checkbox']), .command-page table textarea {{ width: 100%; }} .template-dialog {{ width: calc(100vw - 16px); }} .template-dialog-inner {{ padding: 16px; }} }}
</style>
</head>
<body>
<div class='shell'>
<aside class='nav'>
<div class='brand'>QBot4K</div>
<div class='muted'>{self._escape(session.username)} · {self._escape(session.role)}</div>
<nav>
<a href='/dashboard'>Overview</a>
<a href='/system-health'>Health</a>
<a href='/signals'>Signals</a>
<a href='/intelligence'>Intelligence</a>
<a href='/search'>Search</a>
<a href='/analytics'>Analytics</a>
<a href='/users'>Users</a>
<a href='/moderation'>Moderation</a>
		<a href='/commands'>Commands</a>
<form method='post' action='/logout'><button type='submit'>Logout</button></form>
</nav>
</aside>
<main class='main{page_class}'>{content}</main>
</div>
<script>
document.addEventListener("DOMContentLoaded", () => {{
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
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_html(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, body: str) -> None:
		response = body.encode("utf-8")
		handler.send_response(status)
		handler.send_header("Content-Type", "text/html; charset=utf-8")
		handler.send_header("Content-Length", str(len(response)))
		handler.end_headers()
		handler.wfile.write(response)

	def _send_text(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, text: str) -> None:
		response = text.encode("utf-8")
		handler.send_response(status)
		handler.send_header("Content-Type", "text/plain; charset=utf-8")
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
		for key, value in (extra_headers or {}).items():
			handler.send_header(key, value)
		for cookie in cookies:
			handler.send_header("Set-Cookie", cookie)
		handler.end_headers()

	def _read_json_body(self, handler: BaseHTTPRequestHandler) -> dict[str, object] | None:
		length = int(handler.headers.get("Content-Length", "0") or 0)
		if length <= 0:
			self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_body"})
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
		length = int(handler.headers.get("Content-Length", "0") or 0)
		if length <= 0:
			self._send_text(handler, HTTPStatus.BAD_REQUEST, "Missing form body")
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
