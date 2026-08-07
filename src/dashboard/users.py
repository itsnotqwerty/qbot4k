from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class UserListItem:
	user_id: int
	primary_display_name: str
	current_reputation_score: int
	candidate_flag: bool
	account_count: int
	message_count: int


@dataclass(frozen=True)
class UserRecentMessage:
	message_id: int
	platform: str
	username: str
	channel_id: str
	content_raw: str
	sent_at: str
	attachment_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserPlatformAccount:
	platform_account_id: int
	platform: str
	platform_user_id: str
	username: str


@dataclass(frozen=True)
class UserModerationStatus:
	open_reviews: int
	pending_actions: int
	completed_actions: int
	recent_actions: int


@dataclass(frozen=True)
class UserModerationAction:
	action_id: int
	platform: str
	target_username: str
	action_type: str
	status: str
	reason: str | None
	created_at: str


def search_users(
	connection: sqlite3.Connection,
	*,
	query: str = "",
	limit: int = 25,
	offset: int = 0,
) -> list[UserListItem]:
	search_term = query.strip()
	like_pattern = f"%{search_term}%" if search_term else "%"
	rows = connection.execute(
		"""
		SELECT
			results.user_id,
			results.primary_display_name,
			results.current_reputation_score,
			results.candidate_flag,
			results.account_count,
			results.message_count
		FROM (
			SELECT
				users.id AS user_id,
				users.primary_display_name,
				users.current_reputation_score,
				users.candidate_flag,
				COUNT(DISTINCT platform_accounts.id) AS account_count,
				COUNT(DISTINCT messages.id) AS message_count
			FROM users
			LEFT JOIN platform_accounts ON platform_accounts.user_id = users.id
			LEFT JOIN messages ON messages.platform_account_id = platform_accounts.id
			WHERE users.primary_display_name LIKE ?
			GROUP BY users.id

			UNION ALL

			SELECT
				-platform_accounts.id AS user_id,
				platform_accounts.username || ' (unlinked)' AS primary_display_name,
				500 AS current_reputation_score,
				0 AS candidate_flag,
				1 AS account_count,
				COUNT(DISTINCT messages.id) AS message_count
			FROM platform_accounts
			LEFT JOIN messages ON messages.platform_account_id = platform_accounts.id
			WHERE platform_accounts.user_id IS NULL
				AND platform_accounts.username LIKE ?
			GROUP BY platform_accounts.id
		) AS results
		ORDER BY results.current_reputation_score DESC, results.primary_display_name
		LIMIT ? OFFSET ?
		""",
		(like_pattern, like_pattern, limit, offset),
	).fetchall()
	return [
		UserListItem(
			user_id=int(row[0]),
			primary_display_name=str(row[1]),
			current_reputation_score=int(row[2]),
			candidate_flag=bool(row[3]),
			account_count=int(row[4]),
			message_count=int(row[5]),
		)
		for row in rows
	]


def list_recent_user_messages(
	connection: sqlite3.Connection,
	user_id: int,
	*,
	limit: int = 25,
) -> list[UserRecentMessage]:
	if user_id >= 0:
		rows = connection.execute(
			"""
			SELECT
				messages.id,
				messages.platform,
				platform_accounts.username,
				CASE
					WHEN messages.platform = 'discord' AND discord_channels.channel_name IS NOT NULL THEN '#' || discord_channels.channel_name
					ELSE messages.channel_id
				END AS channel_display,
				messages.content_raw,
				messages.sent_at
			FROM messages
			INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
			LEFT JOIN discord_channels ON (
				discord_channels.channel_id = messages.channel_id
				AND messages.platform = 'discord'
			)
			WHERE platform_accounts.user_id = ?
			ORDER BY messages.sent_at DESC, messages.id DESC
			LIMIT ?
			""",
			(user_id, limit),
		).fetchall()
	else:
		platform_account_id = -user_id
		rows = connection.execute(
			"""
			SELECT
				messages.id,
				messages.platform,
				platform_accounts.username,
				CASE
					WHEN messages.platform = 'discord' AND discord_channels.channel_name IS NOT NULL THEN '#' || discord_channels.channel_name
					ELSE messages.channel_id
				END AS channel_display,
				messages.content_raw,
				messages.sent_at
			FROM messages
			INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
			LEFT JOIN discord_channels ON (
				discord_channels.channel_id = messages.channel_id
				AND messages.platform = 'discord'
			)
			WHERE platform_accounts.id = ?
			ORDER BY messages.sent_at DESC, messages.id DESC
			LIMIT ?
			""",
			(platform_account_id, limit),
		).fetchall()

	message_ids = tuple(int(row[0]) for row in rows)
	attachment_map: dict[int, tuple[str, ...]] = {}
	if message_ids:
		placeholders = ",".join("?" for _ in message_ids)
		attachment_rows = connection.execute(
			f"""
			SELECT message_id, attachment_url
			FROM message_attachments
			WHERE message_id IN ({placeholders})
			ORDER BY message_id, attachment_index
			""",
			message_ids,
		).fetchall()
		buffer: dict[int, list[str]] = {}
		for row in attachment_rows:
			message_id = int(row[0])
			url = str(row[1])
			buffer.setdefault(message_id, []).append(url)
		attachment_map = {message_id: tuple(urls) for message_id, urls in buffer.items()}

	return [
		UserRecentMessage(
			message_id=int(row[0]),
			platform=str(row[1]),
			username=str(row[2]),
			channel_id=str(row[3]),
			content_raw=str(row[4]),
			sent_at=str(row[5]),
			attachment_urls=attachment_map.get(int(row[0]), ()),
		)
		for row in rows
	]


def list_user_platform_accounts(connection: sqlite3.Connection, user_id: int) -> list[UserPlatformAccount]:
	if user_id >= 0:
		rows = connection.execute(
			"""
			SELECT id, platform, platform_user_id, username
			FROM platform_accounts
			WHERE user_id = ?
			ORDER BY platform, username, id
			""",
			(user_id,),
		).fetchall()
	else:
		rows = connection.execute(
			"""
			SELECT id, platform, platform_user_id, username
			FROM platform_accounts
			WHERE id = ?
			ORDER BY id
			""",
			(-user_id,),
		).fetchall()

	return [
		UserPlatformAccount(
			platform_account_id=int(row[0]),
			platform=str(row[1]),
			platform_user_id=str(row[2]),
			username=str(row[3]),
		)
		for row in rows
	]


def get_user_moderation_status(connection: sqlite3.Connection, user_id: int) -> UserModerationStatus:
	account_ids = _resolve_platform_account_ids(connection, user_id)
	if not account_ids:
		return UserModerationStatus(open_reviews=0, pending_actions=0, completed_actions=0, recent_actions=0)

	bindings = tuple(account_ids)
	placeholders = ",".join("?" for _ in bindings)

	open_reviews = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM review_queue
			INNER JOIN messages ON messages.id = review_queue.message_id
			WHERE review_queue.status = 'open'
			  AND messages.platform_account_id IN ({placeholders})
			""",
			bindings,
		).fetchone()[0]
	)
	pending_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE status = 'pending'
			  AND target_platform_account_id IN ({placeholders})
			""",
			bindings,
		).fetchone()[0]
	)
	completed_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE status = 'completed'
			  AND target_platform_account_id IN ({placeholders})
			""",
			bindings,
		).fetchone()[0]
	)
	recent_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE target_platform_account_id IN ({placeholders})
			""",
			bindings,
		).fetchone()[0]
	)

	return UserModerationStatus(
		open_reviews=open_reviews,
		pending_actions=pending_actions,
		completed_actions=completed_actions,
		recent_actions=recent_actions,
	)


def list_recent_user_moderation_actions(
	connection: sqlite3.Connection,
	user_id: int,
	*,
	limit: int = 10,
) -> list[UserModerationAction]:
	account_ids = _resolve_platform_account_ids(connection, user_id)
	if not account_ids:
		return []

	bindings = tuple(account_ids)
	placeholders = ",".join("?" for _ in bindings)
	rows = connection.execute(
		f"""
		SELECT
			moderation_actions.id,
			moderation_actions.platform,
			platform_accounts.username,
			moderation_actions.action_type,
			moderation_actions.status,
			moderation_actions.reason,
			moderation_actions.created_at
		FROM moderation_actions
		INNER JOIN platform_accounts ON platform_accounts.id = moderation_actions.target_platform_account_id
		WHERE moderation_actions.target_platform_account_id IN ({placeholders})
		ORDER BY moderation_actions.created_at DESC, moderation_actions.id DESC
		LIMIT ?
		""",
		(*bindings, limit),
	).fetchall()

	return [
		UserModerationAction(
			action_id=int(row[0]),
			platform=str(row[1]),
			target_username=str(row[2]),
			action_type=str(row[3]),
			status=str(row[4]),
			reason=str(row[5]) if row[5] is not None else None,
			created_at=str(row[6]),
		)
		for row in rows
	]


def _resolve_platform_account_ids(connection: sqlite3.Connection, user_id: int) -> list[int]:
	if user_id >= 0:
		rows = connection.execute(
			"SELECT id FROM platform_accounts WHERE user_id = ?",
			(user_id,),
		).fetchall()
	else:
		rows = connection.execute(
			"SELECT id FROM platform_accounts WHERE id = ?",
			(-user_id,),
		).fetchall()
	return [int(row[0]) for row in rows]
