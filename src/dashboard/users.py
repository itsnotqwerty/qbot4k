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
	platform: str
	username: str
	channel_id: str
	content_raw: str
	sent_at: str


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

	return [
		UserRecentMessage(
			platform=str(row[0]),
			username=str(row[1]),
			channel_id=str(row[2]),
			content_raw=str(row[3]),
			sent_at=str(row[4]),
		)
		for row in rows
	]
