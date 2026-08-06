from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Mapping
from urllib.parse import parse_qs, quote, urlparse

from ..config import AppSettings
from ..db import (
    connect_database,
    initialize_database,
    upsert_operator_account,
)
from ..intelligence.userprofiles import (
    add_user_note,
	create_canonical_user,
    get_canonical_user_profile,
    link_platform_account,
    unlink_platform_account,
)
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
from .users import list_recent_user_messages, search_users


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
			self._serve_dashboard(handler)
			return True
		if handler.command == "GET" and path == "/users":
			self._serve_users(handler, parse_qs(parsed.query))
			return True
		if handler.command == "POST" and path == "/users/link":
			self._serve_users_link(handler)
			return True
		if handler.command == "GET" and path.startswith("/users/"):
			self._serve_user_messages(handler, path)
			return True
		if handler.command == "GET" and path == "/moderation":
			self._serve_moderation(handler)
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
			self._send_text(handler, HTTPStatus.BAD_GATEWAY, f"Discord OAuth failed: {exc}")
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

	def _serve_dashboard(self, handler: BaseHTTPRequestHandler) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			overview = load_overview_snapshot(connection)
		finally:
			connection.close()
		body = self._render_page(
			"Dashboard",
			session,
			f"<section class='hero'><div><p class='eyebrow'>Overview</p><h1>QBot4K dashboard</h1><p class='lede'>Messages processed: {overview.messages_total}. Open reviews: {overview.open_reviews}. Pending actions: {overview.pending_actions}.</p></div></section>"  # noqa: E501
			+ self._render_metric_grid(overview),
		)
		self._send_html(handler, HTTPStatus.OK, body)

	def _serve_users(self, handler: BaseHTTPRequestHandler, query: Mapping[str, list[str]]) -> None:
		session = self._require_session(handler)
		if session is None:
			return
		search = (query.get("q") or [""])[0]
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
			users = search_users(connection, query=search)
		finally:
			connection.close()
		selected_user = next((item for item in users if item.user_id == selected_user_id), None)

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
				+ "<input name='usernames' placeholder='username1, username2' required>"
				+ "<select name='platform'><option value='any'>Any platform</option><option value='discord'>Discord</option><option value='twitch'>Twitch</option></select>"
				+ "<button type='submit'>Tag Link</button>"
				+ "</form></div></section>"
			)
		rows = "".join(
			f"<tr><td><a href='/users/{item.user_id}'>{self._escape(item.primary_display_name)}</a></td><td>{item.current_reputation_score}</td><td>{'yes' if item.candidate_flag else 'no'}</td><td>{item.account_count}</td><td>{item.message_count}</td><td><a href='/users?link_user_id={item.user_id}&q={quote(search)}'>Link</a></td></tr>"
			for item in users
		)
		body = self._render_page(
			"Users",
			session,
			"<section class='hero'><div><p class='eyebrow'>Users</p><h1>Canonical profiles</h1><p class='lede'>Search linked accounts, score bands, and recent activity.</p></div></section>"  # noqa: E501
			+ sticky_panel
			+ f"<form class='search' method='get'><input name='q' value='{self._escape(search)}' placeholder='Search users'><button type='submit'>Search</button></form>"
			+ f"<table><thead><tr><th>Name</th><th>Score</th><th>PowerUser</th><th>Accounts</th><th>Messages</th><th>Link</th></tr></thead><tbody>{rows or '<tr><td colspan=6>No users found</td></tr>'}</tbody></table>"
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
		platform = (form.get("platform") or ["any"])[0].strip().casefold()
		if platform not in {"any", "discord", "twitch"}:
			platform = "any"
		try:
			selected_user_id = int(selected_user_id_raw)
		except ValueError:
			self._redirect(handler, f"/users?q={quote(search)}&link_status={quote('Invalid selected user')}")
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
			self._redirect(
				handler,
				f"/users?link_user_id={selected_user_id}&q={quote(search)}&link_status={quote('No usernames provided')}",
			)
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
					self._redirect(
						handler,
						f"/users?q={quote(search)}&link_status={quote('Selected user not found')}",
					)
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
				self._redirect(
					handler,
					f"/users?q={quote(search)}&link_status={quote('Selected user not found')}",
				)
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
		self._redirect(
			handler,
			f"/users?link_user_id={selected_user_id}&q={quote(search)}&link_status={quote(status_message)}",
		)

	def _serve_user_messages(self, handler: BaseHTTPRequestHandler, path: str) -> None:
		session = self._require_session(handler)
		if session is None:
			return
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
		finally:
			connection.close()

		if selected_user is None:
			self._send_text(handler, HTTPStatus.NOT_FOUND, "User not found")
			return

		message_rows = "".join(
			f"<tr><td>{self._escape(item.sent_at)}</td><td>{self._escape(item.platform)}</td><td>{self._escape(item.channel_id)}</td><td>{self._escape(item.content_raw)}</td></tr>"
			for item in recent_messages
		)
		body = self._render_page(
			"User Messages",
			session,
			"<section class='hero'><div><p class='eyebrow'>Users</p>"
			+ f"<h1>{self._escape(selected_user.primary_display_name)}</h1>"
			+ "<p class='lede'>Recent messages from this profile.</p>"
			+ "</div></section>"
			+ "<p><a href='/users'>&larr; Back to users</a></p>"
			+ f"<table><thead><tr><th>Sent</th><th>Platform</th><th>Channel</th><th>Message</th></tr></thead><tbody>{message_rows or '<tr><td colspan=4>No messages found</td></tr>'}</tbody></table>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

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
			f"<tr><td>{item.review_id}</td><td>{item.severity}</td><td>{item.reason_code}</td><td>{item.status}</td></tr>"
			for item in reviews
		)
		action_rows = "".join(
			f"<tr><td>{item.platform}</td><td>{item.action_type}</td><td>{item.status}</td><td>{item.reason or ''}</td></tr>"
			for item in actions
		)
		body = self._render_page(
			"Moderation",
			session,
			"<section class='hero'><div><p class='eyebrow'>Moderation</p><h1>Review and action queue</h1><p class='lede'>Open cases and recent actions are surfaced here for operators.</p></div></section>"  # noqa: E501
			+ f"<div class='columns'><section><h2>Open reviews</h2><table><thead><tr><th>ID</th><th>Severity</th><th>Reason</th><th>Status</th></tr></thead><tbody>{review_rows or '<tr><td colspan=4>No open reviews</td></tr>'}</tbody></table></section><section><h2>Recent actions</h2><table><thead><tr><th>Platform</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>{action_rows or '<tr><td colspan=4>No actions yet</td></tr>'}</tbody></table></section></div>"
		)
		self._send_html(handler, HTTPStatus.OK, body)

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
		connection = connect_database(self.settings.database_path)
		try:
			initialize_database(connection)
			users = search_users(connection, query=search)
		finally:
			connection.close()
		self._send_json(handler, HTTPStatus.OK, {"items": [item.__dict__ for item in users]})

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
table {{ width: 100%; border-collapse: collapse; margin-top: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; }}
th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
form.search {{ display: flex; gap: 10px; margin: 18px 0; }}
input {{ flex: 1; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
select {{ padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--panel); color: var(--text); }}
button {{ padding: 12px 16px; border: 0; border-radius: 12px; background: var(--accent); color: #041014; font-weight: 700; }}
.columns {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
.sticky-link-panel {{ position: sticky; top: 12px; z-index: 5; margin-bottom: 18px; }}
@media (max-width: 900px) {{ .shell {{ grid-template-columns: 1fr; }} .nav {{ border-right: 0; border-bottom: 1px solid var(--border); }} .columns {{ grid-template-columns: 1fr; }} }}
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
<form method='post' action='/logout'><button type='submit'>Logout</button></form>
</nav>
</aside>
<main class='main'>{content}</main>
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
