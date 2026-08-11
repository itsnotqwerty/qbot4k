from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass

from ..db import enqueue_processing_job, record_moderation_action, upsert_moderation_rule


@dataclass(frozen=True)
class ReviewItem:
	review_id: int
	message_id: int
	target_username: str
	severity: str
	reason_code: str
	status: str
	platform: str
	content: str
	created_at: str
	assigned_operator_id: int | None


@dataclass(frozen=True)
class ModerationActionItem:
	action_id: int
	platform: str
	target_username: str
	action_type: str
	status: str
	reason: str | None
	error_message: str | None
	created_at: str


@dataclass(frozen=True)
class ModerationRuleItem:
	rule_id: int
	name: str
	rule_type: str
	pattern: str
	severity: str
	auto_enforce_action: str | None
	enabled: bool
	enforcement_mode: str
	action_duration_seconds: int


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
			,messages.platform
			,messages.content_raw
			,review_queue.created_at
			,review_queue.assigned_operator_id
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
			platform=str(row[6]),
			content=str(row[7]),
			created_at=str(row[8]),
			assigned_operator_id=int(row[9]) if row[9] is not None else None,
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
			,moderation_actions.error_message
			,moderation_actions.created_at
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
			error_message=str(row[6]) if row[6] is not None else None,
			created_at=str(row[7]),
		)
		for row in rows
	]


def list_moderation_rules(connection: sqlite3.Connection) -> list[ModerationRuleItem]:
	rows = connection.execute(
		"""SELECT id,name,rule_type,pattern,severity,auto_enforce_action,enabled,
		          enforcement_mode,action_duration_seconds
		   FROM moderation_rules ORDER BY name COLLATE NOCASE"""
	).fetchall()
	return [ModerationRuleItem(
		rule_id=int(row[0]), name=str(row[1]), rule_type=str(row[2]), pattern=str(row[3]),
		severity=str(row[4]), auto_enforce_action=str(row[5]) if row[5] is not None else None,
		enabled=bool(row[6]), enforcement_mode=str(row[7]), action_duration_seconds=int(row[8]),
	) for row in rows]


def save_moderation_rule(
	connection: sqlite3.Connection,
	*,
	name: str,
	rule_type: str,
	pattern: str,
	severity: str,
	auto_enforce_action: str | None,
	enabled: bool,
	enforcement_mode: str,
	action_duration_seconds: int,
	operator_id: int,
) -> int:
	rule_id = upsert_moderation_rule(
		connection, name=name, rule_type=rule_type, pattern=pattern, severity=severity,
		auto_enforce_action=auto_enforce_action, enabled=enabled,
		enforcement_mode=enforcement_mode, action_duration_seconds=action_duration_seconds,
	)
	connection.execute(
		"""INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
		   VALUES ('operator',?,'moderation.rule_saved','moderation_rule',?,?)""",
		(operator_id, rule_id, json.dumps({"name": name, "mode": enforcement_mode, "enabled": enabled}, sort_keys=True)),
	)
	return rule_id


def resolve_review(
	connection: sqlite3.Connection,
	review_id: int,
	*,
	resolution: str,
	operator_id: int,
	note: str = "",
	action_type: str | None = None,
	duration_seconds: int = 600,
) -> int | None:
	normalized_resolution = resolution.strip().casefold()
	normalized_action = action_type.strip().casefold() if action_type else None
	if normalized_resolution not in {"dismissed", "confirmed", "escalated"}:
		raise ValueError("invalid review resolution")
	if normalized_action not in {None, "warn", "timeout", "ban"}:
		raise ValueError("invalid review action")
	row = connection.execute(
		"""SELECT q.status,q.message_id,m.platform,m.observation_id,m.platform_account_id
		   FROM review_queue q JOIN messages m ON m.id=q.message_id WHERE q.id=?""",
		(review_id,),
	).fetchone()
	if row is None:
		raise ValueError("review not found")
	if str(row[0]) != "open":
		raise ValueError("review is already resolved")
	action_id = None
	if normalized_resolution == "confirmed" and normalized_action is not None:
		action_id = record_moderation_action(
			connection, platform=str(row[2]), message_id=int(row[1]),
			target_platform_account_id=int(row[4]), action_type=normalized_action,
			reason=note.strip() or "Confirmed analyst review", status="pending",
			actor_type="operator", actor_id=operator_id,
		)
		connection.execute(
			"UPDATE moderation_actions SET duration_seconds=? WHERE id=?",
			(max(1, min(int(duration_seconds), 2_419_200)), action_id),
		)
		enqueue_processing_job(
			connection, stage="action", job_type=f"{row[2]}.moderation.execute",
			observation_id=int(row[3]) if row[3] is not None else None,
			payload={"message_id": int(row[1])},
			idempotency_key=f"review:{review_id}:moderation:{normalized_action}", priority=10,
		)
	connection.execute(
		"""UPDATE review_queue SET status='resolved',resolution=?,resolution_note=?,
		   resolved_by_operator_id=?,assigned_operator_id=COALESCE(assigned_operator_id,?),
		   resolved_at=CURRENT_TIMESTAMP WHERE id=?""",
		(normalized_resolution, note.strip(), operator_id, operator_id, review_id),
	)
	connection.execute(
		"""INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
		   VALUES ('operator',?,'moderation.review_resolved','review_queue',?,?)""",
		(operator_id, review_id, json.dumps({"resolution": normalized_resolution, "action": normalized_action,
			"action_id": action_id, "note": note.strip()}, sort_keys=True)),
	)
	return action_id
