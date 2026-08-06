from __future__ import annotations

import sqlite3
from dataclasses import dataclass


SOCIAL_SCORE_MIN = 350
SOCIAL_SCORE_MAX = 900
SOCIAL_SCORE_DEFAULT = 500
POWERUSER_THRESHOLD = 700

_MAX_SCORE_DEFAULT_HANDLES = {
	"apollyon",
	"its_not_qwerty",
}

_FIXED_SOCIAL_SCORE_BY_NAME = {
	"apollyon": SOCIAL_SCORE_MAX,
	"its_not_qwerty": SOCIAL_SCORE_MAX,
}

_POSITIVE_TERMS = {
	"thanks",
	"thank you",
	"great",
	"awesome",
	"nice",
	"love",
	"good job",
	"well done",
}

_VERY_NEGATIVE_TERMS = {
	"idiot",
	"hate",
	"trash",
	"stupid",
	"kill",
	"worthless",
	"loser",
}


def clamp_social_score(score: int) -> int:
	return max(SOCIAL_SCORE_MIN, min(SOCIAL_SCORE_MAX, score))


def is_poweruser_score(score: int) -> bool:
	return clamp_social_score(score) >= POWERUSER_THRESHOLD


def average_social_scores(first_score: int, second_score: int) -> int:
	return clamp_social_score(int(round((first_score + second_score) / 2)))


def default_social_score_for_name(display_name: str) -> int:
	normalized = display_name.strip().casefold()
	if normalized in _MAX_SCORE_DEFAULT_HANDLES:
		return SOCIAL_SCORE_MAX
	return SOCIAL_SCORE_DEFAULT


def enforced_social_score_for_name(display_name: str, proposed_score: int) -> int:
	normalized = display_name.strip().casefold()
	if normalized in _FIXED_SOCIAL_SCORE_BY_NAME:
		return int(_FIXED_SOCIAL_SCORE_BY_NAME[normalized])
	return clamp_social_score(proposed_score)


def score_delta_for_message(content_raw: str) -> tuple[int, str] | None:
	normalized = content_raw.casefold().strip()
	if not normalized:
		return None

	for term in _VERY_NEGATIVE_TERMS:
		if term in normalized:
			return (-30, "very_negative_content")

	for term in _POSITIVE_TERMS:
		if term in normalized:
			return (1, "positive_message")

	return None


def score_delta_for_moderation(*, severity: str, action_type: str | None = None) -> tuple[int, str]:
	severity_key = severity.casefold().strip()
	base_delta = {
		"low": -20,
		"medium": -35,
		"high": -55,
	}.get(severity_key, -25)
	if action_type:
		base_delta -= 15
	return (base_delta, "moderation_penalty")


@dataclass(frozen=True)
class ReputationUpdate:
	user_id: int
	delta: int
	current_score: int
	candidate_flag: bool
	reason_code: str


def apply_reputation_event(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	delta: int,
	reason_code: str,
	source_type: str,
	source_id: int | None = None,
	candidate_threshold: int = POWERUSER_THRESHOLD,
	minimum_score: int = SOCIAL_SCORE_MIN,
	maximum_score: int = SOCIAL_SCORE_MAX,
) -> ReputationUpdate:
	user = connection.execute(
		"""
		SELECT id, primary_display_name, current_reputation_score
		FROM users
		WHERE id = ?
		""",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	current_score = int(user[2])
	updated_score = max(minimum_score, min(maximum_score, current_score + delta))
	updated_score = enforced_social_score_for_name(str(user[1]), updated_score)
	candidate_flag = updated_score >= candidate_threshold

	with connection:
		connection.execute(
			"""
			INSERT INTO reputation_events (
				user_id,
				source_type,
				source_id,
				delta,
				reason_code
			) VALUES (?, ?, ?, ?, ?)
			""",
			(user_id, source_type, source_id, delta, reason_code),
		)
		connection.execute(
			"""
			UPDATE users
			SET current_reputation_score = ?,
			    candidate_flag = ?,
			    updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(updated_score, int(candidate_flag), user_id),
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
				NULL,
				'user_reputation_update',
				'user',
				?,
				json_object('delta', ?, 'reason_code', ?, 'source_type', ?, 'source_id', ?)
			)
			""",
			(user_id, delta, reason_code, source_type, source_id),
		)

	return ReputationUpdate(
		user_id=user_id,
		delta=delta,
		current_score=updated_score,
		candidate_flag=candidate_flag,
		reason_code=reason_code,
	)


def get_reputation_history(
	connection: sqlite3.Connection,
	user_id: int,
) -> list[sqlite3.Row]:
	rows = connection.execute(
		"""
		SELECT id, source_type, source_id, delta, reason_code, created_at
		FROM reputation_events
		WHERE user_id = ?
		ORDER BY created_at, id
		""",
		(user_id,),
	).fetchall()
	return list(rows)
