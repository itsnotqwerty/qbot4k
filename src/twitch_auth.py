from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from threading import Lock
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TwitchAuthError(RuntimeError):
	pass


class TwitchReauthorizationRequired(TwitchAuthError):
	"""The stored grant is invalid and operator authorization is required."""


class TwitchTemporaryAuthError(TwitchAuthError):
	"""Authorization could not be checked because Twitch is temporarily unavailable."""


@dataclass(frozen=True)
class TwitchTokenValidation:
	access_token: str
	login: str
	client_id: str
	user_id: str = ""


class TwitchTokenManager:
	def __init__(
		self,
		*,
		initial_access_token: str,
		refresh_token: str | None = None,
		client_id: str | None = None,
		client_secret: str | None = None,
		on_token_refresh: Callable[[str, str | None], None] | None = None,
		logger: logging.Logger | None = None,
	) -> None:
		self._lock = Lock()
		self._access_token = self._normalize_access_token(initial_access_token)
		self._refresh_token = (refresh_token or "").strip()
		self._client_id = (client_id or "").strip()
		self._client_secret = (client_secret or "").strip()
		self._on_token_refresh = on_token_refresh
		self._logger = logger or logging.getLogger("qbot4k.twitch.auth")

	def can_refresh(self) -> bool:
		return bool(self._refresh_token and self._client_id and self._client_secret)

	def validate_token(self) -> TwitchTokenValidation:
		with self._lock:
			validation_payload = self._validate_access_token(self._access_token)
			if validation_payload is None and self.can_refresh():
				self._refresh_access_token_locked()
				validation_payload = self._validate_access_token(self._access_token)

		if validation_payload is None:
			raise TwitchReauthorizationRequired("Twitch authorization is invalid")

		login = str(validation_payload.get("login") or "").strip()
		if not login:
			raise TwitchAuthError("Twitch token validation response did not include login")

		validated_client_id = str(validation_payload.get("client_id") or "").strip()
		resolved_client_id = validated_client_id or self._client_id
		if not resolved_client_id:
			raise TwitchAuthError("Twitch token validation response did not include client_id")

		return TwitchTokenValidation(
			access_token=self._access_token,
			login=login,
			client_id=resolved_client_id,
			user_id=str(validation_payload.get("user_id") or "").strip(),
		)

	@property
	def access_token(self) -> str:
		return self._access_token

	def refresh_access_token(self) -> str:
		with self._lock:
			self._refresh_access_token_locked()
			return self._access_token

	def _validate_access_token(self, access_token: str) -> dict[str, object] | None:
		request = Request(
			"https://id.twitch.tv/oauth2/validate",
			headers={"Authorization": f"OAuth {access_token}"},
		)
		try:
			with urlopen(request, timeout=15) as response:
				payload = json.loads(response.read().decode("utf-8"))
		except HTTPError as exc:
			if exc.code in {400, 401}:
				self._logger.info("twitch token validate rejected code=%s", exc.code)
				return None
			self._logger.warning("twitch token validate failed code=%s", exc.code)
			return None
		except URLError as exc:
			raise TwitchTemporaryAuthError(
				f"Twitch token validation is temporarily unavailable: {exc.reason}"
			) from exc

		if not isinstance(payload, dict):
			return None
		return payload

	def _refresh_access_token_locked(self) -> None:
		if not self.can_refresh():
			raise TwitchAuthError("Twitch refresh configuration is incomplete")

		body = urlencode(
			{
				"grant_type": "refresh_token",
				"refresh_token": self._refresh_token,
				"client_id": self._client_id,
				"client_secret": self._client_secret,
			}
		).encode("utf-8")
		request = Request(
			"https://id.twitch.tv/oauth2/token",
			data=body,
			headers={
				"Accept": "application/json",
				"Content-Type": "application/x-www-form-urlencoded",
			},
			method="POST",
		)
		try:
			with urlopen(request, timeout=15) as response:
				payload = json.loads(response.read().decode("utf-8"))
		except HTTPError as exc:
			if exc.code in {400, 401}:
				raise TwitchReauthorizationRequired(
					f"Twitch refresh authorization was rejected: HTTP {exc.code}"
				) from exc
			raise TwitchTemporaryAuthError(f"Failed to refresh Twitch token: HTTP {exc.code}") from exc
		except URLError as exc:
			raise TwitchTemporaryAuthError(f"Failed to refresh Twitch token: {exc.reason}") from exc

		if not isinstance(payload, dict):
			raise TwitchAuthError("Failed to refresh Twitch token: invalid response payload")

		new_access_token = self._normalize_access_token(str(payload.get("access_token") or ""))
		if not new_access_token:
			raise TwitchAuthError("Failed to refresh Twitch token: missing access_token")
		self._access_token = new_access_token

		new_refresh_token = str(payload.get("refresh_token") or "").strip()
		if new_refresh_token:
			self._refresh_token = new_refresh_token

		if self._on_token_refresh is not None:
			try:
				self._on_token_refresh(self._access_token, self._refresh_token or None)
			except Exception as exc:
				self._logger.warning("failed to persist refreshed twitch tokens: %s", exc)

		self._logger.info("refreshed twitch access token")

	@staticmethod
	def _normalize_access_token(token: str) -> str:
		return token.removeprefix("oauth:").strip()
