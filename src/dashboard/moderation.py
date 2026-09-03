from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from ..contexts import ActorAttribution, TenantContext
from ..db import enqueue_processing_job, record_moderation_action, upsert_moderation_rule


@dataclass(frozen=True)
class ReviewItem:
	review_id: int
	message_id: int
	target_platform_account_id: int
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


@dataclass(frozen=True)
class MemberQueueItem:
	item_id: int
	queue_type: str
	username: str
	severity: str
	category_or_reason: str
	summary: str
	assigned_operator_id: int | None
	created_at: str


@dataclass(frozen=True)
class ModerationWorkItem:
	work_type: str
	item_id: int
	platform: str
	username: str
	severity: str
	reason: str
	summary: str
	assigned_operator_id: int | None
	status: str
	created_at: str
	sla_age_hours: float


def list_member_queue(
	connection: sqlite3.Connection, *, queue_type: str, community_id: int, limit: int = 25,
) -> list[MemberQueueItem]:
	normalized_type = queue_type.strip().casefold()
	if normalized_type == "reports":
		rows = connection.execute(
			"""SELECT r.id,pa.username,r.severity,r.category,r.summary,
			          r.assigned_operator_id,r.created_at
			   FROM member_reports r
			   JOIN platform_accounts pa ON pa.id=r.subject_platform_account_id
			   WHERE r.community_id=? AND r.status='open'
			   ORDER BY CASE r.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
			            WHEN 'medium' THEN 2 ELSE 3 END,r.created_at,r.id LIMIT ?""",
			(int(community_id), int(limit)),
		).fetchall()
	elif normalized_type == "appeals":
		rows = connection.execute(
			"""SELECT a.id,pa.username,a.severity,a.reason,ma.action_type,
			          a.assigned_operator_id,a.created_at
			   FROM member_appeals a
			   JOIN platform_accounts pa ON pa.id=a.appellant_platform_account_id
			   JOIN moderation_actions ma ON ma.id=a.moderation_action_id
			   WHERE a.community_id=? AND a.status='open'
			   ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
			            WHEN 'medium' THEN 2 ELSE 3 END,a.created_at,a.id LIMIT ?""",
			(int(community_id), int(limit)),
		).fetchall()
	else:
		raise ValueError("invalid member queue type")
	return [MemberQueueItem(
		item_id=int(row[0]), queue_type=normalized_type, username=str(row[1]),
		severity=str(row[2]), category_or_reason=str(row[3]), summary=str(row[4]),
		assigned_operator_id=int(row[5]) if row[5] is not None else None,
		created_at=str(row[6]),
	) for row in rows]


def list_open_reviews(
	connection: sqlite3.Connection, limit: int = 25, *, community_id: int
) -> list[ReviewItem]:
	rows = connection.execute(
		"""
		SELECT
			review_queue.id,
			review_queue.message_id,
			platform_accounts.id,
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
		WHERE review_queue.status = 'open' AND messages.community_id = ?
		ORDER BY review_queue.created_at DESC, review_queue.id DESC
		LIMIT ?
		""",
		(int(community_id), limit),
	).fetchall()
	return [
		ReviewItem(
			review_id=int(row[0]),
			message_id=int(row[1]),
			target_platform_account_id=int(row[2]),
			target_username=str(row[3]),
			severity=str(row[4]),
			reason_code=str(row[5]),
			status=str(row[6]),
			platform=str(row[7]),
			content=str(row[8]),
			created_at=str(row[9]),
			assigned_operator_id=int(row[10]) if row[10] is not None else None,
		)
		for row in rows
	]


def list_recent_actions(
	connection: sqlite3.Connection, limit: int = 25, *, community_id: int
) -> list[ModerationActionItem]:
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
		WHERE moderation_actions.community_id = ?
		ORDER BY moderation_actions.created_at DESC, moderation_actions.id DESC
		LIMIT ?
		""",
		(int(community_id), limit),
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


def list_moderation_rules(
	connection: sqlite3.Connection, *, community_id: int
) -> list[ModerationRuleItem]:
	rows = connection.execute(
		"""SELECT id,name,rule_type,pattern,severity,auto_enforce_action,enabled,
		          enforcement_mode,action_duration_seconds
		   FROM moderation_rules WHERE community_id=? ORDER BY name COLLATE NOCASE""",
		(int(community_id),),
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
	community_id: int = 1,
) -> int:
	rule_id = upsert_moderation_rule(
		connection, name=name, rule_type=rule_type, pattern=pattern, severity=severity,
		auto_enforce_action=auto_enforce_action, enabled=enabled,
		enforcement_mode=enforcement_mode, action_duration_seconds=action_duration_seconds,
		community_id=community_id,
	)
	connection.execute(
		"""INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
		   VALUES ('operator',?,'moderation.rule_saved','moderation_rule',?,?)""",
		(operator_id, rule_id, json.dumps({"community_id": int(community_id), "name": name, "mode": enforcement_mode, "enabled": enabled}, sort_keys=True)),
	)
	return rule_id


def execute_bulk_moderation(
	connection: sqlite3.Connection, *, tenant: TenantContext, actor: ActorAttribution,
	target_platform_account_ids: list[int], action_type: str, reason: str,
	duration_seconds: int = 600, dry_run: bool = True,
) -> dict[str, object]:
	operator_id = _require_operator(actor)
	community_id = tenant.community_id
	normalized_action = action_type.strip().casefold()
	normalized_reason = reason.strip()
	target_ids = list(dict.fromkeys(int(item) for item in target_platform_account_ids))
	if not target_ids or len(target_ids) > 25:
		raise ValueError("bulk moderation requires 1 to 25 explicit targets")
	if normalized_action not in {"warn", "timeout", "ban"}:
		raise ValueError("invalid bulk moderation action")
	if not normalized_reason:
		raise ValueError("bulk moderation reason is required")
	duration = max(1, min(int(duration_seconds), 2_419_200))
	results: list[dict[str, object]] = []
	for target_id in target_ids:
		row = connection.execute(
			"""SELECT pa.platform,pa.username,m.id,m.observation_id
			   FROM platform_accounts pa
			   JOIN messages m ON m.platform_account_id=pa.id AND m.community_id=?
			   WHERE pa.id=? ORDER BY m.created_at DESC,m.id DESC LIMIT 1""",
			(int(community_id), target_id),
		).fetchone()
		if row is None:
			results.append({"target_platform_account_id": target_id, "status": "not_found"})
			continue
		result: dict[str, object] = {
			"target_platform_account_id": target_id, "platform": str(row[0]),
			"username": str(row[1]), "status": "eligible" if dry_run else "queued",
		}
		if not dry_run:
			try:
				action_id = record_moderation_action(
					connection, platform=str(row[0]), message_id=int(row[2]),
					target_platform_account_id=target_id, action_type=normalized_action,
					reason=normalized_reason, status="pending", actor_type="operator",
					actor_id=int(operator_id), community_id=int(community_id),
				)
				connection.execute(
					"UPDATE moderation_actions SET duration_seconds=?,assigned_operator_id=? WHERE id=?",
					(duration, int(operator_id), action_id),
				)
				enqueue_processing_job(
					connection, stage="action", job_type=f"{row[0]}.moderation.execute",
					observation_id=int(row[3]) if row[3] is not None else None,
					payload={"message_id": int(row[2])},
					idempotency_key=f"bulk:{community_id}:{operator_id}:{normalized_action}:{target_id}:{row[2]}",
					priority=10,
				)
				result["action_id"] = action_id
			except (sqlite3.Error, ValueError) as exc:
				result["status"] = "failed"
				result["error"] = str(exc)
		results.append(result)
	if not dry_run:
		connection.execute(
			"""INSERT INTO audit_log(
			       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
			   ) VALUES ('operator',?,'moderation.bulk_queued','community',?,?)""",
			(int(operator_id), int(community_id), json.dumps({
				"action_type": normalized_action, "reason": normalized_reason,
				"targets": target_ids, "results": results,
			}, sort_keys=True)),
		)
	return {
		"dry_run": bool(dry_run), "action_type": normalized_action,
		"requested": len(target_ids), "results": results,
	}


def resolve_review(
	connection: sqlite3.Connection,
	review_id: int,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	resolution: str,
	note: str = "",
	action_type: str | None = None,
	duration_seconds: int = 600,
) -> int | None:
	operator_id = _require_operator(actor)
	community_id = tenant.community_id
	normalized_resolution = resolution.strip().casefold()
	normalized_action = action_type.strip().casefold() if action_type else None
	if normalized_resolution not in {"dismissed", "confirmed", "escalated"}:
		raise ValueError("invalid review resolution")
	if normalized_action not in {None, "warn", "timeout", "ban"}:
		raise ValueError("invalid review action")
	row = connection.execute(
		"""SELECT q.status,q.message_id,m.platform,m.observation_id,m.platform_account_id
		   FROM review_queue q JOIN messages m ON m.id=q.message_id
		   WHERE q.id=? AND m.community_id=?""",
		(review_id, int(community_id)),
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
			community_id=community_id,
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
		(operator_id, review_id, json.dumps({"community_id": int(community_id), "resolution": normalized_resolution, "action": normalized_action,
			"action_id": action_id, "note": note.strip()}, sort_keys=True)),
	)
	return action_id


def _require_operator(actor: ActorAttribution) -> int:
	if actor.actor_type != "operator" or actor.actor_id is None:
		raise PermissionError("moderation changes require an operator actor")
	return int(actor.actor_id)


def _audit_moderation_lifecycle(
	connection: sqlite3.Connection,
	*,
	operator_id: int,
	action_type: str,
	entity_type: str,
	entity_id: int,
	payload: Mapping[str, object],
) -> None:
	connection.execute(
		"""INSERT INTO audit_log(
		       actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
		   VALUES ('operator',?,?,?,?,?)""",
		(
			operator_id, action_type, entity_type, entity_id,
			json.dumps(dict(payload), sort_keys=True),
		),
	)


def _normalize_rule_config(config: Mapping[str, object]) -> dict[str, object]:
	name = str(config.get("name", "")).strip()
	rule_type = str(config.get("rule_type", "")).strip().casefold()
	pattern = str(config.get("pattern", "")).strip()
	severity = str(config.get("severity", "")).strip().casefold()
	action = str(config.get("auto_enforce_action", "")).strip().casefold() or None
	platforms = list(dict.fromkeys(
		str(value).strip().casefold() for value in config.get("platform_scope", ())
		if str(value).strip()
	))
	if not name or not pattern:
		raise ValueError("moderation rule name and pattern are required")
	if rule_type not in {
		"exact_term", "banned_phrase", "streamboo_viewer_spam", "link_restriction",
		"duplicate_message", "egregious_term",
	}:
		raise ValueError("unsupported moderation rule type")
	if severity not in {"low", "medium", "high", "critical"}:
		raise ValueError("unsupported moderation severity")
	if action not in {None, "warn", "timeout", "ban"}:
		raise ValueError("unsupported moderation action")
	if not platforms or not set(platforms).issubset({"discord", "twitch"}):
		raise ValueError("platform_scope must contain discord or twitch")
	return {
		"name": name,
		"rule_type": rule_type,
		"pattern": pattern,
		"severity": severity,
		"auto_enforce_action": action,
		"action_duration_seconds": max(
			1, min(int(config.get("action_duration_seconds", 600)), 2_419_200)
		),
		"platform_scope": platforms,
	}


def create_moderation_rule_draft(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	config: Mapping[str, object],
) -> int:
	operator_id = _require_operator(actor)
	normalized = _normalize_rule_config(config)
	row = connection.execute(
		"SELECT id FROM moderation_rules WHERE community_id=? AND name=?",
		(tenant.community_id, normalized["name"]),
	).fetchone()
	if row is None:
		rule_id = upsert_moderation_rule(
			connection,
			community_id=tenant.community_id,
			name=str(normalized["name"]),
			rule_type=str(normalized["rule_type"]),
			pattern=str(normalized["pattern"]),
			severity=str(normalized["severity"]),
			auto_enforce_action=normalized["auto_enforce_action"],
			enabled=False,
			enforcement_mode="disabled",
			action_duration_seconds=int(normalized["action_duration_seconds"]),
			platform_scope=tuple(normalized["platform_scope"]),
		)
	else:
		rule_id = int(row[0])
	next_version = int(connection.execute(
		"""SELECT COALESCE(MAX(version_number),0)+1 FROM moderation_rule_versions
		   WHERE moderation_rule_id=?""",
		(rule_id,),
	).fetchone()[0])
	with connection:
		cursor = connection.execute(
			"""INSERT INTO moderation_rule_versions(
			       community_id,moderation_rule_id,version_number,lifecycle_state,
			       config_json,created_by_operator_id)
			   VALUES (?,?,?,'draft',?,?)""",
			(
				tenant.community_id,
				rule_id,
				next_version,
				json.dumps(normalized, sort_keys=True),
				operator_id,
			),
		)
		version_id = int(cursor.lastrowid)
		_audit_moderation_lifecycle(
			connection,
			operator_id=operator_id,
			action_type="moderation.rule_drafted",
			entity_type="moderation_rule_version",
			entity_id=version_id,
			payload={"community_id": tenant.community_id, "rule_id": rule_id},
		)
	return version_id


def _rule_version(
	connection: sqlite3.Connection, tenant: TenantContext, version_id: int,
) -> sqlite3.Row:
	row = connection.execute(
		"""SELECT * FROM moderation_rule_versions
		   WHERE id=? AND community_id=?""",
		(int(version_id), tenant.community_id),
	).fetchone()
	if row is None:
		raise LookupError("moderation rule version not found")
	return row


def _sample_matches(config: Mapping[str, object], sample: str) -> bool:
	pattern = str(config["pattern"]).casefold()
	text = sample.casefold()
	return pattern in text


def preview_moderation_rule_version(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	version_id: int,
	samples: list[str],
) -> dict[str, object]:
	row = _rule_version(connection, tenant, version_id)
	config = json.loads(str(row["config_json"]))
	matched_indexes = [
		index for index, sample in enumerate(samples) if _sample_matches(config, str(sample))
	]
	impact = {
		"sample_count": len(samples),
		"match_count": len(matched_indexes),
		"matched_indexes": matched_indexes,
	}
	with connection:
		connection.execute(
			"UPDATE moderation_rule_versions SET impact_json=? WHERE id=?",
			(json.dumps(impact, sort_keys=True), int(version_id)),
		)
	return impact


def _apply_rule_config(
	connection: sqlite3.Connection,
	*,
	rule_id: int,
	config: Mapping[str, object],
	lifecycle_state: str,
) -> None:
	connection.execute(
		"""UPDATE moderation_rules SET name=?,rule_type=?,pattern=?,severity=?,
		   auto_enforce_action=?,action_duration_seconds=?,platform_scope_json=?,
		   enforcement_mode=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
		(
			config["name"], config["rule_type"], config["pattern"], config["severity"],
			config.get("auto_enforce_action"), config["action_duration_seconds"],
			json.dumps(config["platform_scope"]), lifecycle_state,
			int(lifecycle_state != "disabled"), rule_id,
		),
	)


def publish_moderation_rule_version(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	version_id: int,
	lifecycle_state: str,
) -> int:
	operator_id = _require_operator(actor)
	state = lifecycle_state.strip().casefold()
	if state not in {"shadow", "enforce"}:
		raise ValueError("rule lifecycle state must be shadow or enforce")
	row = _rule_version(connection, tenant, version_id)
	if state == "enforce" and int(row["created_by_operator_id"]) == operator_id:
		raise PermissionError("enforced rules require approval by a different operator")
	config = json.loads(str(row["config_json"]))
	with connection:
		_apply_rule_config(
			connection,
			rule_id=int(row["moderation_rule_id"]),
			config=config,
			lifecycle_state=state,
		)
		connection.execute(
			"""UPDATE moderation_rule_versions SET lifecycle_state=?,
			   approved_by_operator_id=?,approved_at=CURRENT_TIMESTAMP WHERE id=?""",
			(state, operator_id, int(version_id)),
		)
		_audit_moderation_lifecycle(
			connection,
			operator_id=operator_id,
			action_type="moderation.rule_published",
			entity_type="moderation_rule_version",
			entity_id=int(version_id),
			payload={"community_id": tenant.community_id, "lifecycle_state": state},
		)
	return int(row["moderation_rule_id"])


def rollback_moderation_rule(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	version_id: int,
) -> int:
	operator_id = _require_operator(actor)
	source = _rule_version(connection, tenant, version_id)
	config = json.loads(str(source["config_json"]))
	rule_id = int(source["moderation_rule_id"])
	next_version = int(connection.execute(
		"""SELECT COALESCE(MAX(version_number),0)+1 FROM moderation_rule_versions
		   WHERE moderation_rule_id=?""",
		(rule_id,),
	).fetchone()[0])
	state = str(source["lifecycle_state"])
	if state not in {"shadow", "enforce"}:
		state = "shadow"
	with connection:
		_apply_rule_config(
			connection, rule_id=rule_id, config=config, lifecycle_state=state,
		)
		cursor = connection.execute(
			"""INSERT INTO moderation_rule_versions(
			       community_id,moderation_rule_id,version_number,lifecycle_state,
			       config_json,impact_json,created_by_operator_id,
			       approved_by_operator_id,approved_at)
			   VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
			(
				tenant.community_id, rule_id, next_version, state,
				json.dumps(config, sort_keys=True),
				json.dumps({"rollback_of": int(version_id)}, sort_keys=True),
				operator_id, operator_id,
			),
		)
		rollback_id = int(cursor.lastrowid)
		_audit_moderation_lifecycle(
			connection,
			operator_id=operator_id,
			action_type="moderation.rule_rolled_back",
			entity_type="moderation_rule_version",
			entity_id=rollback_id,
			payload={"community_id": tenant.community_id, "rollback_of": int(version_id)},
		)
	return rollback_id


def add_moderation_rule_exemption(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	rule_id: int,
	exemption_type: str,
	exemption_value: str,
	reason: str,
) -> int:
	operator_id = _require_operator(actor)
	normalized_type = exemption_type.strip().casefold()
	normalized_value = exemption_value.strip()
	if normalized_type not in {"channel", "platform_account"}:
		raise ValueError("invalid moderation rule exemption type")
	if not normalized_value or not reason.strip():
		raise ValueError("moderation rule exemption value and reason are required")
	if connection.execute(
		"SELECT 1 FROM moderation_rules WHERE id=? AND community_id=?",
		(int(rule_id), tenant.community_id),
	).fetchone() is None:
		raise LookupError("moderation rule not found")
	with connection:
		cursor = connection.execute(
			"""INSERT INTO moderation_rule_exemptions(
			       community_id,moderation_rule_id,exemption_type,exemption_value,
			       reason,created_by_operator_id)
			   VALUES (?,?,?,?,?,?)""",
			(
				tenant.community_id, int(rule_id), normalized_type,
				normalized_value, reason.strip(), operator_id,
			),
		)
		exemption_id = int(cursor.lastrowid)
		_audit_moderation_lifecycle(
			connection,
			operator_id=operator_id,
			action_type="moderation.rule_exemption_added",
			entity_type="moderation_rule_exemption",
			entity_id=exemption_id,
			payload={"community_id": tenant.community_id, "rule_id": int(rule_id)},
		)
	return exemption_id


def save_moderation_filter(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	name: str,
	filters: Mapping[str, object],
) -> int:
	operator_id = _require_operator(actor)
	cleaned_name = name.strip()
	if not cleaned_name:
		raise ValueError("saved filter name is required")
	with connection:
		connection.execute(
			"""INSERT INTO moderation_saved_filters(
			       community_id,operator_id,name,filters_json)
			   VALUES (?,?,?,?) ON CONFLICT(community_id,operator_id,name)
			   DO UPDATE SET filters_json=excluded.filters_json,
			                 updated_at=CURRENT_TIMESTAMP""",
			(
				tenant.community_id, operator_id, cleaned_name,
				json.dumps(dict(filters), sort_keys=True),
			),
		)
		row = connection.execute(
			"""SELECT id FROM moderation_saved_filters
			   WHERE community_id=? AND operator_id=? AND name=?""",
			(tenant.community_id, operator_id, cleaned_name),
		).fetchone()
	return int(row[0])


def list_moderation_filters(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
) -> list[dict[str, object]]:
	operator_id = _require_operator(actor)
	rows = connection.execute(
		"""SELECT id,name,filters_json FROM moderation_saved_filters
		   WHERE community_id=? AND operator_id=? ORDER BY name""",
		(tenant.community_id, operator_id),
	).fetchall()
	return [
		{"id": int(row[0]), "name": str(row[1]), "filters": json.loads(str(row[2]))}
		for row in rows
	]


def _sla_age_hours(created_at: str) -> float:
	try:
		created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
		if created.tzinfo is None:
			created = created.replace(tzinfo=timezone.utc)
		return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)
	except ValueError:
		return 0.0


def list_moderation_work(
	connection: sqlite3.Connection,
	*,
	community_id: int,
	operator_id: int,
	queue: str = "all",
	search: str = "",
	severity: str = "",
	rule: str = "",
	platform: str = "",
	start_at: str = "",
	end_at: str = "",
	assignment: str = "",
	limit: int = 25,
	offset: int = 0,
) -> tuple[list[ModerationWorkItem], int]:
	review_rows = connection.execute(
		"""SELECT q.id,m.platform,pa.username,q.severity,q.queue_reason_code,
		          m.content_raw,q.assigned_operator_id,q.status,q.resolution,q.created_at
		   FROM review_queue q
		   JOIN messages m ON m.id=q.message_id
		   JOIN platform_accounts pa ON pa.id=m.platform_account_id
		   WHERE m.community_id=?""",
		(int(community_id),),
	).fetchall()
	appeal_rows = connection.execute(
		"""SELECT a.id,ma.platform,pa.username,a.severity,a.reason,ma.action_type,
		          a.assigned_operator_id,a.status,a.disposition,a.created_at
		   FROM member_appeals a
		   JOIN moderation_actions ma ON ma.id=a.moderation_action_id
		   JOIN platform_accounts pa ON pa.id=a.appellant_platform_account_id
		   WHERE a.community_id=?""",
		(int(community_id),),
	).fetchall()
	report_rows = connection.execute(
		"""SELECT r.id,pa.platform,pa.username,r.severity,r.category,r.summary,
		          r.assigned_operator_id,r.status,r.resolution,r.created_at
		   FROM member_reports r
		   JOIN platform_accounts pa ON pa.id=r.subject_platform_account_id
		   WHERE r.community_id=?""",
		(int(community_id),),
	).fetchall()
	items = [
		ModerationWorkItem(
			work_type="review", item_id=int(row[0]), platform=str(row[1]),
			username=str(row[2]), severity=str(row[3]), reason=str(row[4]),
			summary=str(row[5]),
			assigned_operator_id=int(row[6]) if row[6] is not None else None,
			status=str(row[7]), created_at=str(row[9]), sla_age_hours=_sla_age_hours(str(row[9])),
		)
		for row in review_rows
	] + [
		ModerationWorkItem(
			work_type="appeal", item_id=int(row[0]), platform=str(row[1]),
			username=str(row[2]), severity=str(row[3]), reason=str(row[4]),
			summary=str(row[5]),
			assigned_operator_id=int(row[6]) if row[6] is not None else None,
			status=str(row[7]), created_at=str(row[9]), sla_age_hours=_sla_age_hours(str(row[9])),
		)
		for row in appeal_rows
	] + [
		ModerationWorkItem(
			work_type="report", item_id=int(row[0]), platform=str(row[1]),
			username=str(row[2]), severity=str(row[3]), reason=str(row[4]),
			summary=str(row[5]),
			assigned_operator_id=int(row[6]) if row[6] is not None else None,
			status=str(row[7]), created_at=str(row[9]), sla_age_hours=_sla_age_hours(str(row[9])),
		)
		for row in report_rows
	]
	normalized_queue = queue.strip().casefold()
	if normalized_queue not in {"all", "unassigned", "mine", "escalated", "appeals", "resolved"}:
		raise ValueError("invalid moderation work queue")
	if normalized_queue == "unassigned":
		items = [item for item in items if item.status == "open" and item.assigned_operator_id is None]
	elif normalized_queue == "mine":
		items = [item for item in items if item.status == "open" and item.assigned_operator_id == int(operator_id)]
	elif normalized_queue == "escalated":
		escalated_ids = {int(row[0]) for row in review_rows if str(row[8]) == "escalated"}
		items = [item for item in items if item.work_type == "review" and item.item_id in escalated_ids]
	elif normalized_queue == "appeals":
		items = [item for item in items if item.work_type == "appeal" and item.status == "open"]
	elif normalized_queue == "resolved":
		items = [item for item in items if item.status != "open"]
	search_text = search.strip().casefold()
	severity_text = severity.strip().casefold()
	rule_text = rule.strip().casefold()
	platform_text = platform.strip().casefold()
	if search_text:
		items = [
			item for item in items
			if search_text in " ".join((item.username, item.reason, item.summary)).casefold()
		]
	if severity_text:
		items = [item for item in items if item.severity.casefold() == severity_text]
	if rule_text:
		items = [item for item in items if rule_text in item.reason.casefold()]
	if platform_text:
		items = [item for item in items if item.platform.casefold() == platform_text]
	if start_at:
		items = [item for item in items if item.created_at >= start_at]
	if end_at:
		items = [item for item in items if item.created_at <= end_at]
	if assignment == "unassigned":
		items = [item for item in items if item.assigned_operator_id is None]
	elif assignment == "mine":
		items = [item for item in items if item.assigned_operator_id == int(operator_id)]
	items.sort(key=lambda item: (item.created_at, item.item_id), reverse=True)
	total = len(items)
	start = max(0, int(offset))
	return items[start:start + max(1, min(int(limit), 100))], total


def assign_moderation_work(
	connection: sqlite3.Connection,
	*,
	tenant: TenantContext,
	actor: ActorAttribution,
	work_type: str,
	item_id: int,
) -> None:
	operator_id = _require_operator(actor)
	normalized_type = work_type.strip().casefold()
	with connection:
		if normalized_type == "review":
			cursor = connection.execute(
				"""UPDATE review_queue SET assigned_operator_id=?
				   WHERE id=? AND status='open' AND EXISTS(
				       SELECT 1 FROM messages
				       WHERE messages.id=review_queue.message_id AND messages.community_id=?)""",
				(operator_id, int(item_id), tenant.community_id),
			)
		elif normalized_type == "appeal":
			cursor = connection.execute(
				"""UPDATE member_appeals SET assigned_operator_id=?
				   WHERE id=? AND community_id=? AND status='open'""",
				(operator_id, int(item_id), tenant.community_id),
			)
		elif normalized_type == "report":
			cursor = connection.execute(
				"""UPDATE member_reports SET assigned_operator_id=?
				   WHERE id=? AND community_id=? AND status='open'""",
				(operator_id, int(item_id), tenant.community_id),
			)
		else:
			raise ValueError("invalid moderation work type")
		if cursor.rowcount != 1:
			raise LookupError("moderation work item not found")
