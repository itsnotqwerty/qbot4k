from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from websocket import ABNF, WebSocket, create_connection
from websocket._exceptions import WebSocketTimeoutException

from .db import (
	connect_database,
	initialize_database,
	list_pending_moderation_actions_for_message,
	persist_normalized_message,
	mark_moderation_action_completed,
	record_server_boost_request,
	reward_server_boost_request,
)
from .commands import CommandContext, CommandRegistry, build_default_command_registry, render_command_reply
from .models import ConnectorHealth, IngestionResult, NormalizedMessage, coerce_timestamp


class DiscordPayloadError(ValueError):
	pass


class DiscordConnectionError(RuntimeError):
	pass


class DiscordAuthError(DiscordConnectionError):
	pass


def normalize_discord_message(payload: Mapping[str, object]) -> NormalizedMessage:
	author = payload.get("author")
	if not isinstance(author, Mapping):
		raise DiscordPayloadError("Discord payload requires an author object")

	author_id = str(author.get("id") or "").strip()
	username = str(author.get("username") or author.get("global_name") or "").strip()
	channel_id = str(payload.get("channel_id") or "").strip()
	content = str(payload.get("content") or "")

	if not author_id:
		raise DiscordPayloadError("Discord payload requires author.id")
	if not username:
		raise DiscordPayloadError("Discord payload requires author.username")
	if not channel_id:
		raise DiscordPayloadError("Discord payload requires channel_id")

	role_names = payload.get("role_names")
	if isinstance(role_names, (list, tuple)):
		normalized_roles = tuple(str(role).strip() for role in role_names if str(role).strip())
	else:
		normalized_roles = ()

	mentioned_user_ids: tuple[str, ...] = ()
	mentions = payload.get("mentions")
	if isinstance(mentions, (list, tuple)):
		collected_ids: list[str] = []
		for mention in mentions:
			if isinstance(mention, Mapping):
				target = str(mention.get("id") or "").strip()
			else:
				target = str(mention).strip()
			if not target:
				continue
			collected_ids.append(target)
		mentioned_user_ids = tuple(collected_ids)

	attachment_urls: tuple[str, ...] = ()
	attachments = payload.get("attachments")
	if isinstance(attachments, (list, tuple)):
		collected_urls: list[str] = []
		for attachment in attachments:
			if isinstance(attachment, Mapping):
				url = str(attachment.get("url") or "").strip()
			else:
				url = str(attachment).strip()
			if not url:
				continue
			collected_urls.append(url)
		attachment_urls = tuple(collected_urls)

	guild_id = payload.get("guild_id")
	is_moderator = bool(payload.get("author_is_moderator", False))
	if not is_moderator and normalized_roles:
		moderation_roles = {"mod", "moderator", "admin", "administrator"}
		is_moderator = any(role.casefold() in moderation_roles for role in normalized_roles)

	reply_to_author_is_bot: bool | None = None
	referenced_message = payload.get("referenced_message")
	if isinstance(referenced_message, Mapping):
		referenced_author = referenced_message.get("author")
		if isinstance(referenced_author, Mapping) and "bot" in referenced_author:
			reply_to_author_is_bot = bool(referenced_author.get("bot"))

	metadata = {
		"guild_id": str(guild_id) if guild_id is not None else None,
		"author_is_bot": bool(author.get("bot", False)),
		"mentioned_user_ids": mentioned_user_ids,
		"attachment_urls": attachment_urls,
		"reply_to_author_is_bot": reply_to_author_is_bot,
		"interaction_user_id": str(payload.get("interaction_user_id") or "").strip() or None,
	}

	return NormalizedMessage(
		platform="discord",
		platform_message_id=str(payload.get("id")) if payload.get("id") is not None else None,
		platform_user_id=author_id,
		username=username,
		channel_id=channel_id,
		guild_or_channel_context=str(guild_id) if guild_id is not None else channel_id,
		content_raw=content,
		sent_at=coerce_timestamp(payload.get("timestamp")),
		role_names=normalized_roles,
		is_moderator=is_moderator,
		metadata=metadata,
	)


def build_discord_message_payload(payload: Mapping[str, object]) -> dict[str, object]:
	author = payload.get("author")
	if not isinstance(author, Mapping):
		raise DiscordPayloadError("Discord payload requires an author object")

	member = payload.get("member")
	role_names: tuple[str, ...] = ()
	if isinstance(member, Mapping):
		roles = member.get("roles")
		if isinstance(roles, (list, tuple)):
			role_names = tuple(str(role).strip() for role in roles if str(role).strip())

	interaction_user_id = ""
	interaction = payload.get("interaction")
	if isinstance(interaction, Mapping):
		interaction_user = interaction.get("user")
		if isinstance(interaction_user, Mapping):
			interaction_user_id = str(interaction_user.get("id") or "").strip()

	if not interaction_user_id:
		interaction_metadata = payload.get("interaction_metadata")
		if isinstance(interaction_metadata, Mapping):
			interaction_user = interaction_metadata.get("user")
			if isinstance(interaction_user, Mapping):
				interaction_user_id = str(interaction_user.get("id") or "").strip()

	if not interaction_user_id:
		interaction_user = payload.get("interaction_user")
		if isinstance(interaction_user, Mapping):
			interaction_user_id = str(interaction_user.get("id") or "").strip()

	reply_to_author_is_bot: bool | None = None
	referenced_message = payload.get("referenced_message")
	if isinstance(referenced_message, Mapping):
		referenced_author = referenced_message.get("author")
		if isinstance(referenced_author, Mapping) and "bot" in referenced_author:
			reply_to_author_is_bot = bool(referenced_author.get("bot"))

	return {
		"id": payload.get("id"),
		"timestamp": payload.get("timestamp"),
		"guild_id": payload.get("guild_id"),
		"channel_id": payload.get("channel_id"),
		"content": payload.get("content"),
		"author": {
			"id": author.get("id"),
			"username": author.get("username"),
			"global_name": author.get("global_name"),
			"bot": bool(author.get("bot", False)),
		},
		"mentions": tuple(
			str(mention.get("id") or "").strip()
			for mention in payload.get("mentions", ())
			if isinstance(mention, Mapping) and str(mention.get("id") or "").strip()
		),
		"attachments": tuple(
			str(attachment.get("url") or "").strip()
			for attachment in payload.get("attachments", ())
			if isinstance(attachment, Mapping) and str(attachment.get("url") or "").strip()
		),
		"reply_to_author_is_bot": reply_to_author_is_bot,
		"interaction_user_id": interaction_user_id,
		"role_names": role_names,
		"author_is_moderator": bool(payload.get("author_is_moderator", False)),
	}


class DiscordConnector:
	def __init__(
		self,
		database_path: Path,
		*,
		guild_ids: tuple[str, ...] = (),
		allow_bot_messages: bool = False,
		command_registry: CommandRegistry | None = None,
		bot_token: str | None = None,
	) -> None:
		self.database_path = Path(database_path)
		self.guild_ids = tuple(guild_id.strip() for guild_id in guild_ids if guild_id.strip())
		self.allow_bot_messages = allow_bot_messages
		self._bot_token = bot_token.strip() if bot_token else ""
		self._command_registry = command_registry or build_default_command_registry()
		self._last_status = "idle"
		self._guild_filter_warned_guilds: set[str] = set()
		self._logger = logging.getLogger("qbot4k.discord")
		self._stop_event = threading.Event()
		self._active_websocket: WebSocket | None = None

	def stop(self) -> None:
		self._stop_event.set()
		websocket = self._active_websocket
		if websocket is not None:
			try:
				websocket.close()
			except Exception:
				pass

	def run_forever(self, bot_token: str) -> None:
		self._bot_token = bot_token.strip()
		self._stop_event.clear()
		while not self._stop_event.is_set():
			try:
				self._connect_and_listen(bot_token)
				return
			except DiscordAuthError as exc:
				self._last_status = "auth_failed"
				self._logger.error("discord auth failed: %s", exc)
				return
			except KeyboardInterrupt:
				raise
			except Exception as exc:
				if self._stop_event.is_set():
					return
				self._last_status = "reconnecting"
				self._logger.exception("discord gateway loop failed: %s", exc)
				self._stop_event.wait(5)

	def ingest_message(self, payload: Mapping[str, object]) -> IngestionResult:
		normalized = normalize_discord_message(payload)
		if normalized.metadata.get("author_is_bot"):
			boost_command = self._detect_server_boost_success(normalized.content_raw)
			if boost_command is not None:
				connection = connect_database(self.database_path)
				try:
					initialize_database(connection)
					rewarded_request_id = reward_server_boost_request(
						connection,
						platform="discord",
						channel_id=normalized.channel_id,
						command_names=(boost_command,),
					)
					if rewarded_request_id is None:
						rewarded_request_id = self._reward_server_boost_from_interaction(
							connection,
							normalized,
							boost_command,
						)
					if rewarded_request_id is not None:
						self._last_status = "ready"
						return IngestionResult(
							status="rewarded",
							platform="discord",
							reason="server_boost_success",
						)
				finally:
					connection.close()
			if not self.allow_bot_messages:
				self._last_status = "ready"
				return IngestionResult(
					status="ignored",
					platform="discord",
					reason="bot_authored_message",
				)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			result = persist_normalized_message(connection, normalized)
			if result.status == "persisted":
				self._maybe_record_server_boost_request(connection, normalized, result)
				self._execute_pending_moderation_actions(connection, normalized, result)
				self._dispatch_registered_command(connection, normalized)
			self._last_status = "ready"
			return result
		finally:
			connection.close()

	def _execute_pending_moderation_actions(
		self,
		connection,
		normalized: NormalizedMessage,
		result: IngestionResult,
	) -> None:
		if not self._bot_token.strip():
			return
		if result.message_id is None:
			return

		pending_actions = list_pending_moderation_actions_for_message(connection, result.message_id)
		if not pending_actions:
			return

		guild_id = str(normalized.metadata.get("guild_id") or "").strip()
		platform_message_id = normalized.platform_message_id or str(result.message_id)
		for action in pending_actions:
			action_id = int(action[0])
			action_type = str(action[3]).strip().casefold()
			reason = str(action[4]) if action[4] is not None else action_type
			try:
				if action_type == "timeout":
					if platform_message_id:
						self._delete_discord_message(normalized.channel_id, platform_message_id)
					if normalized.is_moderator:
						self._logger.info(
							"discord timeout skipped for moderator user=%s message=%s reason=%s",
							normalized.username,
							result.message_id,
							reason,
						)
						mark_moderation_action_completed(connection, action_id)
						continue
					if guild_id:
						self._timeout_discord_member(
							guild_id=guild_id,
							user_id=normalized.platform_user_id,
							reason=reason,
						)
						mark_moderation_action_completed(connection, action_id)
					else:
						self._logger.warning(
							"discord timeout skipped for message=%s because guild_id is missing",
							result.message_id,
						)
				elif action_type == "ban":
					if platform_message_id:
						self._delete_discord_message(normalized.channel_id, platform_message_id)
					if guild_id:
						self._ban_discord_member(
							guild_id=guild_id,
							user_id=normalized.platform_user_id,
							reason=reason,
						)
						mark_moderation_action_completed(connection, action_id)
					else:
						self._logger.warning(
							"discord ban skipped for message=%s because guild_id is missing",
							result.message_id,
						)
				elif action_type == "warn":
					self._logger.info(
						"discord moderation warning recorded for user=%s message=%s reason=%s",
						normalized.username,
						result.message_id,
						reason,
					)
				else:
					self._logger.warning(
						"discord moderation action not executed for unsupported type=%s message=%s",
						action_type,
						result.message_id,
					)
			except Exception as exc:
				self._logger.exception(
					"discord moderation action failed action_id=%s type=%s message=%s: %s",
					action_id,
					action_type,
					result.message_id,
					exc,
				)

	def _maybe_record_server_boost_request(
		self,
		connection,
		normalized: NormalizedMessage,
		result: IngestionResult,
	) -> None:
		command_name = self._server_boost_command_name(normalized.content_raw)
		if command_name is None or result.platform_account_id is None:
			return

		record_server_boost_request(
			connection,
			platform="discord",
			channel_id=normalized.channel_id,
			requester_platform_account_id=result.platform_account_id,
			command_name=command_name,
		)

	def _server_boost_command_name(self, content: str) -> str | None:
		normalized = content.casefold().strip()
		if not normalized:
			return None
		first_token = normalized.split(None, 1)[0]
		if first_token in {"/bump", "/boop"}:
			return first_token
		return None

	def _detect_server_boost_success(self, content: str) -> str | None:
		normalized = content.casefold()
		success_signals = {
			"/bump": ("bump done", "bumped successfully", "server bumped"),
			"/boop": ("boop done", "booped successfully", "server booped"),
		}
		for command_name, phrases in success_signals.items():
			if any(phrase in normalized for phrase in phrases):
				return command_name
		return None

	def _reward_server_boost_from_interaction(
		self,
		connection,
		normalized: NormalizedMessage,
		command_name: str,
	) -> int | None:
		interaction_user_id = str(normalized.metadata.get("interaction_user_id") or "").strip()
		if not interaction_user_id:
			return None

		account = connection.execute(
			"""
			SELECT id, user_id
			FROM platform_accounts
			WHERE platform = 'discord' AND platform_user_id = ?
			ORDER BY updated_at DESC, id DESC
			LIMIT 1
			""",
			(interaction_user_id,),
		).fetchone()
		if account is None or account[1] is None:
			return None

		record_server_boost_request(
			connection,
			platform="discord",
			channel_id=normalized.channel_id,
			requester_platform_account_id=int(account[0]),
			command_name=command_name,
		)
		return reward_server_boost_request(
			connection,
			platform="discord",
			channel_id=normalized.channel_id,
			command_names=(command_name,),
		)

	def _dispatch_registered_command(
		self,
		connection,
		normalized: NormalizedMessage,
	) -> None:
		if not self._bot_token:
			return

		context = CommandContext(
			platform="discord",
			database_path=self.database_path,
			connection=connection,
			author_platform_user_id=normalized.platform_user_id,
			author_username=normalized.username,
			channel_id=normalized.channel_id,
			guild_id=str(normalized.metadata.get("guild_id")) if normalized.metadata.get("guild_id") else None,
			message_id=normalized.platform_message_id,
			content=normalized.content_raw,
		)
		try:
			response = self._command_registry.dispatch(normalized.content_raw, context)
		except Exception as exc:
			self._logger.warning("discord command dispatch failed: %s", exc)
			return

		if response is None:
			return

		try:
			self._send_discord_message(normalized.channel_id, render_command_reply(response, "discord"))
		except Exception as exc:
			self._logger.warning("discord command response failed: %s", exc)

	def _send_discord_message(self, channel_id: str, payload: Mapping[str, object]) -> None:
		self._discord_api_request(
			"POST",
			f"/channels/{channel_id}/messages",
			payload,
		)

	def _delete_discord_message(self, channel_id: str, message_id: str) -> None:
		self._discord_api_request(
			"DELETE",
			f"/channels/{channel_id}/messages/{message_id}",
		)

	def _timeout_discord_member(self, *, guild_id: str, user_id: str, reason: str) -> None:
		until = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
		self._discord_api_request(
			"PATCH",
			f"/guilds/{guild_id}/members/{user_id}",
			{
				"communication_disabled_until": until,
			},
			audit_log_reason=reason,
		)

	def _ban_discord_member(self, *, guild_id: str, user_id: str, reason: str) -> None:
		self._discord_api_request(
			"PUT",
			f"/guilds/{guild_id}/bans/{user_id}",
			{
				"delete_message_seconds": 86400,
			},
			audit_log_reason=reason,
		)

	def _discord_api_request(
		self,
		method: str,
		path: str,
		payload: Mapping[str, object] | None = None,
		audit_log_reason: str | None = None,
	) -> object:
		token = self._bot_token.removeprefix("Bot ").strip()
		if not token:
			raise DiscordConnectionError("Discord bot token is not configured")
		headers = {
			"Authorization": f"Bot {token}",
			"Accept": "application/json",
			"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
		}
		data: bytes | None = None
		if payload is not None:
			headers["Content-Type"] = "application/json"
			data = json.dumps(payload).encode("utf-8")
		if audit_log_reason:
			headers["X-Audit-Log-Reason"] = audit_log_reason
		request = Request(
			f"https://discord.com/api/v10{path}",
			data=data,
			headers=headers,
			method=method,
		)
		try:
			with urlopen(request, timeout=15) as response:
				body = response.read().decode("utf-8")
				return json.loads(body) if body else {}
		except HTTPError as exc:
			message = self._read_http_error_body(exc)
			raise DiscordConnectionError(
				f"Discord API request failed for {method} {path}: HTTP {exc.code}{f' - {message}' if message else ''}"
			) from exc
		except URLError as exc:
			raise DiscordConnectionError(f"Discord API request failed for {method} {path}: {exc.reason}") from exc

	def _connect_and_listen(self, bot_token: str) -> None:
		gateway_url = self._fetch_gateway_url(bot_token)
		identify_token = bot_token.removeprefix("Bot ").strip()
		ws = create_connection(gateway_url, timeout=1)
		self._active_websocket = ws
		try:
			hello = self._recv_json_frame(ws)
			if hello.get("op") != 10:
				raise DiscordConnectionError("Discord gateway did not send hello")

			heartbeat_interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
			sequence: int | None = None
			self._send_json(
				ws,
				self._build_identify_payload(identify_token),
			)
			heartbeat_due = time.monotonic() + heartbeat_interval
			self._last_status = "connecting"

			while not self._stop_event.is_set():
				if time.monotonic() >= heartbeat_due:
					self._send_json(ws, {"op": 1, "d": sequence})
					heartbeat_due = time.monotonic() + heartbeat_interval

				try:
					frame = ws.recv_frame()
				except WebSocketTimeoutException:
					continue

				if frame is None:
					raise DiscordConnectionError("Discord websocket closed")

				if frame.opcode == ABNF.OPCODE_CLOSE:
					close_description = self._describe_close_frame(frame.data)
					raise DiscordConnectionError(f"Discord websocket closed{close_description}")

				if frame.opcode != ABNF.OPCODE_TEXT:
					continue

				message = json.loads(frame.data)
				if message.get("s") is not None:
					sequence = int(message["s"])

				opcode = message.get("op")
				if opcode == 11:
					continue
				if opcode in {7, 9}:
					raise DiscordConnectionError(f"Discord gateway requested reconnect: op={opcode}")
				if opcode != 0:
					continue

				event_type = message.get("t")
				data = message.get("d")
				if not isinstance(data, Mapping):
					continue

				if event_type == "READY":
					self._last_status = "ready"
					self._logger.info("connected to discord gateway")
					if self.guild_ids:
						self._logger.info(
							"discord subscribed guild filter active guild_ids=%s",
							",".join(self.guild_ids),
						)
						available_guild_ids = {
							str(item.get("id") or "").strip()
							for item in data.get("guilds", ())
							if isinstance(item, Mapping) and str(item.get("id") or "").strip()
						}
						missing_guild_ids = [guild_id for guild_id in self.guild_ids if guild_id not in available_guild_ids]
						if missing_guild_ids:
							self._logger.warning(
								"configured discord guild IDs not present in READY payload missing=%s available_count=%s",
								",".join(missing_guild_ids),
								len(available_guild_ids),
							)
					else:
						self._logger.info("discord subscribed guild filter inactive processing all guilds")
					self._logger.info(
						"discord gateway intents guilds=%s guild_messages=%s message_content=%s",
						bool(self._gateway_intents() & (1 << 0)),
						bool(self._gateway_intents() & (1 << 9)),
						bool(self._gateway_intents() & (1 << 15)),
					)
					continue
				if event_type != "MESSAGE_CREATE":
					continue

				guild_id = str(data.get("guild_id") or "").strip()
				if self.guild_ids and guild_id not in self.guild_ids:
					if guild_id not in self._guild_filter_warned_guilds:
						self._guild_filter_warned_guilds.add(guild_id)
						self._logger.warning(
							"skipping discord messages for unsubscribed guild guild=%s channel=%s subscribed_guild_ids=%s",
							guild_id,
							str(data.get("channel_id") or "").strip(),
							",".join(self.guild_ids),
						)
					continue

				payload = build_discord_message_payload(data)
				result = self.ingest_message(payload)
				self._logger.info(
					"ingested discord message guild=%s channel=%s user=%s status=%s",
					payload["guild_id"],
					payload["channel_id"],
					payload["author"]["username"],
					result.status,
				)
		finally:
			self._active_websocket = None
			ws.close()

	def _fetch_gateway_url(self, bot_token: str) -> str:
		token = bot_token.removeprefix("Bot ").strip()
		request = Request(
			"https://discord.com/api/v10/gateway/bot",
			headers={
				"Authorization": f"Bot {token}",
				"Accept": "application/json",
				"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
			},
		)
		try:
			with urlopen(request, timeout=15) as response:
				payload = json.loads(response.read().decode("utf-8"))
		except HTTPError as exc:
			message = self._read_http_error_body(exc)
			raise DiscordAuthError(
				f"Failed to fetch Discord gateway URL: HTTP {exc.code}{f' - {message}' if message else ''}"
			) from exc
		except URLError as exc:
			raise DiscordConnectionError(f"Failed to fetch Discord gateway URL: {exc.reason}") from exc

		url = str(payload.get("url") or "").strip()
		if not url:
			raise DiscordConnectionError("Discord gateway response did not include a url")
		app_id = str(payload.get("id") or "").strip()
		if app_id:
			self._logger.info("discord gateway app id=%s", app_id)
		self._logger.info("discord gateway websocket url=%s", url)
		return f"{url}?v=10&encoding=json"

	def _send_json(self, ws: WebSocket, payload: Mapping[str, object]) -> None:
		ws.send(json.dumps(payload))

	def _recv_json_frame(self, ws: WebSocket) -> Mapping[str, object]:
		while True:
			frame = ws.recv_frame()
			if frame is None:
				raise DiscordConnectionError("Discord websocket closed")
			if frame.opcode == ABNF.OPCODE_CLOSE:
				close_description = self._describe_close_frame(frame.data)
				raise DiscordConnectionError(f"Discord websocket closed{close_description}")
			if frame.opcode != ABNF.OPCODE_TEXT:
				continue

			message = json.loads(frame.data)
			if not isinstance(message, Mapping):
				raise DiscordConnectionError("Discord gateway sent a non-object JSON payload")
			return message

	def _describe_close_frame(self, data: object) -> str:
		if not isinstance(data, (bytes, bytearray)):
			return ""

		if len(data) < 2:
			return ""

		close_code = int.from_bytes(data[:2], "big")
		close_reason = data[2:].decode("utf-8", errors="replace").strip()
		parts: list[str] = [f"code={close_code}"]
		if close_reason:
			parts.append(f"reason={close_reason}")
		return f" ({'; '.join(parts)})"

	def _build_identify_payload(self, identify_token: str) -> dict[str, object]:
		return {
			"op": 2,
			"d": {
				"token": identify_token,
				"intents": self._gateway_intents(),
				"properties": {
					"os": "linux",
					"browser": "qbot4k",
					"device": "qbot4k",
				},
			},
		}

	def _gateway_intents(self) -> int:
		guilds = 1 << 0
		guild_messages = 1 << 9
		message_content = 1 << 15
		return guilds | guild_messages | message_content

	def _read_http_error_body(self, exc: HTTPError) -> str:
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

		message = payload.get("message") if isinstance(payload, Mapping) else None
		if isinstance(message, str) and message.strip():
			return message.strip()
		return body

	def health_snapshot(self) -> ConnectorHealth:
		return ConnectorHealth(
			name="discord",
			status=self._last_status,
			details={
				"allow_bot_messages": self.allow_bot_messages,
				"guild_ids": list(self.guild_ids),
			},
		)
