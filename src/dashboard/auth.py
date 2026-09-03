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

from ..intelligence.community import TWITCH_INSTALL_OAUTH_SCOPES


DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_OAUTH_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
TWITCH_OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
SESSION_LIFETIME = timedelta(hours=12)
DISCORD_INSTALL_STATE_LIFETIME = timedelta(minutes=15)
TWITCH_INSTALL_STATE_LIFETIME = timedelta(minutes=20)
TWITCH_INSTALL_SCOPES = TWITCH_INSTALL_OAUTH_SCOPES
DISCORD_INSTALL_PERMISSIONS = (
	(1 << 1)
	| (1 << 2)
	| (1 << 10)
	| (1 << 11)
	| (1 << 13)
	| (1 << 14)
	| (1 << 16)
	| (1 << 28)
	| (1 << 40)
)


@dataclass(frozen=True)
class DashboardSession:
	user_id: str
	username: str
	role: str
	expires_at: str
	community_id: int | None = None
	session_version: int = 1


@dataclass(frozen=True)
class DiscordIdentity:
	user_id: str
	username: str
	guild_ids: tuple[str, ...]
	permissions: Mapping[str, str]


@dataclass(frozen=True)
class DiscordInstallState:
	operator_id: str
	community_id: int
	guild_id: str
	nonce: str
	expires_at: str


@dataclass(frozen=True)
class TwitchInstallState:
	operator_id: str
	community_id: int
	broadcaster_login: str
	scopes: tuple[str, ...]
	nonce: str
	expires_at: str


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


def build_discord_install_url(
	client_id: str,
	redirect_uri: str,
	state: str,
	guild_id: str,
	*,
	permissions: int = DISCORD_INSTALL_PERMISSIONS,
) -> str:
	params = {
		"client_id": client_id.strip(),
		"redirect_uri": redirect_uri.strip(),
		"response_type": "code",
		"scope": "bot applications.commands",
		"permissions": str(int(permissions)),
		"guild_id": guild_id.strip(),
		"disable_guild_select": "true",
		"state": state,
	}
	return f"{DISCORD_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def build_twitch_oauth_url(
	client_id: str,
	redirect_uri: str,
	state: str,
	scopes: tuple[str, ...],
) -> str:
	reviewed_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
	unsupported = set(reviewed_scopes) - TWITCH_INSTALL_SCOPES
	if not reviewed_scopes or unsupported:
		raise ValueError("Twitch installation requested unsupported scopes")
	params = {
		"client_id": client_id.strip(),
		"redirect_uri": redirect_uri.strip(),
		"response_type": "code",
		"scope": " ".join(reviewed_scopes),
		"state": state,
		"force_verify": "true",
	}
	return f"{TWITCH_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def build_oauth_state() -> str:
	return secrets.token_urlsafe(24)


def create_discord_install_state(
	secret: str,
	*,
	operator_id: str,
	community_id: int,
	guild_id: str,
	now: datetime | None = None,
) -> str:
	current_time = now or datetime.now(timezone.utc)
	payload = {
		"community_id": int(community_id),
		"expires_at": (current_time + DISCORD_INSTALL_STATE_LIFETIME).isoformat(),
		"guild_id": guild_id.strip(),
		"nonce": secrets.token_urlsafe(24),
		"operator_id": operator_id.strip(),
	}
	encoded_payload = base64.urlsafe_b64encode(
		json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	).decode("ascii")
	signature = hmac.new(
		secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
	).hexdigest()
	return f"{encoded_payload}.{signature}"


def parse_discord_install_state(
	secret: str, state: str | None, *, now: datetime | None = None
) -> DiscordInstallState | None:
	if not state or "." not in state:
		return None
	encoded_payload, signature = state.rsplit(".", 1)
	expected_signature = hmac.new(
		secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
	).hexdigest()
	if not hmac.compare_digest(signature, expected_signature):
		return None
	try:
		payload = json.loads(
			base64.urlsafe_b64decode(encoded_payload.encode("ascii")).decode("utf-8")
		)
		expires_at = datetime.fromisoformat(str(payload["expires_at"]))
		community_id = int(payload["community_id"])
	except (KeyError, TypeError, ValueError, json.JSONDecodeError):
		return None
	if expires_at < (now or datetime.now(timezone.utc)):
		return None
	operator_id = str(payload.get("operator_id") or "").strip()
	guild_id = str(payload.get("guild_id") or "").strip()
	nonce = str(payload.get("nonce") or "").strip()
	if not operator_id or not guild_id or not nonce:
		return None
	return DiscordInstallState(
		operator_id=operator_id,
		community_id=community_id,
		guild_id=guild_id,
		nonce=nonce,
		expires_at=expires_at.isoformat(),
	)


def create_twitch_install_state(
	secret: str,
	*,
	operator_id: str,
	community_id: int,
	broadcaster_login: str,
	scopes: tuple[str, ...],
	now: datetime | None = None,
) -> str:
	reviewed_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
	if not reviewed_scopes or set(reviewed_scopes) - TWITCH_INSTALL_SCOPES:
		raise ValueError("Twitch installation requested unsupported scopes")
	current_time = now or datetime.now(timezone.utc)
	payload = {
		"broadcaster_login": broadcaster_login.strip().lower(),
		"community_id": int(community_id),
		"expires_at": (current_time + TWITCH_INSTALL_STATE_LIFETIME).isoformat(),
		"nonce": secrets.token_urlsafe(24),
		"operator_id": operator_id.strip(),
		"scopes": reviewed_scopes,
	}
	encoded_payload = base64.urlsafe_b64encode(
		json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	).decode("ascii")
	signature = hmac.new(
		secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
	).hexdigest()
	return f"{encoded_payload}.{signature}"


def parse_twitch_install_state(
	secret: str, state: str | None, *, now: datetime | None = None
) -> TwitchInstallState | None:
	if not state or "." not in state:
		return None
	encoded_payload, signature = state.rsplit(".", 1)
	expected_signature = hmac.new(
		secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
	).hexdigest()
	if not hmac.compare_digest(signature, expected_signature):
		return None
	try:
		payload = json.loads(
			base64.urlsafe_b64decode(encoded_payload.encode("ascii")).decode("utf-8")
		)
		expires_at = datetime.fromisoformat(str(payload["expires_at"]))
		community_id = int(payload["community_id"])
		scopes = tuple(sorted({str(scope).strip() for scope in payload["scopes"] if str(scope).strip()}))
	except (KeyError, TypeError, ValueError, json.JSONDecodeError):
		return None
	if expires_at < (now or datetime.now(timezone.utc)) or not scopes:
		return None
	if set(scopes) - TWITCH_INSTALL_SCOPES:
		return None
	operator_id = str(payload.get("operator_id") or "").strip()
	broadcaster_login = str(payload.get("broadcaster_login") or "").strip().lower()
	nonce = str(payload.get("nonce") or "").strip()
	if not operator_id or not broadcaster_login or not nonce:
		return None
	return TwitchInstallState(
		operator_id=operator_id,
		community_id=community_id,
		broadcaster_login=broadcaster_login,
		scopes=scopes,
		nonce=nonce,
		expires_at=expires_at.isoformat(),
	)


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
		community_id=int(payload["community_id"]) if payload.get("community_id") is not None else None,
		session_version=int(payload.get("session_version") or 1),
	)


def build_session(
	user_id: str, username: str, role: str, *, community_id: int | None = None,
	session_version: int = 1,
) -> DashboardSession:
	expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
	return DashboardSession(
		user_id=user_id,
		username=username,
		role=role,
		expires_at=expires_at.isoformat(),
		community_id=community_id,
		session_version=session_version,
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
