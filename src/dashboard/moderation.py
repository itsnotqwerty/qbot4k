from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewItem:
	review_id: int
	message_id: int
	target_username: str
	severity: str
	reason_code: str
	status: str


@dataclass(frozen=True)
class ModerationActionItem:
	action_id: int
	platform: str
	target_username: str
	action_type: str
	status: str
	reason: str | None


def list_open_reviews(connection: sqlite3.Connection, limit: int = 25) -> list[ReviewItem]:
	rows = connection.execute(
		"""
		SELECT
			review_queue.id,
			review_queue.message_id,
			platform_accounts.username,
			review_queue.severity,
			review_queue.queue_reason_code,
			review_queue.status
		FROM review_queue
		INNER JOIN messages ON messages.id = review_queue.message_id
		INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
		WHERE review_queue.status = 'open'
		ORDER BY review_queue.created_at DESC, review_queue.id DESC
		LIMIT ?
		""",
		(limit,),
	).fetchall()
	return [
		ReviewItem(
			review_id=int(row[0]),
			message_id=int(row[1]),
			target_username=str(row[2]),
			severity=str(row[3]),
			reason_code=str(row[4]),
			status=str(row[5]),
		)
		for row in rows
	]


def list_recent_actions(connection: sqlite3.Connection, limit: int = 25) -> list[ModerationActionItem]:
	rows = connection.execute(
		"""
		SELECT
			moderation_actions.id,
			moderation_actions.platform,
			platform_accounts.username,
			moderation_actions.action_type,
			moderation_actions.status,
			moderation_actions.reason
		FROM moderation_actions
		INNER JOIN platform_accounts ON platform_accounts.id = moderation_actions.target_platform_account_id
		ORDER BY moderation_actions.created_at DESC, moderation_actions.id DESC
		LIMIT ?
		""",
		(limit,),
	).fetchall()
	return [
		ModerationActionItem(
			action_id=int(row[0]),
			platform=str(row[1]),
			target_username=str(row[2]),
			action_type=str(row[3]),
			status=str(row[4]),
			reason=str(row[5]) if row[5] is not None else None,
		)
		for row in rows
	]
