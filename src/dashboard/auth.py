from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
SESSION_LIFETIME = timedelta(hours=12)


@dataclass(frozen=True)
class DashboardSession:
	user_id: str
	username: str
	role: str
	expires_at: str


@dataclass(frozen=True)
class DiscordIdentity:
	user_id: str
	username: str
	guild_ids: tuple[str, ...]
	permissions: Mapping[str, str]


def build_discord_oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
	params = {
		"client_id": client_id,
		"redirect_uri": redirect_uri,
		"response_type": "code",
		"scope": "identify guilds",
		"state": state,
		"prompt": "consent",
	}
	return f"{DISCORD_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def build_oauth_state() -> str:
	return secrets.token_urlsafe(24)


def exchange_discord_code_for_token(
	client_id: str,
	client_secret: str,
	redirect_uri: str,
	code: str,
) -> str:
	request = Request(
		DISCORD_OAUTH_TOKEN_URL,
		data=urlencode(
			{
				"client_id": client_id,
				"client_secret": client_secret,
				"grant_type": "authorization_code",
				"code": code,
				"redirect_uri": redirect_uri,
			}
		).encode("utf-8"),
		headers={
			"Content-Type": "application/x-www-form-urlencoded",
			"Accept": "application/json",
			"User-Agent": "qbot4k/1.0",
		},
		method="POST",
	)
	try:
		with urlopen(request, timeout=15) as response:
			payload = json.loads(response.read().decode("utf-8"))
	except HTTPError as exc:
		error_message = _read_http_error_body(exc)
		raise ValueError(
			f"Discord token exchange failed: HTTP {exc.code}{f' - {error_message}' if error_message else ''}"
		) from exc
	token = str(payload.get("access_token") or "").strip()
	if not token:
		raise ValueError("Discord OAuth token response did not include access_token")
	return token


def fetch_discord_identity(access_token: str) -> DiscordIdentity:
	user = _discord_api_get("/users/@me", access_token)
	guilds = _discord_api_get("/users/@me/guilds", access_token)

	user_id = str(user.get("id") or "").strip()
	username = str(user.get("global_name") or user.get("username") or "").strip()
	if not user_id or not username:
		raise ValueError("Discord identity response was incomplete")

	if not isinstance(guilds, list):
		guilds = []

	return DiscordIdentity(
		user_id=user_id,
		username=username,
		guild_ids=tuple(str(guild.get("id") or "").strip() for guild in guilds if str(guild.get("id") or "").strip()),
		permissions={str(guild.get("id") or "").strip(): str(guild.get("permissions") or "") for guild in guilds if str(guild.get("id") or "").strip()},
	)


def determine_operator_role(identity: DiscordIdentity, operator_guild_ids: tuple[str, ...]) -> str | None:
	allowed = {guild_id.strip() for guild_id in operator_guild_ids if guild_id.strip()}
	if not allowed:
		return None

	for guild_id in identity.guild_ids:
		if guild_id not in allowed:
			continue
		permissions_raw = identity.permissions.get(guild_id, "")
		try:
			permissions = int(permissions_raw)
		except ValueError:
			permissions = 0
		if permissions & 0x8:
			return "admin"
		return "moderator"

	return None


def create_session_cookie(secret: str, session: DashboardSession) -> str:
	payload = json.dumps(session.__dict__, sort_keys=True, separators=(",", ":")).encode("utf-8")
	encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii")
	signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()
	return f"{encoded_payload}.{signature}"


def parse_session_cookie(secret: str, cookie_value: str | None) -> DashboardSession | None:
	if not cookie_value or "." not in cookie_value:
		return None

	encoded_payload, signature = cookie_value.rsplit(".", 1)
	expected_signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()
	if not hmac.compare_digest(signature, expected_signature):
		return None

	try:
		payload = json.loads(base64.urlsafe_b64decode(encoded_payload.encode("ascii")).decode("utf-8"))
	except Exception:
		return None

	expires_at = str(payload.get("expires_at") or "")
	if not expires_at:
		return None
	try:
		expires = datetime.fromisoformat(expires_at)
	except ValueError:
		return None
	if expires < datetime.now(timezone.utc):
		return None

	return DashboardSession(
		user_id=str(payload.get("user_id") or ""),
		username=str(payload.get("username") or ""),
		role=str(payload.get("role") or ""),
		expires_at=expires.isoformat(),
	)


def build_session(user_id: str, username: str, role: str) -> DashboardSession:
	expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
	return DashboardSession(
		user_id=user_id,
		username=username,
		role=role,
		expires_at=expires_at.isoformat(),
	)


def _discord_api_get(path: str, access_token: str) -> object:
	request = Request(
		f"{DISCORD_API_BASE}{path}",
		headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": "qbot4k/1.0"},
	)
	try:
		with urlopen(request, timeout=15) as response:
			return json.loads(response.read().decode("utf-8"))
	except HTTPError as exc:
		error_message = _read_http_error_body(exc)
		raise ValueError(
			f"Discord API request failed for {path}: HTTP {exc.code}{f' - {error_message}' if error_message else ''}"
		) from exc


def _read_http_error_body(exc: HTTPError) -> str:
	try:
		body = exc.read().decode("utf-8", errors="replace").strip()
	except Exception:
		return ""

	if not body:
		return ""

	try:
		payload = json.loads(body)
	except json.JSONDecodeError:
		return body

	if isinstance(payload, Mapping):
		error = payload.get("error")
		error_description = payload.get("error_description")
		message = payload.get("message")
		parts = [str(part).strip() for part in (error, error_description, message) if isinstance(part, str) and part.strip()]
		if parts:
			return " | ".join(parts)
	return body
