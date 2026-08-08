from __future__ import annotations

import logging
import hashlib
import hmac
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Mapping
from urllib.parse import parse_qs, quote, urlencode, urlparse

from ..config import AppSettings
from ..db import (
    connect_database,
	delete_simple_command_definition,
    initialize_database,
	record_moderation_action,
    upsert_operator_account,
	list_command_definitions,
	list_simple_command_definitions,
	upsert_command_definition,
	upsert_simple_command_definition,
)
from ..intelligence.userprofiles import (
    add_user_note,
	create_canonical_user,
    get_canonical_user_profile,
    link_platform_account,
    unlink_platform_account,
)
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


@dataclass(frozen=True)
class DashboardResponse:
	status: HTTPStatus
	body: bytes
	content_type: str
	headers: Mapping[str, str]


class DashboardApp:
	def __init__(self, settings: AppSettings, service_states: Mapping[str, str] | None = None) -> None:
		self.settings = settings
		self.service_states = dict(service_states or {})

	def dispatch(self, handler: BaseHTTPRequestHandler) -> bool:
		parsed = urlparse(handler.path)
		path = parsed.path

		if handler.command == "GET" and path in {"/", "/dashboard"}:
			self._serve_dashboard(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/dashboard/go-live":
			self._serve_dashboard_go_live(handler)
			return True
		if handler.command == "GET" and path == "/users":
			self._serve_users(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/users/link":
			self._serve_users_link(handler)
			return True
		if handler.command == "POST" and path.startswith("/users/") and path.endswith("/moderation"):
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
		if handler.command == "GET" and path in {"/auth/discord/callback", "/oauth/discord/callback"}:
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
		if handler.command == "GET" and path.startswith("/api/users/"):
			self._serve_api_user_detail(handler, path)
			return True
		if handler.command == "POST" and path == "/api/users/link":
			self._serve_api_link_user(handler)
			return True
		if handler.command == "POST" and path.startswith("/api/users/") and path.endswith("/notes"):
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

	def _read_session(self, handler: BaseHTTPRequestHandler) -> DashboardSession | None:
		return parse_session_cookie(
			self.settings.dashboard_session_secret or "",
			self._read_cookie_value(handler, "qbot4k_session"),
		)

	def _read_cookie_value(self, handler: BaseHTTPRequestHandler, cookie_name: str) -> str | None:
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
		scheme = (handler.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip() or "http"
		host = (handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "").split(",", 1)[0].strip()
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

	def _require_session(self, handler: BaseHTTPRequestHandler, admin_only: bool = False) -> DashboardSession | None:
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
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Discord OAuth is not configured")
			return
		state = self._build_oauth_state_token()
		redirect_uri = self._oauth_redirect_uri(handler)
		redirect_target = build_discord_oauth_url(
			self.settings.discord_oauth_client_id,
			redirect_uri,
			state,
		)
		self._redirect(handler, redirect_target, cookies=(f"qbot4k_oauth_state={state}; Path=/; HttpOnly; SameSite=Lax",))

	def _serve_oauth_callback(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		if not self.settings.dashboard_session_secret:
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Session secret is not configured")
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
			self._send_text(handler, HTTPStatus.SERVICE_UNAVAILABLE, "Discord OAuth is not configured")
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
				self._send_text(handler, HTTPStatus.FORBIDDEN, "You are not authorized to access the dashboard")
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
		cookie_value = create_session_cookie(self.settings.dashboard_session_secret, session)
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

	def _serve_dashboard(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
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
		status_html = f"<p class='status-banner'>{self._escape(status_message)}</p>" if status_message else ""
		go_live_action = (
			"<form method='post' action='/dashboard/go-live'>"
			+ "<button type='submit'>Go Live</button>"
			+ "</form>"
			if session.role == "admin"
			else ""
		)
		toolbar_html = f"<div class='toolbar'>{go_live_action}{status_html}</div>"
		body = self._render_page(
			"Dashboard",
			session,
			f"<section class='hero'><div><p class='eyebrow'>Overview</p><h1>QBot4K dashboard</h1><p class='lede'>Messages processed: {overview.messages_total}. Open reviews: {overview.open_reviews}. Pending actions: {overview.pending_actions}.</p></div></section>"  # noqa: E501
			+ toolbar_html
			+ self._render_metric_grid(overview),
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
			+ f"<table><thead>{headers}</thead><tbody>{rows or '<tr><td colspan=6>No users found</td></tr>'}</tbody></table>"
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
		account_options = "".join(
			f"<option value='{item.platform_account_id}'>{self._escape(item.platform)} · {self._escape(item.username)} ({self._escape(item.platform_user_id)})</option>"
			for item in platform_accounts
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
			+ f"<div class='columns'><section><h2>Open reviews</h2><table><thead><tr><th>ID</th><th>Target</th><th>Severity</th><th>Reason</th><th>Status</th></tr></thead><tbody>{review_rows or '<tr><td colspan=5>No open reviews</td></tr>'}</tbody></table></section><section><h2>Recent actions</h2><table><thead><tr><th>Platform</th><th>Target</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>{action_rows or '<tr><td colspan=5>No actions yet</td></tr>'}</tbody></table></section></div>"
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
				+ f"<td><input form='builtin-{command_name}' name='title' value='{self._escape(row[1])}' required></td>"
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
			+ "<section class='card'><h2>Built-Ins</h2><p class='muted'>Builtins are the commands that ship with the bot.</p><table><thead><tr><th>Command</th><th>Title</th><th>Description template</th><th>Footer template</th><th>Status</th><th>Action</th></tr></thead><tbody>"
			+ ("".join(builtin_rows) or "<tr><td colspan='6'>No builtin commands found</td></tr>")
			+ "</tbody></table></section>"
			+ "<section class='card'><h2>Plaintext Commands</h2><p class='muted'>Insert quick text replies or update existing simple commands.</p><form id='simple-new' method='post' action='/commands' class='new-command-form'><input type='hidden' name='record_type' value='simple'><input class='new-command-name' name='command_name' placeholder='Command name (without !) e.g. website' required><input class='new-command-response' name='response_template' placeholder='Plain text response with {display_name}' required><label class='checkbox new-command-enabled'><input type='checkbox' name='enabled' value='1' checked> Enabled</label><button type='submit'>New Command</button></form><table><thead><tr><th>Command</th><th>Response template</th><th>Status</th><th>Action</th></tr></thead><tbody>"
			+ "".join(simple_rows_html)
			+ "</tbody></table></section>"
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

	def _normalize_user_sort(self, sort_by_raw: str, sort_dir_raw: str) -> tuple[str, str]:
		sort_by = (sort_by_raw or "score").strip().casefold()
		if sort_by not in {"score", "messages", "poweruser", "accounts", "name"}:
			sort_by = "score"
		sort_dir = (sort_dir_raw or "").strip().casefold()
		default_dir = "asc" if sort_by == "name" else "desc"
		if sort_dir not in {"asc", "desc"}:
			sort_dir = default_dir
		return sort_by, sort_dir

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
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"user": profile.__dict__})

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
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			database_state = connection.execute(
				"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
			).fetchone()[0]
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"status": "ready", "table_count": int(database_state), "services": dict(self.service_states)})

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
.main {{ padding: 28px; }}
.hero, .card {{ background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,0)), var(--panel); border: 1px solid var(--border); border-radius: 20px; padding: 22px; box-shadow: 0 20px 60px rgba(0,0,0,.25); }}
.hero {{ margin-bottom: 18px; }}
.eyebrow {{ text-transform: uppercase; letter-spacing: .18em; color: var(--accent); font-size: 11px; margin: 0 0 10px; }}
h1, h2 {{ margin: 0 0 10px; }}
.lede {{ color: var(--muted); max-width: 62ch; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 18px 0; }}
.metric {{ background: var(--panel); border: 1px solid var(--border); border-radius: 18px; padding: 18px; }}
.metric .value {{ font-size: 30px; font-weight: 800; }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0 20px; }}
.status-banner {{ margin: 0; padding: 10px 14px; border-radius: 999px; background: rgba(120,220,202,.12); color: var(--accent); border: 1px solid rgba(120,220,202,.25); }}
table {{ width: 100%; border-collapse: collapse; margin-top: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; }}
th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
form.search {{ display: flex; gap: 10px; margin: 18px 0; }}
input {{ flex: 1; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
textarea {{ width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); resize: vertical; }}
select {{ padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
button {{ padding: 12px 16px; border: 0; border-radius: 12px; background: var(--accent); color: #041014; font-weight: 700; }}
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
.command-page table input:not([type='checkbox']), .command-page table textarea {{ min-width: 100%; }}
.command-page .new-command-form {{ display: flex; align-items: center; gap: 12px; margin: 14px 0 10px; }}
.command-page .new-command-form .new-command-name {{ flex: 1 1 240px; min-width: 210px; }}
.command-page .new-command-form .new-command-response {{ flex: 2 1 380px; min-width: 260px; }}
.command-page .new-command-form .new-command-enabled {{ margin-left: 8px; margin-right: 2px; white-space: nowrap; }}
.command-page .new-command-form button {{ white-space: nowrap; }}
.command-page table td .checkbox {{ display: inline-flex; justify-content: flex-start; margin: 0; }}
.command-page table td .checkbox input {{ margin: 0; }}
.command-page .row-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.command-page .row-actions form {{ margin: 0; }}
.command-page .insert-row td {{ background: rgba(120,220,202,.04); }}
.command-page .insert-row code {{ color: var(--accent); }}
@media (max-width: 900px) {{ .shell {{ grid-template-columns: 1fr; }} .nav {{ border-right: 0; border-bottom: 1px solid var(--border); }} .columns {{ grid-template-columns: 1fr; }} .command-page .new-command-form {{ flex-wrap: wrap; }} .command-page .new-command-form button {{ width: 100%; }} }}
</style>
</head>
<body>
<div class='shell'>
<aside class='nav'>
<div class='brand'>QBot4K</div>
<div class='muted'>{self._escape(session.username)} · {self._escape(session.role)}</div>
<nav>
<a href='/dashboard'>Overview</a>
<a href='/users'>Users</a>
<a href='/moderation'>Moderation</a>
		<a href='/commands'>Commands</a>
<form method='post' action='/logout'><button type='submit'>Logout</button></form>
</nav>
</aside>
<main class='main{page_class}'>{content}</main>
</div>
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
		return f"<div class='grid'><div class='metric'><div class='label'>Messages</div><div class='value'>{overview.messages_total}</div></div><div class='metric'><div class='label'>Open reviews</div><div class='value'>{overview.open_reviews}</div></div><div class='metric'><div class='label'>Pending actions</div><div class='value'>{overview.pending_actions}</div></div></div><div class='grid'>{platform_cards}{channel_cards}</div>"

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
