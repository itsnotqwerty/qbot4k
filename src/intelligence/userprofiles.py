from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .powerusers import (
	POWERUSER_THRESHOLD,
	SOCIAL_SCORE_DEFAULT,
	average_social_scores,
	default_social_score_for_name,
	enforced_social_score_for_name,
)


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
		SELECT id, user_id, username
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
	merged_from_user_id: int | None = None
	merged_score: int | None = None
	if existing_user_id is not None and int(existing_user_id) != user_id:
		source_user = connection.execute(
			"SELECT id, primary_display_name, current_reputation_score FROM users WHERE id = ?",
			(int(existing_user_id),),
		).fetchone()
		if source_user is None:
			raise ValueError("linked source user not found")
		merged_from_user_id = int(source_user[0])
		merged_score = average_social_scores(int(user[2]), int(source_user[2]))
		merged_score = enforced_social_score_for_name(str(user[1]), merged_score)

	with connection:
		if merged_score is not None:
			connection.execute(
				"""
				UPDATE users
				SET current_reputation_score = ?,
				    candidate_flag = ?,
				    updated_at = CURRENT_TIMESTAMP
				WHERE id = ?
				""",
				(merged_score, int(merged_score >= POWERUSER_THRESHOLD), user_id),
			)
			connection.execute(
				"""
				INSERT INTO reputation_events (
					user_id,
					source_type,
					source_id,
					delta,
					reason_code
				) VALUES (?, 'account_link_merge', ?, ?, 'account_link_average')
				""",
				(user_id, merged_from_user_id, merged_score - int(user[2])),
			)
		connection.execute(
			"""
			UPDATE platform_accounts
			SET user_id = ?, updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(user_id, account[0]),
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
			SET user_id = NULL, updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(account[0],),
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
				json_object('platform', ?, 'platform_user_id', ?)
			)
			""",
			(operator_id, account[0], platform, platform_user_id),
		)


def get_canonical_user_profile(
	connection: sqlite3.Connection,
	user_id: int,
) -> CanonicalUserProfile:
	user = connection.execute(
		"""
		SELECT id, primary_display_name, current_reputation_score, candidate_flag
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

