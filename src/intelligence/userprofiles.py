from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .powerusers import (
	POWERUSER_THRESHOLD,
	SOCIAL_SCORE_DEFAULT,
	default_social_score_for_name,
	record_reputation_evidence,
)
from .scoring import calculate_social_score, score_band
from .signals import refresh_user_derived_signals


@dataclass(frozen=True)
class LinkedAccount:
	platform: str
	platform_user_id: str
	username: str
	guild_or_channel_context: str | None


@dataclass(frozen=True)
class CanonicalUserProfile:
	user_id: int
	primary_display_name: str
	current_reputation_score: int
	candidate_flag: bool
	score_confidence: float
	score_band: str
	score_model_version: int | None
	linked_accounts: tuple[LinkedAccount, ...]
	notes: tuple[sqlite3.Row, ...] = ()


def create_canonical_user(
	connection: sqlite3.Connection,
	*,
	primary_display_name: str,
	current_reputation_score: int = SOCIAL_SCORE_DEFAULT,
	candidate_flag: bool = False,
) -> int:
	name = primary_display_name.strip()
	if not name:
		raise ValueError("primary_display_name must not be empty")

	effective_score = current_reputation_score
	if current_reputation_score == SOCIAL_SCORE_DEFAULT:
		effective_score = default_social_score_for_name(name)
	effective_candidate_flag = candidate_flag or effective_score >= POWERUSER_THRESHOLD

	with connection:
		connection.execute(
			"""
			INSERT INTO users (
				primary_display_name,
				current_reputation_score,
				candidate_flag
			) VALUES (?, ?, ?)
			""",
			(name, effective_score, int(effective_candidate_flag)),
		)
		row = connection.execute(
			"SELECT id FROM users WHERE rowid = last_insert_rowid()"
		).fetchone()
		if row is not None and effective_score != SOCIAL_SCORE_DEFAULT:
			record_reputation_evidence(
				connection,
				user_id=int(row[0]),
				delta=effective_score - SOCIAL_SCORE_DEFAULT,
				reason_code="initial_score_calibration",
				source_type="initial_calibration",
			)

	if row is None:
		raise sqlite3.IntegrityError("Failed to resolve canonical user after insert")

	return int(row[0])


def link_platform_account(
	connection: sqlite3.Connection,
	*,
	platform: str,
	platform_user_id: str,
	user_id: int,
	operator_id: int | None = None,
) -> None:
	account = connection.execute(
		"""
	SELECT id, user_id, username, detached_from_user_id
		FROM platform_accounts
		WHERE platform = ? AND platform_user_id = ?
		""",
		(platform, platform_user_id),
	).fetchone()
	if account is None:
		raise ValueError("platform account not found")

	user = connection.execute(
		"SELECT id, primary_display_name, current_reputation_score FROM users WHERE id = ?",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	existing_user_id = account[1]
	detached_from_user_id = account[3]
	merged_from_user_id: int | None = None
	if existing_user_id is not None and int(existing_user_id) != user_id:
		source_user = connection.execute(
			"SELECT id, primary_display_name, current_reputation_score FROM users WHERE id = ?",
			(int(existing_user_id),),
		).fetchone()
		if source_user is None:
			raise ValueError("linked source user not found")
		merged_from_user_id = int(source_user[0])

	with connection:
		if merged_from_user_id is not None:
			_merge_canonical_users(
				connection,
				source_user_id=merged_from_user_id,
				target_user_id=user_id,
			)
		else:
			connection.execute(
				"""
				UPDATE platform_accounts
				SET user_id = ?, detached_from_user_id = NULL, updated_at = CURRENT_TIMESTAMP
				WHERE id = ?
				""",
				(user_id, account[0]),
			)
			if detached_from_user_id is not None and int(detached_from_user_id) != user_id:
				_reattribute_detached_account_history(
					connection,
					platform_account_id=int(account[0]),
					source_user_id=int(detached_from_user_id),
					target_user_id=user_id,
				)
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type,
				actor_id,
				action_type,
				entity_type,
				entity_id,
				payload_json
			) VALUES (
				'system',
				?,
				'user_account_link',
				'platform_account',
				?,
				json_object('platform', ?, 'platform_user_id', ?, 'user_id', ?)
			)
			""",
			(operator_id, account[0], platform, platform_user_id, user_id),
		)
		refresh_user_derived_signals(connection, user_id)
		calculate_social_score(connection, user_id)
		if (
			detached_from_user_id is not None
			and int(detached_from_user_id) != user_id
			and connection.execute("SELECT 1 FROM users WHERE id = ?", (int(detached_from_user_id),)).fetchone()
		):
			refresh_user_derived_signals(connection, int(detached_from_user_id))
			calculate_social_score(connection, int(detached_from_user_id))


def unlink_platform_account(
	connection: sqlite3.Connection,
	*,
	platform: str,
	platform_user_id: str,
	operator_id: int | None = None,
) -> None:
	account = connection.execute(
		"""
		SELECT id, user_id
		FROM platform_accounts
		WHERE platform = ? AND platform_user_id = ?
		""",
		(platform, platform_user_id),
	).fetchone()
	if account is None:
		raise ValueError("platform account not found")

	with connection:
		connection.execute(
			"""
			UPDATE platform_accounts
			SET user_id = NULL,
			    detached_from_user_id = ?,
			    updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(account[1], account[0]),
		)
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type,
				actor_id,
				action_type,
				entity_type,
				entity_id,
				payload_json
			) VALUES (
				'system',
				?,
				'user_account_unlink',
				'platform_account',
				?,
				json_object('platform', ?, 'platform_user_id', ?, 'user_id', ?)
			)
			""",
			(operator_id, account[0], platform, platform_user_id, account[1]),
		)
		if account[1] is not None:
			refresh_user_derived_signals(connection, int(account[1]))
			calculate_social_score(connection, int(account[1]))


def _reattribute_detached_account_history(
	connection: sqlite3.Connection,
	*,
	platform_account_id: int,
	source_user_id: int,
	target_user_id: int,
) -> None:
	connection.execute(
		"UPDATE messages SET user_id = ? WHERE platform_account_id = ? AND user_id = ?",
		(target_user_id, platform_account_id, source_user_id),
	)
	connection.execute(
		"""
		UPDATE reputation_events
		SET user_id = ?
		WHERE user_id = ?
		  AND source_type IN ('message', 'moderation')
		  AND source_id IN (
			SELECT id FROM messages WHERE platform_account_id = ?
		  )
		""",
		(target_user_id, source_user_id, platform_account_id),
	)
	connection.execute(
		"UPDATE moderation_actions SET user_id = ? WHERE target_platform_account_id = ? AND user_id = ?",
		(target_user_id, platform_account_id, source_user_id),
	)


def _merge_canonical_users(
	connection: sqlite3.Connection,
	*,
	source_user_id: int,
	target_user_id: int,
) -> None:
	if source_user_id == target_user_id:
		return

	connection.execute(
		"UPDATE platform_accounts SET user_id = ?, detached_from_user_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute(
		"UPDATE platform_accounts SET detached_from_user_id = ? WHERE detached_from_user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute("UPDATE messages SET user_id = ? WHERE user_id = ?", (target_user_id, source_user_id))
	connection.execute(
		"UPDATE moderation_actions SET user_id = ? WHERE user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute(
		"UPDATE reputation_events SET user_id = ? WHERE user_id = ? AND source_type != 'initial_calibration'",
		(target_user_id, source_user_id),
	)
	connection.execute("UPDATE user_notes SET user_id = ? WHERE user_id = ?", (target_user_id, source_user_id))
	connection.execute(
		"UPDATE server_boost_requests SET requester_user_id = ? WHERE requester_user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute(
		"UPDATE intelligence_alerts SET user_id = ? WHERE user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute(
		"UPDATE intelligence_reports SET subject_user_id = ? WHERE subject_user_id = ?",
		(target_user_id, source_user_id),
	)
	connection.execute(
		"""
		INSERT OR IGNORE INTO case_entities (case_id, user_id, role, added_at)
		SELECT case_id, ?, role, added_at FROM case_entities WHERE user_id = ?
		""",
		(target_user_id, source_user_id),
	)
	connection.execute("DELETE FROM case_entities WHERE user_id = ?", (source_user_id,))

	relationships = connection.execute(
		"""
		SELECT source_user_id, target_user_id, relationship_type, context_key,
		       strength, evidence_count, first_observed_at, last_observed_at, evidence_json
		FROM entity_relationships
		WHERE source_user_id = ? OR target_user_id = ?
		""",
		(source_user_id, source_user_id),
	).fetchall()
	connection.execute(
		"DELETE FROM entity_relationships WHERE source_user_id = ? OR target_user_id = ?",
		(source_user_id, source_user_id),
	)
	for relationship in relationships:
		source = target_user_id if int(relationship[0]) == source_user_id else int(relationship[0])
		target = target_user_id if int(relationship[1]) == source_user_id else int(relationship[1])
		if source == target:
			continue
		source, target = sorted((source, target))
		connection.execute(
			"""
			INSERT INTO entity_relationships (
				source_user_id, target_user_id, relationship_type, context_key,
				strength, evidence_count, first_observed_at, last_observed_at, evidence_json
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(source_user_id, target_user_id, relationship_type, context_key)
			DO UPDATE SET
				strength = MAX(entity_relationships.strength, excluded.strength),
				evidence_count = entity_relationships.evidence_count + excluded.evidence_count,
				first_observed_at = MIN(entity_relationships.first_observed_at, excluded.first_observed_at),
				last_observed_at = MAX(entity_relationships.last_observed_at, excluded.last_observed_at)
			""",
			(source, target, *relationship[2:]),
		)

	connection.execute(
		"""
		INSERT INTO audit_log (
			actor_type, actor_id, action_type, entity_type, entity_id, payload_json
		) VALUES ('system', NULL, 'user_merge', 'user', ?, json_object('source_user_id', ?))
		""",
		(target_user_id, source_user_id),
	)
	connection.execute("DELETE FROM users WHERE id = ?", (source_user_id,))


def get_canonical_user_profile(
	connection: sqlite3.Connection,
	user_id: int,
) -> CanonicalUserProfile:
	user = connection.execute(
		"""
		SELECT id, primary_display_name, current_reputation_score, candidate_flag,
		       score_confidence, score_model_version
		FROM users
		WHERE id = ?
		""",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	accounts = connection.execute(
		"""
		SELECT platform, platform_user_id, username, guild_or_channel_context
		FROM platform_accounts
		WHERE user_id = ?
		ORDER BY platform, platform_user_id
		""",
		(user_id,),
	).fetchall()
	notes = connection.execute(
		"""
		SELECT id, operator_id, body, created_at
		FROM user_notes
		WHERE user_id = ?
		ORDER BY created_at, id
		""",
		(user_id,),
	).fetchall()

	return CanonicalUserProfile(
		user_id=int(user[0]),
		primary_display_name=str(user[1]),
		current_reputation_score=int(user[2]),
		candidate_flag=bool(user[3]),
		score_confidence=float(user[4] or 0.0),
		score_band=score_band(int(user[2])),
		score_model_version=int(user[5]) if user[5] is not None else None,
		linked_accounts=tuple(
			LinkedAccount(
				platform=str(account[0]),
				platform_user_id=str(account[1]),
				username=str(account[2]),
				guild_or_channel_context=str(account[3]) if account[3] is not None else None,
			)
			for account in accounts
		),
		notes=tuple(notes),
	)


def get_canonical_user_profile_for_platform_account(
	connection: sqlite3.Connection,
	*,
	platform: str,
	platform_user_id: str,
) -> CanonicalUserProfile | None:
	row = connection.execute(
		"""
		SELECT users.id
		FROM users
		INNER JOIN platform_accounts ON platform_accounts.user_id = users.id
		WHERE platform_accounts.platform = ?
		  AND platform_accounts.platform_user_id = ?
		""",
		(platform, platform_user_id),
	).fetchone()
	if row is None:
		return None

	return get_canonical_user_profile(connection, int(row[0]))


def add_user_note(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	operator_id: int,
	body: str,
) -> int:
	clean_body = body.strip()
	if not clean_body:
		raise ValueError("body must not be empty")

	user = connection.execute(
		"SELECT id FROM users WHERE id = ?",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	with connection:
		connection.execute(
			"""
			INSERT INTO user_notes (
				user_id,
				operator_id,
				body
			) VALUES (?, ?, ?)
			""",
			(user_id, operator_id, clean_body),
		)
		row = connection.execute(
			"SELECT id FROM user_notes WHERE rowid = last_insert_rowid()"
		).fetchone()
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type,
				actor_id,
				action_type,
				entity_type,
				entity_id,
				payload_json
			) VALUES (
				'operator',
				?,
				'user_note_create',
				'user',
				?,
				json_object('body', ?)
			)
			""",
			(operator_id, user_id, clean_body),
		)

	if row is None:
		raise sqlite3.IntegrityError("Failed to resolve user note after insert")

	return int(row[0])


def list_user_notes(
	connection: sqlite3.Connection,
	user_id: int,
) -> list[sqlite3.Row]:
	rows = connection.execute(
		"""
		SELECT id, operator_id, body, created_at
		FROM user_notes
		WHERE user_id = ?
		ORDER BY created_at, id
		""",
		(user_id,),
	).fetchall()
	return list(rows)
