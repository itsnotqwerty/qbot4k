from __future__ import annotations

import logging
import socket
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .commands import CommandContext, CommandRegistry, build_default_command_registry, render_command_reply
from .db import (
	connect_database,
	initialize_database,
	list_pending_moderation_actions_for_message,
	list_twitch_channels,
	mark_moderation_action_completed,
	update_twitch_channel_status,
	upsert_twitch_channel,
	collect_observation
)
from .intelligence.events import ingest_event, observation_from_twitch_irc_event
from .moderation import contains_streamboo_viewer_spam
from .models import ConnectorHealth, IngestionResult, NormalizedMessage, CollectionResult, coerce_timestamp, observation_from_message
from .twitch_auth import TwitchAuthError, TwitchTokenManager


class TwitchPayloadError(ValueError):
	pass


class TwitchConnectionError(RuntimeError):
	pass

class TwitchAuthenticationRequired(Exception):
    """The user must authorize the Twitch integration again."""

class TwitchTemporaryAuthError(Exception):
    """Twitch authentication is temporarily unavailable."""


def normalize_twitch_message(payload: Mapping[str, object]) -> NormalizedMessage:
	user_id = str(payload.get("user_id") or "").strip()
	username = str(payload.get("username") or payload.get("display_name") or "").strip()
	channel = str(payload.get("channel") or payload.get("channel_id") or "").strip()
	content = str(payload.get("content") or payload.get("message") or "")

	if not user_id:
		raise TwitchPayloadError("Twitch payload requires user_id")
	if not username:
		raise TwitchPayloadError("Twitch payload requires username")
	if not channel:
		raise TwitchPayloadError("Twitch payload requires channel")
	if not content.strip():
		raise TwitchPayloadError("Twitch payload requires content")

	badges = payload.get("badges")
	if isinstance(badges, (list, tuple)):
		normalized_badges = tuple(str(badge).strip() for badge in badges if str(badge).strip())
	else:
		normalized_badges = ()

	is_moderator = bool(payload.get("is_moderator", False))
	if not is_moderator and normalized_badges:
		moderator_badges = {"moderator", "broadcaster", "vip"}
		is_moderator = any(badge.casefold() in moderator_badges for badge in normalized_badges)

	return NormalizedMessage(
		platform="twitch",
		platform_message_id=str(payload.get("message_id")) if payload.get("message_id") is not None else None,
		platform_user_id=user_id,
		username=username,
		channel_id=channel,
		guild_or_channel_context=channel,
		content_raw=content,
		sent_at=coerce_timestamp(payload.get("sent_at") or payload.get("timestamp")),
		role_names=normalized_badges,
		is_moderator=is_moderator,
		metadata={"badges": normalized_badges},
	)


def parse_twitch_irc_message(raw_line: str) -> Mapping[str, object] | None:
	line = raw_line.strip()
	if not line or " PRIVMSG " not in line:
		return None

	tags: dict[str, str] = {}
	remainder = line
	if remainder.startswith("@"):
		tag_blob, remainder = remainder.split(" ", 1)
		for item in tag_blob[1:].split(";"):
			if "=" not in item:
				continue
			key, value = item.split("=", 1)
			tags[key] = value

	if not remainder.startswith(":"):
		return None

	try:
		prefix, command_section = remainder[1:].split(" PRIVMSG ", 1)
		channel_part, content = command_section.split(" :", 1)
	except ValueError:
		return None

	username = prefix.split("!", 1)[0].strip()
	channel = channel_part.strip().lstrip("#")
	if not username or not channel:
		return None

	badges = []
	for badge_entry in tags.get("badges", "").split(","):
		if not badge_entry:
			continue
		badge_name = badge_entry.split("/", 1)[0].strip()
		if badge_name:
			badges.append(badge_name)

	timestamp = None
	tmi_sent_ts = tags.get("tmi-sent-ts")
	if tmi_sent_ts and tmi_sent_ts.isdigit():
		timestamp = datetime.fromtimestamp(
			int(tmi_sent_ts) / 1000,
			tz=timezone.utc,
		).isoformat()

	return {
		"message_id": tags.get("id"),
		"timestamp": timestamp,
		"channel": channel,
		"content": content,
		"user_id": tags.get("user-id") or "",
		"username": tags.get("display-name") or username,
		"display_name": tags.get("display-name") or username,
		"badges": tuple(badges),
		"is_moderator": tags.get("mod") == "1",
	}


class TwitchConnector:
	def __init__(
		self,
		database_path: Path,
		*,
		join_command_channel: str = "its_not_qwerty",
		bootstrap_channels: tuple[str, ...] = (),
		command_registry: CommandRegistry | None = None,
		token_manager: TwitchTokenManager | None = None,
	) -> None:
		self.database_path = Path(database_path)
		self.join_command_channel = join_command_channel.strip().casefold()
		self.bootstrap_channels = tuple(
			channel.strip().casefold() for channel in bootstrap_channels if channel.strip()
		)
		self._command_registry = command_registry or build_default_command_registry()
		self._token_manager = token_manager
		self._active_irc_token: str | None = None
		self._last_status = "idle"
		self._logger = logging.getLogger("qbot4k.twitch")
		self._stop_event = threading.Event()
		self._active_socket: ssl.SSLSocket | None = None
		self._send_lock = threading.Lock()

	def stop(self) -> None:
		self._stop_event.set()
		irc_socket = self._active_socket
		if irc_socket is not None:
			try:
				irc_socket.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
			try:
				irc_socket.close()
			except OSError:
				pass

	def ingest_message(self, payload: Mapping[str, object]) -> CollectionResult:
		normalized = normalize_twitch_message(payload)
		observation = observation_from_message(normalized)

		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			result = collect_observation(connection, observation)
			self._last_status = "ready"
			return result
		finally:
			connection.close()

	def ingest_event(self, raw_line: str) -> CollectionResult | None:
		observation = observation_from_twitch_irc_event(raw_line)
		if observation is None:
			return None
		return ingest_event(self.database_path, observation)

	def configured_channels(self) -> tuple[str, ...]:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			self._seed_bootstrap_channels(connection)
			rows = list_twitch_channels(connection)
			self._last_status = "ready"
			return tuple(str(row["channel_name"]) for row in rows)
		finally:
			connection.close()

	def run_forever(self, bot_token: str) -> None:
		self._stop_event.clear()
		bot_login = self._validate_token_and_get_login(bot_token)
		channels = self.configured_channels()
		if not channels:
			raise TwitchConnectionError("No Twitch channels configured")

		self._last_status = "connecting"
		token_for_irc = self._active_irc_token or bot_token.removeprefix("oauth:")
		ssl_context = ssl.create_default_context()

		with socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=30) as raw_socket:
			with ssl_context.wrap_socket(raw_socket, server_hostname="irc.chat.twitch.tv") as irc_socket:
				self._active_socket = irc_socket
				try:
					# Avoid timed file reads; Python file wrappers over timed sockets can raise
					# "cannot read from timed out object" and break the connector loop.
					irc_socket.settimeout(None)
					irc_reader = irc_socket.makefile("r", encoding="utf-8", newline="\r\n")
					self._send_irc_line(irc_socket, f"PASS oauth:{token_for_irc}")
					self._send_irc_line(irc_socket, f"NICK {bot_login}")
					self._send_irc_line(irc_socket, "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
					joined_channels = set()
					for channel_name in channels:
						self._join_channel(irc_socket, channel_name)
						joined_channels.add(channel_name.casefold())

					self._last_status = "ready"
					self._logger.info("connected to twitch irc for channels: %s", ", ".join(sorted(joined_channels)))

					while not self._stop_event.is_set():
						try:
							raw_line = irc_reader.readline()
						except OSError:
							if self._stop_event.is_set():
								return
							raise
						if raw_line == "":
							if self._stop_event.is_set():
								return
							raise TwitchConnectionError("Twitch IRC connection closed")

						line = raw_line.rstrip("\r\n")
						if not line:
							continue
						if line.startswith("PING "):
							self._send_irc_line(irc_socket, line.replace("PING", "PONG", 1))
							continue

						payload = parse_twitch_irc_message(line)
						if payload is None:
							self.ingest_event(line)
							continue

						result = self.ingest_message(
							payload
						)
						self._logger.info(
							"ingested twitch message channel=%s user=%s status=%s",
							payload["channel"],
							payload["username"],
							result.status,
						)

						requested_channel = self._requested_join_channel_from_payload(payload)
						if requested_channel and requested_channel not in joined_channels:
							self._join_channel(irc_socket, requested_channel)
							joined_channels.add(requested_channel)
							self._mark_channel_active(requested_channel)
							self._logger.info(
								"joined requested twitch channel=%s from command channel=%s",
								requested_channel,
								payload["channel"],
							)
				finally:
					self._active_socket = None

	def run_twitch_safely(self, initial_token: str, service_states: dict[str, str]) -> None:
		twitch_logger = logging.getLogger("qbot4k.twitch")
		retry_delay = 1.0
		while not self._stop_event.is_set():
			try:
				self.run_forever(initial_token)
			except TwitchAuthenticationRequired:
				service_states["twitch"] = "auth_failed"
				twitch_logger.error(
					"Twitch authorization is invalid; reauthorization is required"
				)
				return
			except KeyboardInterrupt:
				raise
			except Exception:
				if self._stop_event.is_set():
					return
				service_states["twitch"] = "reconnecting"
				twitch_logger.exception(
					"Twitch connector stopped unexpectedly; reconnecting in %.1fs",
					retry_delay,
				)
				if self._stop_event.wait(retry_delay):
					return
				retry_delay = min(60.0, retry_delay * 2.0)
				continue

			if self._stop_event.is_set():
				return
			service_states["twitch"] = "reconnecting"
			twitch_logger.warning(
				"Twitch connector returned without shutdown; reconnecting in %.1fs",
				retry_delay,
			)
			if self._stop_event.wait(retry_delay):
				return
			retry_delay = min(60.0, retry_delay * 2.0)

	def _seed_bootstrap_channels(self, connection: object) -> None:
		for channel_name in self.bootstrap_channels:
			upsert_twitch_channel(
				connection,
				channel_name=channel_name,
				requested_by_platform_account_id=None,
				request_source_message_id=None,
				join_source="config",
				status="active",
			)

	def _process_join_command(
		self,
		connection: object,
		normalized: NormalizedMessage,
		result: IngestionResult,
	) -> None:
		if normalized.channel_id.casefold() != self.join_command_channel:
			return
		if normalized.content_normalized != "!join":
			return
		if result.platform_account_id is None:
			return

		upsert_twitch_channel(
			connection,
			channel_name=normalized.username,
			requested_by_platform_account_id=result.platform_account_id,
			request_source_message_id=result.message_id,
			join_source="command",
			status="requested",
		)

	def health_snapshot(self) -> ConnectorHealth:
		return ConnectorHealth(
			name="twitch",
			status=self._last_status,
			details={"join_command_channel": self.join_command_channel},
		)

	def _validate_token_and_get_login(self, bot_token: str) -> str:
		if self._token_manager is None:
			self._token_manager = TwitchTokenManager(
				initial_access_token=bot_token,
				logger=self._logger,
			)

		try:
			validation = self._token_manager.validate_token()
		except TwitchAuthError as exc:
			raise TwitchConnectionError(str(exc)) from exc

		self._active_irc_token = validation.access_token
		return validation.login

	def send_message(
		self,
		channel_id: str,
		message: str,
	) -> None:
		irc_socket = self._active_socket

		if irc_socket is None:
			raise TwitchConnectionError(
				"Twitch IRC connection is not ready"
			)

		normalized_channel = (
			channel_id.strip()
			.removeprefix("#")
			.casefold()
		)

		if not normalized_channel:
			raise ValueError(
				"Twitch channel must not be empty"
			)

		if not message.strip():
			raise ValueError(
				"Twitch message must not be empty"
			)

		self._send_privmsg(
			irc_socket,
			normalized_channel,
			message,
		)

	def execute_pending_moderation_actions(
		self,
		connection,
		message_id: int,
	) -> None:
		message = connection.execute(
			"SELECT channel_id FROM messages WHERE id = ? AND platform = 'twitch'",
			(message_id,),
		).fetchone()
		if message is None:
			raise ValueError(f"Twitch message {message_id} was not found")

		pending_actions = list_pending_moderation_actions_for_message(
			connection,
			message_id,
		)
		for action in pending_actions:
			action_id = int(action[0])
			action_type = str(action[3]).strip().casefold()
			reason = str(action[4] or action_type).strip()
			username = str(action[5]).strip()
			duration_seconds = int(action[6]) if len(action) > 6 else 600
			channel_id = str(message[0]).strip()
			if not username or not channel_id:
				raise ValueError(
					f"Twitch moderation action {action_id} has no target or channel"
				)

			if action_type == "timeout":
				command = f"/timeout {username} {duration_seconds} {reason}"
			elif action_type == "ban":
				command = f"/ban {username} {reason}"
			elif action_type == "warn":
				command = f"@{username} Warning: {reason}"
			else:
				raise ValueError(
					f"Unsupported Twitch moderation action: {action_type}"
				)

			self.send_message(channel_id, command)
			mark_moderation_action_completed(connection, action_id)
			self._logger.warning(
				"executed twitch moderation action action_id=%s type=%s "
				"channel=%s user=%s",
				action_id,
				action_type,
				channel_id,
				username,
			)

	def _send_irc_line(
		self,
		irc_socket: ssl.SSLSocket,
		line: str,
	) -> None:
		with self._send_lock:
			irc_socket.sendall(
				f"{line}\r\n".encode("utf-8")
			)

	def _join_channel(self, irc_socket: ssl.SSLSocket, channel_name: str) -> None:
		normalized_channel_name = channel_name.strip().casefold()
		if not normalized_channel_name:
			return
		self._send_irc_line(irc_socket, f"JOIN #{normalized_channel_name}")

	def _requested_join_channel_from_payload(self, payload: Mapping[str, object]) -> str | None:
		channel = str(payload.get("channel") or "").strip().casefold()
		content = str(payload.get("content") or "").strip().casefold()
		username = str(payload.get("username") or payload.get("display_name") or "").strip().casefold()
		if channel != self.join_command_channel:
			return None
		if content != "!join":
			return None
		return username or None

	def _dispatch_registered_command(
		self,
		connection: object,
		normalized: NormalizedMessage,
	) -> object | None:
		context = CommandContext(
			platform="twitch",
			database_path=self.database_path,
			connection=connection,
			author_platform_user_id=normalized.platform_user_id,
			author_username=normalized.username,
			channel_id=normalized.channel_id,
			guild_id=None,
			message_id=normalized.platform_message_id,
			content=normalized.content_raw,
		)
		try:
			return self._command_registry.dispatch(normalized.content_raw, context)
		except Exception as exc:
			self._logger.warning("twitch command dispatch failed: %s", exc)
			return None

	def _send_privmsg(self, irc_socket: ssl.SSLSocket, channel_name: str, message: str) -> None:
		self._send_irc_line(irc_socket, f"PRIVMSG #{channel_name.strip().casefold()} :{message}")

	def _mark_channel_active(self, channel_name: str) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			update_twitch_channel_status(
				connection,
				channel_name=channel_name,
				status="active",
			)
		finally:
			connection.close()

	def _contains_streamboo_viewer_spam(self, content: str) -> bool:
		return contains_streamboo_viewer_spam(content)
