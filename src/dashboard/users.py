from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime


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


@dataclass(frozen=True)
class UserLifecycleEvent:
	occurred_at: str
	event_type: str
	summary: str
	detail: str | None = None


def search_users(
	connection: sqlite3.Connection,
	*,
	community_id: int,
	query: str = "",
	sort_by: str = "score",
	sort_dir: str = "desc",
	limit: int = 25,
	offset: int = 0,
) -> list[UserListItem]:
	search_term = query.strip()
	like_pattern = f"%{search_term}%" if search_term else "%"

	normalized_sort = sort_by.strip().casefold() if sort_by else "score"
	if normalized_sort not in {"score", "messages", "poweruser", "accounts", "name"}:
		normalized_sort = "score"

	normalized_direction = sort_dir.strip().casefold() if sort_dir else ""
	default_direction = "asc" if normalized_sort == "name" else "desc"
	if normalized_direction not in {"asc", "desc"}:
		normalized_direction = default_direction

	order_column = {
		"score": "results.current_reputation_score",
		"messages": "results.message_count",
		"poweruser": "results.candidate_flag",
		"accounts": "results.account_count",
		"name": "results.primary_display_name COLLATE NOCASE",
	}[normalized_sort]
	order_direction = "ASC" if normalized_direction == "asc" else "DESC"
	rows = connection.execute(
		f"""
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
				COUNT(DISTINCT messages.platform_account_id) AS account_count,
				COUNT(DISTINCT messages.id) AS message_count
			FROM users
			INNER JOIN messages ON messages.user_id = users.id AND messages.community_id = ?
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
			INNER JOIN messages ON messages.platform_account_id = platform_accounts.id
				AND messages.user_id IS NULL AND messages.community_id = ?
			WHERE platform_accounts.user_id IS NULL
				AND platform_accounts.detached_from_user_id IS NULL
				AND platform_accounts.username LIKE ?
			GROUP BY platform_accounts.id
		) AS results
		ORDER BY {order_column} {order_direction}, results.current_reputation_score DESC, results.primary_display_name COLLATE NOCASE ASC
		LIMIT ? OFFSET ?
		""",
		(int(community_id), like_pattern, int(community_id), like_pattern, limit, offset),
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
	community_id: int,
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
			WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
			  AND messages.community_id = ?
			ORDER BY messages.sent_at DESC, messages.id DESC
			LIMIT ?
			""",
			(user_id, int(community_id), limit),
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
			WHERE platform_accounts.id = ? AND messages.community_id = ?
			ORDER BY messages.sent_at DESC, messages.id DESC
			LIMIT ?
			""",
			(platform_account_id, int(community_id), limit),
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


def list_user_platform_accounts(
	connection: sqlite3.Connection, user_id: int, *, community_id: int
) -> list[UserPlatformAccount]:
	if user_id >= 0:
		rows = connection.execute(
			"""
			SELECT id, platform, platform_user_id, username
			FROM platform_accounts
			WHERE user_id = ? AND EXISTS (
				SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
				AND messages.community_id=?
			)
			ORDER BY platform, username, id
			""",
			(user_id, int(community_id)),
		).fetchall()
	else:
		rows = connection.execute(
			"""
			SELECT id, platform, platform_user_id, username
			FROM platform_accounts
			WHERE id = ? AND EXISTS (
				SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
				AND messages.community_id=?
			)
			ORDER BY id
			""",
			(-user_id, int(community_id)),
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


def get_user_moderation_status(
	connection: sqlite3.Connection, user_id: int, *, community_id: int
) -> UserModerationStatus:
	if user_id >= 0:
		open_reviews = int(
			connection.execute(
				"""
				SELECT COUNT(*)
				FROM review_queue
				INNER JOIN messages ON messages.id = review_queue.message_id
				INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
				WHERE review_queue.status = 'open'
				  AND COALESCE(messages.user_id, platform_accounts.user_id) = ?
				  AND messages.community_id = ?
				""",
				(user_id, int(community_id)),
			).fetchone()[0]
		)
		counts = connection.execute(
			"""
			SELECT
				SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
				SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
				COUNT(*)
			FROM moderation_actions
			INNER JOIN platform_accounts
				ON platform_accounts.id = moderation_actions.target_platform_account_id
			WHERE COALESCE(moderation_actions.user_id, platform_accounts.user_id) = ?
			  AND moderation_actions.community_id = ?
			""",
			(user_id, int(community_id)),
		).fetchone()
		return UserModerationStatus(
			open_reviews=open_reviews,
			pending_actions=int(counts[0] or 0),
			completed_actions=int(counts[1] or 0),
			recent_actions=int(counts[2] or 0),
		)

	account_ids = _resolve_platform_account_ids(connection, user_id, community_id=community_id)
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
			  AND messages.community_id = ?
			""",
			(*bindings, int(community_id)),
		).fetchone()[0]
	)
	pending_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE status = 'pending'
			  AND target_platform_account_id IN ({placeholders})
			  AND community_id = ?
			""",
			(*bindings, int(community_id)),
		).fetchone()[0]
	)
	completed_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE status = 'completed'
			  AND target_platform_account_id IN ({placeholders})
			  AND community_id = ?
			""",
			(*bindings, int(community_id)),
		).fetchone()[0]
	)
	recent_actions = int(
		connection.execute(
			f"""
			SELECT COUNT(*)
			FROM moderation_actions
			WHERE target_platform_account_id IN ({placeholders})
			  AND community_id = ?
			""",
			(*bindings, int(community_id)),
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
	community_id: int,
	limit: int = 10,
) -> list[UserModerationAction]:
	if user_id >= 0:
		rows = connection.execute(
			"""
			SELECT
				moderation_actions.id,
				moderation_actions.platform,
				platform_accounts.username,
				moderation_actions.action_type,
				moderation_actions.status,
				moderation_actions.reason,
				moderation_actions.created_at
			FROM moderation_actions
			INNER JOIN platform_accounts
				ON platform_accounts.id = moderation_actions.target_platform_account_id
			WHERE COALESCE(moderation_actions.user_id, platform_accounts.user_id) = ?
			  AND moderation_actions.community_id = ?
			ORDER BY moderation_actions.created_at DESC, moderation_actions.id DESC
			LIMIT ?
			""",
			(user_id, int(community_id), limit),
		).fetchall()
		return [_moderation_action_from_row(row) for row in rows]

	account_ids = _resolve_platform_account_ids(connection, user_id, community_id=community_id)
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
		  AND moderation_actions.community_id = ?
		ORDER BY moderation_actions.created_at DESC, moderation_actions.id DESC
		LIMIT ?
		""",
		(*bindings, int(community_id), limit),
	).fetchall()

	return [_moderation_action_from_row(row) for row in rows]


def list_user_lifecycle_events(
	connection: sqlite3.Connection,
	user_id: int,
	*,
	community_id: int,
	limit: int = 50,
	event_types: Iterable[str] = (),
) -> list[UserLifecycleEvent]:
	account_ids = _resolve_platform_account_ids(connection, user_id, community_id=community_id)
	if not account_ids:
		return []
	placeholders = ",".join("?" for _ in account_ids)
	events: list[UserLifecycleEvent] = []
	observation_rows = connection.execute(
		f"""SELECT event_type,occurred_at,attributes_json
		    FROM observations
		    WHERE community_id=? AND target_platform_account_id IN ({placeholders})
		      AND event_type IN (
		          'member.joined','member.left','member.roles_changed',
		          'moderation.ban_added','moderation.ban_removed'
		      )""",
		(int(community_id), *account_ids),
	).fetchall()
	labels = {
		"member.joined": "Joined community",
		"member.left": "Left community",
		"member.roles_changed": "Discord roles changed",
		"moderation.ban_added": "Discord ban added",
		"moderation.ban_removed": "Discord ban removed",
	}
	for row in observation_rows:
		detail = None
		if str(row[0]) == "member.roles_changed":
			try:
				attributes = json.loads(str(row[2] or "{}"))
			except json.JSONDecodeError:
				attributes = {}
			roles = attributes.get("roles") if isinstance(attributes, dict) else None
			role_names = (
				attributes.get("resolved_role_names") if isinstance(attributes, dict) else None
			)
			if isinstance(role_names, list) and role_names:
				detail = "Roles: " + ", ".join(str(role) for role in role_names)
			elif isinstance(roles, list):
				detail = "Role IDs: " + (", ".join(str(role) for role in roles) or "none")
		events.append(UserLifecycleEvent(str(row[1]), str(row[0]), labels[str(row[0])], detail))

	if user_id >= 0:
		for row in connection.execute(
			"""SELECT created_at,body FROM user_notes
			   WHERE community_id=? AND user_id=?""",
			(int(community_id), int(user_id)),
		).fetchall():
			events.append(UserLifecycleEvent(str(row[0]), "note", "Operator note", str(row[1])))

	for row in connection.execute(
		f"""SELECT created_at,action_type,status,reason FROM moderation_actions
		    WHERE community_id=? AND target_platform_account_id IN ({placeholders})""",
		(int(community_id), *account_ids),
	).fetchall():
		detail_parts = [str(row[2])]
		if row[3]:
			detail_parts.append(str(row[3]))
		events.append(UserLifecycleEvent(
			str(row[0]), "moderation", f"Moderation: {row[1]}", " · ".join(detail_parts)
		))

	for row in connection.execute(
		f"""SELECT verified_at,verification_evidence FROM community_onboarding_members
		    WHERE community_id=? AND verified_at IS NOT NULL
		      AND platform_user_id IN (
		          SELECT platform_user_id FROM platform_accounts WHERE id IN ({placeholders})
		      )""",
		(int(community_id), *account_ids),
	).fetchall():
		events.append(UserLifecycleEvent(
			str(row[0]), "verification", "Verification completed",
			str(row[1]) if row[1] else None,
		))

	normalized_types = {str(event_type).strip() for event_type in event_types if str(event_type).strip()}
	if normalized_types:
		events = [event for event in events if event.event_type in normalized_types]
	events.sort(key=lambda event: _lifecycle_timestamp(event.occurred_at), reverse=True)
	return events[:max(1, min(int(limit), 200))]


def _lifecycle_timestamp(value: str) -> datetime:
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return datetime.min.replace(tzinfo=UTC)
	return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _moderation_action_from_row(row: sqlite3.Row | tuple[object, ...]) -> UserModerationAction:
	return UserModerationAction(
		action_id=int(row[0]),
		platform=str(row[1]),
		target_username=str(row[2]),
		action_type=str(row[3]),
		status=str(row[4]),
		reason=str(row[5]) if row[5] is not None else None,
		created_at=str(row[6]),
	)


def user_is_visible(
	connection: sqlite3.Connection, user_id: int, *, community_id: int
) -> bool:
	return bool(_resolve_platform_account_ids(connection, user_id, community_id=community_id))


def _resolve_platform_account_ids(
	connection: sqlite3.Connection, user_id: int, *, community_id: int
) -> list[int]:
	if user_id >= 0:
		rows = connection.execute(
			"""SELECT id FROM platform_accounts WHERE user_id = ? AND EXISTS (
			       SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
			       AND messages.community_id=?
			   )""",
			(user_id, int(community_id)),
		).fetchall()
	else:
		rows = connection.execute(
			"""SELECT id FROM platform_accounts WHERE id = ? AND EXISTS (
			       SELECT 1 FROM messages WHERE messages.platform_account_id=platform_accounts.id
			       AND messages.community_id=?
			   )""",
			(-user_id, int(community_id)),
		).fetchall()
	return [int(row[0]) for row in rows]
