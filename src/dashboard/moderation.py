from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewItem:
	review_id: int
	message_id: int
	severity: str
	reason_code: str
	status: str


@dataclass(frozen=True)
class ModerationActionItem:
	action_id: int
	platform: str
	action_type: str
	status: str
	reason: str | None


def list_open_reviews(connection: sqlite3.Connection, limit: int = 25) -> list[ReviewItem]:
	rows = connection.execute(
		"""
		SELECT id, message_id, severity, queue_reason_code, status
		FROM review_queue
		WHERE status = 'open'
		ORDER BY created_at DESC, id DESC
		LIMIT ?
		""",
		(limit,),
	).fetchall()
	return [
		ReviewItem(
			review_id=int(row[0]),
			message_id=int(row[1]),
			severity=str(row[2]),
			reason_code=str(row[3]),
			status=str(row[4]),
		)
		for row in rows
	]


def list_recent_actions(connection: sqlite3.Connection, limit: int = 25) -> list[ModerationActionItem]:
	rows = connection.execute(
		"""
		SELECT id, platform, action_type, status, reason
		FROM moderation_actions
		ORDER BY created_at DESC, id DESC
		LIMIT ?
		""",
		(limit,),
	).fetchall()
	return [
		ModerationActionItem(
			action_id=int(row[0]),
			platform=str(row[1]),
			action_type=str(row[2]),
			status=str(row[3]),
			reason=str(row[4]) if row[4] is not None else None,
		)
		for row in rows
	]
