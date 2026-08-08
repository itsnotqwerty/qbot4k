from __future__ import annotations

import logging
import re
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
	list_twitch_channels,
	persist_normalized_message,
	record_moderation_action,
	update_twitch_channel_status,
	upsert_twitch_channel,
)
from .intelligence.powerusers import apply_reputation_event, score_delta_for_moderation
from .models import ConnectorHealth, IngestionResult, NormalizedMessage, coerce_timestamp
from .twitch_auth import TwitchAuthError, TwitchTokenManager


class TwitchPayloadError(ValueError):
	pass


class TwitchConnectionError(RuntimeError):
	pass


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
		self._streamboo_term_pattern = re.compile(r"(?<!\\w)viewers?(?!\\w)", re.IGNORECASE)
		self._stop_event = threading.Event()
		self._active_socket: ssl.SSLSocket | None = None

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

	def ingest_message(
		self,
		payload: Mapping[str, object],
		*,
		reply_sink: callable | None = None,
	) -> IngestionResult:
		normalized = normalize_twitch_message(payload)
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			self._seed_bootstrap_channels(connection)
			result = persist_normalized_message(connection, normalized)
			self._process_join_command(connection, normalized, result)
			reply = self._dispatch_registered_command(connection, normalized)
			if reply is not None and reply_sink is not None:
				reply_sink(render_command_reply(reply, "twitch"))
			self._last_status = "ready"
			return result
		finally:
			connection.close()

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
					self._send_irc_line(irc_socket, "CAP REQ :twitch.tv/tags twitch.tv/commands")
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
							continue

						result = self.ingest_message(
							payload,
							reply_sink=lambda message: self._send_privmsg(
								irc_socket,
								str(payload["channel"]),
								message,
							),
						)
						self._logger.info(
							"ingested twitch message channel=%s user=%s status=%s",
							payload["channel"],
							payload["username"],
							result.status,
						)

						self._maybe_auto_moderate_streamboo_viewer_spam(
							irc_socket,
							payload,
							result,
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

	def _send_irc_line(self, irc_socket: ssl.SSLSocket, line: str) -> None:
		irc_socket.sendall(f"{line}\r\n".encode("utf-8"))

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

	def _maybe_auto_moderate_streamboo_viewer_spam(
		self,
		irc_socket: ssl.SSLSocket,
		payload: Mapping[str, object],
		result: IngestionResult,
	) -> None:
		if result.status != "persisted":
			return
		if result.message_id is None or result.platform_account_id is None:
			return
		if bool(payload.get("is_moderator")):
			return

		content = str(payload.get("content") or "")
		if not self._contains_streamboo_viewer_spam(content):
			return

		target_username = str(payload.get("username") or payload.get("display_name") or "").strip()
		channel_name = str(payload.get("channel") or "").strip()
		if not target_username or not channel_name:
			return

		reason = "streamboo_viewer_spam"
		self._send_privmsg(
			irc_socket,
			channel_name,
			f"/timeout {target_username} 600 Streamboo viewer-buying spam",
		)
		self._record_moderation_action(
			message_id=result.message_id,
			target_platform_account_id=result.platform_account_id,
			action_type="timeout",
			reason=reason,
		)
		self._apply_streamboo_penalty(
			target_platform_account_id=result.platform_account_id,
			message_id=result.message_id,
		)
		self._logger.warning(
			"auto-moderated twitch message channel=%s user=%s reason=%s",
			channel_name,
			target_username,
			reason,
		)

	def _contains_streamboo_viewer_spam(self, content: str) -> bool:
		normalized = content.casefold()
		if "streamboo" not in normalized:
			return False
		return self._streamboo_term_pattern.search(normalized) is not None

	def _record_moderation_action(
		self,
		*,
		message_id: int,
		target_platform_account_id: int,
		action_type: str,
		reason: str,
	) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			record_moderation_action(
				connection,
				platform="twitch",
				message_id=message_id,
				target_platform_account_id=target_platform_account_id,
				action_type=action_type,
				reason=reason,
				status="completed",
			)
		finally:
			connection.close()

	def _apply_streamboo_penalty(
		self,
		*,
		target_platform_account_id: int,
		message_id: int,
	) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_row = connection.execute(
				"SELECT user_id FROM platform_accounts WHERE id = ?",
				(target_platform_account_id,),
			).fetchone()
			if user_row is None or user_row[0] is None:
				return
			delta, penalty_reason = score_delta_for_moderation(
				severity="high",
				action_type="timeout",
				reason_code="streamboo_viewer_spam",
			)
			apply_reputation_event(
				connection,
				user_id=int(user_row[0]),
				delta=delta,
				reason_code=penalty_reason,
				source_type="moderation",
				source_id=message_id,
			)
		finally:
			connection.close()
