from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .powerusers import (
	POWERUSER_THRESHOLD,
	SOCIAL_SCORE_DEFAULT,
	SOCIAL_SCORE_MIN,
	clamp_social_score,
	enforce_score_floor_ban,
)
from .signals import SIGNAL_ANALYZER_VERSION


SOCIAL_SCORE_MODEL_VERSION = 2


@dataclass(frozen=True)
class ScoreComponent:
	key: str
	label: str
	raw_value: float
	normalized_value: float
	weight: float
	contribution: float
	confidence: float
	evidence_count: int
	source: Mapping[str, object]


@dataclass(frozen=True)
class SocialScoreResult:
	user_id: int
	score: int
	confidence: float
	evidence_count: int
	band: str
	model_version: int
	calculated_at: str
	components: tuple[ScoreComponent, ...]
	run_id: int | None = None


def calculate_social_score(
	connection: sqlite3.Connection,
	user_id: int,
	*,
	trigger_signal_run_id: int | None = None,
	calculated_at: str | None = None,
	persist: bool = True,
) -> SocialScoreResult:
	"""Calculate a reproducible score from current signals and adjudicated intelligence.

	The materialized value on ``users`` is an output cache. It is deliberately never
	read as an input, which prevents a reputation/risk feedback loop.
	"""
	user = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	signals = _current_signals(connection, user_id)
	components: list[ScoreComponent] = []

	message_count = _value(signals, "activity.message_count")
	eligible_message_count = _value(signals, "activity.eligible_message_count")
	behavior_confidence = _confidence(signals, "activity.message_count")
	components.append(_component(
		"activity.depth", "Sustained participation", eligible_message_count,
		min(1.0, eligible_message_count / 100.0), 100.0,
		confidence=behavior_confidence,
		evidence_count=_evidence(signals, "activity.message_count"),
		source={"signal_key": "activity.eligible_message_count", "cap": 100},
		apply_confidence=False,
	))

	channel_count = _value(signals, "activity.active_channel_count")
	components.append(_component(
		"activity.breadth", "Channel breadth", channel_count,
		min(1.0, max(0.0, channel_count - 1.0) / 10.0), 20.0,
		confidence=behavior_confidence,
		evidence_count=_evidence(signals, "activity.active_channel_count"),
		source={"signal_key": "activity.active_channel_count"},
	))

	platform_count = _value(signals, "activity.platform_count")
	components.append(_component(
		"identity.cross_platform", "Cross-platform continuity", platform_count,
		min(1.0, max(0.0, platform_count - 1.0) / 2.0), 30.0,
		confidence=behavior_confidence,
		evidence_count=_evidence(signals, "activity.platform_count"),
		source={"signal_key": "activity.platform_count"},
	))

	positive_ratio = _value(signals, "behavior.positive_message_ratio")
	components.append(_component(
		"behavior.constructive", "Constructive behavior", positive_ratio,
		_clip01(positive_ratio), 5.0,
		confidence=_confidence(signals, "behavior.positive_message_ratio"),
		evidence_count=_evidence(signals, "behavior.positive_message_ratio"),
		source={"signal_key": "behavior.positive_message_ratio"},
	))

	reply_count = _value(signals, "behavior.reply_to_human_count")
	components.append(_component(
		"behavior.reciprocity", "Human reciprocity", reply_count,
		min(1.0, reply_count / 30.0), 30.0,
		confidence=behavior_confidence,
		evidence_count=int(reply_count),
		source={"signal_key": "behavior.reply_to_human_count", "cap": 30},
		apply_confidence=False,
	))

	welcome_count = _value(signals, "behavior.welcome_count")
	components.append(_component(
		"community.welcomes", "Community welcomes", welcome_count,
		min(1.0, welcome_count / 30.0), 30.0,
		confidence=behavior_confidence,
		evidence_count=int(welcome_count),
		source={"signal_key": "behavior.welcome_count", "cap": 30},
		apply_confidence=False,
	))

	duplicate_welcomes = _value(signals, "behavior.welcome_duplicate_count")
	components.append(_component(
		"community.welcome_spam", "Duplicate welcomes", duplicate_welcomes,
		min(1.0, duplicate_welcomes / 20.0), -60.0,
		confidence=behavior_confidence,
		evidence_count=int(duplicate_welcomes),
		source={"signal_key": "behavior.welcome_duplicate_count", "penalty_per_event": 3},
		apply_confidence=False,
	))

	negative_points = _value(signals, "behavior.negative_severity_points")
	components.append(_component(
		"behavior.harmful_content", "Harmful content", negative_points,
		min(1.0, negative_points / 200.0), -200.0,
		confidence=_confidence(signals, "behavior.negative_message_ratio"),
		evidence_count=_evidence(signals, "behavior.negative_message_ratio"),
		source={"signal_key": "behavior.negative_severity_points", "cap": 200},
		apply_confidence=False,
	))

	moderation_points = _value(signals, "moderation.penalty_points")
	components.append(_component(
		"moderation.findings", "Moderation findings", moderation_points,
		min(1.0, moderation_points / 250.0), -250.0,
		confidence=_confidence(signals, "moderation.finding_count"),
		evidence_count=_evidence(signals, "moderation.finding_count"),
		source={"signal_key": "moderation.penalty_points", "cap": 250},
		apply_confidence=False,
	))

	temporal_risk = _temporal_risk(connection, user_id)
	risk = temporal_risk[0] if temporal_risk is not None else _value(signals, "risk.composite")
	risk_confidence = temporal_risk[1] if temporal_risk is not None else _confidence(signals, "risk.composite")
	risk_evidence = temporal_risk[2] if temporal_risk is not None else _evidence(signals, "risk.composite")
	components.append(_component(
		"risk.corroboration", "Corroborated composite risk", risk,
		_clip01(risk / 100.0), -25.0,
		confidence=risk_confidence,
		evidence_count=risk_evidence,
		source={
			"signal_key": "risk.composite",
			"window_blend": temporal_risk[3] if temporal_risk is not None else "lifetime_snapshot",
		},
	))

	velocity = connection.execute(
		"""
		SELECT value_real, confidence, evidence_count
		FROM derived_signal_windows
		WHERE user_id = ? AND signal_key = 'behavior.negative_velocity'
		  AND window_name = '24h_vs_7d' AND analyzer_version = ?
		""",
		(user_id, SIGNAL_ANALYZER_VERSION),
	).fetchone()
	if velocity is not None:
		velocity_value = float(velocity[0])
		components.append(_component(
			"behavior.negative_velocity", "Negative behavior velocity", velocity_value,
			_clip01(max(0.0, velocity_value)), -75.0,
			confidence=float(velocity[1]), evidence_count=int(velocity[2]),
			source={"signal_key": "behavior.negative_velocity", "window": "24h_vs_7d"},
		))

	adjustment = _adjudicated_adjustment(connection, user_id)
	if adjustment[0] != 0.0 or adjustment[1] > 0:
		components.append(ScoreComponent(
			key="intelligence.adjudication",
			label="Adjudicated intelligence",
			raw_value=adjustment[0],
			normalized_value=min(1.0, abs(adjustment[0]) / 200.0),
			weight=adjustment[0],
			contribution=adjustment[0],
			confidence=adjustment[2],
			evidence_count=adjustment[1],
			source={"dispositions": adjustment[3]},
		))

	manual = _manual_adjustment(connection, user_id)
	if manual[0] != 0.0 or manual[1] > 0:
		components.append(ScoreComponent(
			key="operator.adjustment",
			label="Explicit adjustments",
			raw_value=manual[0],
			normalized_value=min(1.0, abs(manual[0]) / 150.0),
			weight=manual[0],
			contribution=manual[0],
			confidence=1.0,
			evidence_count=manual[1],
			source={"source_types": ["initial_calibration", "manual_adjustment", "server_boost"]},
		))

	raw_score = SOCIAL_SCORE_DEFAULT + sum(component.contribution for component in components)
	score = clamp_social_score(int(round(raw_score)))
	evidence_count = int(message_count) + adjustment[1] + manual[1]
	confidence = round(max(
		behavior_confidence,
		adjustment[2] if adjustment[1] else 0.0,
		1.0 if manual[1] else 0.0,
	), 4)
	band = score_band(score)
	timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
	result = SocialScoreResult(
		user_id=user_id,
		score=score,
		confidence=confidence,
		evidence_count=evidence_count,
		band=band,
		model_version=SOCIAL_SCORE_MODEL_VERSION,
		calculated_at=timestamp,
		components=tuple(component for component in components if component.contribution != 0.0),
	)
	if not persist:
		return result
	return _persist_score(connection, result, trigger_signal_run_id=trigger_signal_run_id)


def get_current_social_score(connection: sqlite3.Connection, user_id: int) -> SocialScoreResult | None:
	row = connection.execute(
		"""
		SELECT id, score, confidence, evidence_count, band, model_version, calculated_at
		FROM social_score_runs WHERE user_id = ?
		ORDER BY calculated_at DESC, id DESC LIMIT 1
		""",
		(user_id,),
	).fetchone()
	if row is None:
		return None
	components = tuple(
		ScoreComponent(
			key=str(item[0]), label=str(item[1]), raw_value=float(item[2]),
			normalized_value=float(item[3]), weight=float(item[4]), contribution=float(item[5]),
			confidence=float(item[6]), evidence_count=int(item[7]),
			source=_json_mapping(item[8]),
		)
		for item in connection.execute(
			"""
			SELECT component_key, label, raw_value, normalized_value, weight,
			       contribution, confidence, evidence_count, source_json
			FROM social_score_components WHERE score_run_id = ?
			ORDER BY ABS(contribution) DESC, component_key
			""",
			(int(row[0]),),
		).fetchall()
	)
	return SocialScoreResult(
		user_id=user_id, score=int(row[1]), confidence=float(row[2]),
		evidence_count=int(row[3]), band=str(row[4]), model_version=int(row[5]),
		calculated_at=str(row[6]), components=components, run_id=int(row[0]),
	)


def score_band(score: int) -> str:
	if score <= SOCIAL_SCORE_MIN:
		return "critical"
	if score < 450:
		return "high risk"
	if score < 550:
		return "developing"
	if score < POWERUSER_THRESHOLD:
		return "established"
	return "trusted"


def _persist_score(
	connection: sqlite3.Connection,
	result: SocialScoreResult,
	*,
	trigger_signal_run_id: int | None,
) -> SocialScoreResult:
	previous = connection.execute(
		"SELECT current_reputation_score FROM users WHERE id = ?", (result.user_id,)
	).fetchone()
	previous_score = int(previous[0]) if previous is not None else SOCIAL_SCORE_DEFAULT
	explanation = {
		"baseline": SOCIAL_SCORE_DEFAULT,
		"formula": "baseline + sum(versioned signal and adjudication components)",
		"component_count": len(result.components),
	}
	with connection:
		cursor = connection.execute(
			"""
			INSERT INTO social_score_runs (
				user_id, trigger_signal_run_id, model_version, score, confidence,
				evidence_count, band, explanation_json, calculated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(result.user_id, trigger_signal_run_id, result.model_version, result.score,
			 result.confidence, result.evidence_count, result.band,
			 json.dumps(explanation, sort_keys=True), result.calculated_at),
		)
		run_id = int(cursor.lastrowid)
		for component in result.components:
			connection.execute(
				"""
				INSERT INTO social_score_components (
					score_run_id, component_key, label, raw_value, normalized_value,
					weight, contribution, confidence, evidence_count, source_json
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(run_id, component.key, component.label, component.raw_value,
				 component.normalized_value, component.weight, component.contribution,
				 component.confidence, component.evidence_count,
				 json.dumps(dict(component.source), sort_keys=True)),
			)
		candidate = result.score >= POWERUSER_THRESHOLD and result.confidence >= 0.5
		connection.execute(
			"""
			UPDATE users SET current_reputation_score = ?, candidate_flag = ?,
				score_confidence = ?, score_model_version = ?,
				score_calculated_at = ?, updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(result.score, int(candidate), result.confidence, result.model_version,
			 result.calculated_at, result.user_id),
		)
		if previous_score > SOCIAL_SCORE_MIN and result.score <= SOCIAL_SCORE_MIN:
			enforce_score_floor_ban(
				connection,
				user_id=result.user_id,
				floor_score=SOCIAL_SCORE_MIN,
			)
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type, action_type, entity_type, entity_id, payload_json
			) VALUES ('system', 'social_score.calculated', 'user', ?, ?)
			""",
			(result.user_id, json.dumps({
				"previous_score": previous_score, "score": result.score,
				"confidence": result.confidence, "model_version": result.model_version,
				"score_run_id": run_id,
			}, sort_keys=True)),
		)
	return SocialScoreResult(**{**result.__dict__, "run_id": run_id})


def _current_signals(connection: sqlite3.Connection, user_id: int) -> dict[str, sqlite3.Row]:
	rows = connection.execute(
		"""
		SELECT signal_key, value_real, confidence, evidence_count, value_json
		FROM derived_signals WHERE user_id = ? AND analyzer_version = ?
		""",
		(user_id, SIGNAL_ANALYZER_VERSION),
	).fetchall()
	return {str(row[0]): row for row in rows}


def _adjudicated_adjustment(connection: sqlite3.Connection, user_id: int) -> tuple[float, int, float, dict[str, int]]:
	rows = connection.execute(
		"""
		SELECT severity, disposition, confidence
		FROM intelligence_alerts
		WHERE user_id = ? AND status = 'resolved'
		  AND disposition IN ('confirmed', 'escalated')
		""",
		(user_id,),
	).fetchall()
	severity_weight = {"low": 10.0, "medium": 25.0, "high": 50.0}
	total = 0.0
	counts: dict[str, int] = {}
	max_confidence = 0.0
	for row in rows:
		disposition = str(row[1])
		confidence = _clip01(float(row[2]))
		multiplier = 1.5 if disposition == "escalated" else 1.0
		total -= severity_weight.get(str(row[0]), 20.0) * multiplier * confidence
		counts[disposition] = counts.get(disposition, 0) + 1
		max_confidence = max(max_confidence, confidence)
	return max(-200.0, total), len(rows), max_confidence, counts


def _temporal_risk(connection: sqlite3.Connection, user_id: int) -> tuple[float, float, int, dict[str, float]] | None:
	weights = {"24h": 0.45, "7d": 0.30, "30d": 0.15, "lifetime": 0.10}
	rows = connection.execute(
		"""
		SELECT window_name, value_real, confidence, evidence_count
		FROM derived_signal_windows
		WHERE user_id = ? AND signal_key = 'risk.composite' AND analyzer_version = ?
		""",
		(user_id, SIGNAL_ANALYZER_VERSION),
	).fetchall()
	if not rows:
		return None
	available = [(str(row[0]), float(row[1]), float(row[2]), int(row[3])) for row in rows if str(row[0]) in weights]
	if not available:
		return None
	weight_total = sum(weights[name] for name, *_ in available)
	value = sum(value * weights[name] for name, value, _, _ in available) / weight_total
	confidence = sum(confidence * weights[name] for name, _, confidence, _ in available) / weight_total
	evidence = max(count for _, _, _, count in available)
	window_values = {name: round(value, 4) for name, value, _, _ in available}
	return round(value, 4), round(confidence, 4), evidence, window_values


def _manual_adjustment(connection: sqlite3.Connection, user_id: int) -> tuple[float, int]:
	row = connection.execute(
		"""
		SELECT COALESCE(SUM(delta), 0), COUNT(*) FROM reputation_events
		WHERE user_id = ? AND source_type IN (
			'initial_calibration', 'manual_adjustment', 'server_boost'
		)
		""",
		(user_id,),
	).fetchone()
	return max(-150.0, min(400.0, float(row[0] or 0.0))), int(row[1] or 0)


def _component(
	key: str, label: str, raw: float, normalized: float, weight: float, *,
	confidence: float, evidence_count: int, source: Mapping[str, object],
	apply_confidence: bool = True,
) -> ScoreComponent:
	resolved_confidence = _clip01(confidence)
	resolved_normalized = _clip01(normalized)
	contribution = weight * resolved_normalized
	if apply_confidence:
		contribution *= resolved_confidence
	return ScoreComponent(
		key=key, label=label, raw_value=round(float(raw), 6),
		normalized_value=round(resolved_normalized, 6), weight=weight,
		contribution=round(contribution, 4), confidence=resolved_confidence,
		evidence_count=max(0, int(evidence_count)), source=source,
	)


def _value(signals: Mapping[str, sqlite3.Row], key: str) -> float:
	row = signals.get(key)
	return float(row[1]) if row is not None else 0.0


def _confidence(signals: Mapping[str, sqlite3.Row], key: str) -> float:
	row = signals.get(key)
	return float(row[2]) if row is not None else 0.0


def _evidence(signals: Mapping[str, sqlite3.Row], key: str) -> int:
	row = signals.get(key)
	return int(row[3]) if row is not None else 0


def _clip01(value: float) -> float:
	return max(0.0, min(1.0, float(value)))


def _json_mapping(raw: object) -> Mapping[str, object]:
	try:
		value = json.loads(str(raw or "{}"))
	except (TypeError, ValueError):
		return {}
	return value if isinstance(value, dict) else {}
