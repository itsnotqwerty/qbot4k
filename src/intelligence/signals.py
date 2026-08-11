from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


SIGNAL_ANALYZER_VERSION = 2

SIGNAL_LABELS: Mapping[str, str] = {
    "activity.message_count": "Messages observed",
    "activity.eligible_message_count": "Score-eligible messages",
    "activity.active_channel_count": "Active channels",
    "activity.platform_count": "Observed platforms",
    "identity.linked_account_count": "Linked accounts",
    "behavior.positive_message_ratio": "Positive message ratio",
    "behavior.negative_message_ratio": "Negative message ratio",
    "behavior.negative_severity_points": "Negative content severity",
    "behavior.reply_to_human_count": "Human replies",
    "behavior.welcome_count": "Welcome messages",
    "behavior.welcome_duplicate_count": "Duplicate welcomes",
    "moderation.finding_count": "Moderation findings",
    "moderation.penalty_points": "Moderation penalty evidence",
    "moderation.severity_index": "Moderation severity index",
    "risk.composite": "Composite risk",
}


@dataclass(frozen=True)
class DerivedSignal:
    user_id: int
    signal_key: str
    value: float
    confidence: float
    evidence_count: int
    window_start: str | None
    window_end: str | None
    details: Mapping[str, object]
    analyzer_version: int = SIGNAL_ANALYZER_VERSION
    calculated_at: str | None = None

    @property
    def label(self) -> str:
        return SIGNAL_LABELS.get(self.signal_key, self.signal_key)


def refresh_user_derived_signals(
    connection: sqlite3.Connection,
    user_id: int,
) -> list[DerivedSignal]:
    user_row = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if user_row is None:
        raise ValueError(f"User {user_id} does not exist")

    activity = connection.execute(
        """
        SELECT
            COUNT(DISTINCT messages.id),
            COUNT(DISTINCT messages.platform || ':' || messages.channel_id),
            COUNT(DISTINCT messages.platform),
            MIN(messages.sent_at),
            MAX(messages.sent_at)
        FROM messages
        INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
        WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
        """,
        (user_id,),
    ).fetchone()
    message_count = int(activity[0] or 0)
    channel_count = int(activity[1] or 0)
    platform_count = int(activity[2] or 0)
    window_start = str(activity[3]) if activity[3] is not None else None
    window_end = str(activity[4]) if activity[4] is not None else None

    account_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM platform_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    behavior = connection.execute(
        """
        SELECT
            COUNT(DISTINCT CASE WHEN reason_code IN ('message_sent', 'positive_message', 'very_negative_content', 'reply_to_non_bot') THEN source_id END),
            SUM(CASE WHEN reason_code = 'positive_message' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reason_code = 'very_negative_content' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reason_code = 'very_negative_content' THEN ABS(delta) ELSE 0 END),
            SUM(CASE WHEN reason_code = 'reply_to_non_bot' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reason_code = 'welcome_new_user' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reason_code = 'welcome_spam_duplicate' THEN 1 ELSE 0 END)
        FROM reputation_events
        WHERE user_id = ? AND source_type = 'message'
        """,
        (user_id,),
    ).fetchone()
    eligible_message_count = int(behavior[0] or 0)
    positive_count = int(behavior[1] or 0)
    negative_count = int(behavior[2] or 0)
    negative_points = int(behavior[3] or 0)
    reply_count = int(behavior[4] or 0)
    welcome_positive_count = int(behavior[5] or 0)
    welcome_duplicate_count = int(behavior[6] or 0)
    positive_ratio = positive_count / message_count if message_count else 0.0
    negative_ratio = negative_count / message_count if message_count else 0.0

    welcome_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM welcome_events
            INNER JOIN messages ON messages.id = welcome_events.message_id
            INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
            WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
            """,
            (user_id,),
        ).fetchone()[0]
    )
    moderation = connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE severity WHEN 'high' THEN 1.0 WHEN 'medium' THEN 0.6 ELSE 0.25 END), 0.0)
            FROM rule_matches
            INNER JOIN messages ON messages.id = rule_matches.message_id
            INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
            WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
            """,
            (user_id,),
        ).fetchone()
    finding_count = int(moderation[0] or 0)
    severity_points = float(moderation[1] or 0.0)
    moderation_penalty_points = int(connection.execute(
        """
        SELECT COALESCE(SUM(ABS(delta)), 0) FROM reputation_events
        WHERE user_id = ? AND source_type = 'moderation'
        """,
        (user_id,),
    ).fetchone()[0])
    moderation_rate = min(1.0, finding_count / message_count) if message_count else 0.0
    severity_rate = min(1.0, severity_points / message_count) if message_count else 0.0
    risk_score = round(
        min(100.0, negative_ratio * 45.0 + moderation_rate * 35.0 + severity_rate * 20.0),
        2,
    )
    behavioral_confidence = round(min(1.0, message_count / 20.0), 4)
    calculated_at = datetime.now(timezone.utc).isoformat()

    signals = [
        _signal(user_id, "activity.message_count", message_count, message_count, window_start, window_end, {"unit": "messages"}, calculated_at),
        _signal(user_id, "activity.eligible_message_count", eligible_message_count, message_count, window_start, window_end, {"unit": "messages", "excludes": ["commands", "empty_messages"]}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "activity.active_channel_count", channel_count, message_count, window_start, window_end, {"unit": "channels"}, calculated_at),
        _signal(user_id, "activity.platform_count", platform_count, message_count, window_start, window_end, {"unit": "platforms"}, calculated_at),
        _signal(user_id, "identity.linked_account_count", account_count, account_count, None, None, {"unit": "accounts"}, calculated_at, confidence=1.0),
        _signal(user_id, "behavior.positive_message_ratio", positive_ratio, message_count, window_start, window_end, {"positive_messages": positive_count, "message_count": message_count, "unit": "ratio"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "behavior.negative_message_ratio", negative_ratio, message_count, window_start, window_end, {"negative_messages": negative_count, "message_count": message_count, "unit": "ratio"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "behavior.negative_severity_points", negative_points, negative_count, window_start, window_end, {"unit": "legacy_penalty_points", "source": "classified_message_evidence"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "behavior.reply_to_human_count", reply_count, reply_count, window_start, window_end, {"unit": "replies"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "behavior.welcome_count", welcome_positive_count, welcome_count, window_start, window_end, {"unit": "welcome_events", "all_welcome_events": welcome_count}, calculated_at),
        _signal(user_id, "behavior.welcome_duplicate_count", welcome_duplicate_count, welcome_duplicate_count, window_start, window_end, {"unit": "duplicate_welcome_events"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "moderation.finding_count", finding_count, message_count, window_start, window_end, {"unit": "findings"}, calculated_at),
        _signal(user_id, "moderation.penalty_points", moderation_penalty_points, finding_count, window_start, window_end, {"unit": "legacy_penalty_points", "source": "moderation_evidence"}, calculated_at, confidence=behavioral_confidence),
        _signal(user_id, "moderation.severity_index", severity_rate, finding_count, window_start, window_end, {"weighted_severity": round(severity_points, 4), "message_count": message_count, "unit": "ratio"}, calculated_at, confidence=behavioral_confidence),
        _signal(
            user_id,
            "risk.composite",
            risk_score,
            message_count,
            window_start,
            window_end,
            {
                "unit": "score_0_100",
                "negative_ratio": round(negative_ratio, 4),
                "moderation_rate": round(moderation_rate, 4),
                "severity_rate": round(severity_rate, 4),
                "formula": "negative_ratio*45 + moderation_rate*35 + severity_rate*20",
                "independent_of_social_score": True,
            },
            calculated_at,
            confidence=behavioral_confidence,
        ),
    ]

    for signal in signals:
        connection.execute(
            """
            INSERT INTO derived_signals (
                user_id, signal_key, analyzer_version, value_real,
                value_json, confidence, evidence_count, window_start,
                window_end, calculated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, signal_key, analyzer_version) DO UPDATE SET
                value_real = excluded.value_real,
                value_json = excluded.value_json,
                confidence = excluded.confidence,
                evidence_count = excluded.evidence_count,
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                calculated_at = excluded.calculated_at,
                updated_at = excluded.updated_at
            """,
            (
                signal.user_id,
                signal.signal_key,
                signal.analyzer_version,
                signal.value,
                json.dumps(dict(signal.details), sort_keys=True, separators=(",", ":")),
                signal.confidence,
                signal.evidence_count,
                signal.window_start,
                signal.window_end,
                signal.calculated_at,
                signal.calculated_at,
            ),
        )
    return signals


def refresh_all_derived_signals(connection: sqlite3.Connection) -> int:
    user_ids = [int(row[0]) for row in connection.execute("SELECT id FROM users ORDER BY id").fetchall()]
    for user_id in user_ids:
        refresh_user_derived_signals(connection, user_id)
    return len(user_ids)


def list_user_derived_signals(
    connection: sqlite3.Connection,
    user_id: int,
) -> list[DerivedSignal]:
    rows = connection.execute(
        """
        SELECT signal_key, value_real, confidence, evidence_count,
               window_start, window_end, value_json, analyzer_version, calculated_at
        FROM derived_signals
        WHERE user_id = ? AND analyzer_version = ?
        ORDER BY CASE signal_key
            WHEN 'risk.composite' THEN 0
            WHEN 'activity.message_count' THEN 1
            ELSE 2
        END, signal_key
        """,
        (user_id, SIGNAL_ANALYZER_VERSION),
    ).fetchall()
    return [_from_row(user_id, row) for row in rows]


def list_signal_overview(
    connection: sqlite3.Connection,
    *,
    signal_keys: tuple[str, ...] = (),
    sort_by: str = "default",
    sort_dir: str = "desc",
    limit: int = 100,
) -> list[tuple[str, DerivedSignal]]:
    selected_keys = tuple(dict.fromkeys(key for key in signal_keys if key in SIGNAL_LABELS))
    normalized_sort = sort_by.strip().casefold()
    if normalized_sort not in {"default", "signal", "value", "confidence", "evidence", "timestamp"}:
        normalized_sort = "default"
    normalized_direction = sort_dir.strip().casefold()
    if normalized_direction not in {"asc", "desc"}:
        normalized_direction = "desc"
    direction_sql = "ASC" if normalized_direction == "asc" else "DESC"
    order_sql = {
        "default": "CASE derived_signals.signal_key WHEN 'risk.composite' THEN 0 ELSE 1 END ASC, derived_signals.value_real DESC",
        "signal": f"derived_signals.signal_key {direction_sql}",
        "value": f"derived_signals.value_real {direction_sql}",
        "confidence": f"derived_signals.confidence {direction_sql}",
        "evidence": f"derived_signals.evidence_count {direction_sql}",
        "timestamp": f"derived_signals.calculated_at {direction_sql}",
    }[normalized_sort]
    conditions = ["derived_signals.analyzer_version = ?"]
    bindings: list[object] = [SIGNAL_ANALYZER_VERSION]
    if selected_keys:
        conditions.append(f"derived_signals.signal_key IN ({','.join('?' for _ in selected_keys)})")
        bindings.extend(selected_keys)
    where_sql = "WHERE " + " AND ".join(conditions)
    bindings.append(max(1, min(limit, 500)))
    rows = connection.execute(
        f"""
        SELECT users.primary_display_name, derived_signals.user_id,
               derived_signals.signal_key, derived_signals.value_real,
               derived_signals.confidence, derived_signals.evidence_count,
               derived_signals.window_start, derived_signals.window_end,
               derived_signals.value_json, derived_signals.analyzer_version,
               derived_signals.calculated_at
        FROM derived_signals
        INNER JOIN users ON users.id = derived_signals.user_id
        {where_sql}
        ORDER BY {order_sql},
            users.primary_display_name COLLATE NOCASE,
            derived_signals.signal_key
        LIMIT ?
        """,
        tuple(bindings),
    ).fetchall()
    return [
        (str(row[0]), _from_row(int(row[1]), row[2:]))
        for row in rows
    ]


def derived_signal_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM derived_signals WHERE analyzer_version = ?",
        (SIGNAL_ANALYZER_VERSION,),
    ).fetchone()[0])


def _signal(
    user_id: int,
    key: str,
    value: float,
    evidence_count: int,
    window_start: str | None,
    window_end: str | None,
    details: Mapping[str, object],
    calculated_at: str,
    *,
    confidence: float | None = None,
) -> DerivedSignal:
    resolved_confidence = min(1.0, evidence_count / 20.0) if confidence is None else confidence
    return DerivedSignal(
        user_id=user_id,
        signal_key=key,
        value=float(value),
        confidence=round(max(0.0, min(1.0, resolved_confidence)), 4),
        evidence_count=evidence_count,
        window_start=window_start,
        window_end=window_end,
        details=details,
        calculated_at=calculated_at,
    )


def _from_row(user_id: int, row: sqlite3.Row | tuple[object, ...]) -> DerivedSignal:
    details = json.loads(str(row[6] or "{}"))
    if not isinstance(details, dict):
        details = {}
    return DerivedSignal(
        user_id=user_id,
        signal_key=str(row[0]),
        value=float(row[1]),
        confidence=float(row[2]),
        evidence_count=int(row[3]),
        window_start=str(row[4]) if row[4] is not None else None,
        window_end=str(row[5]) if row[5] is not None else None,
        details=details,
        analyzer_version=int(row[7]),
        calculated_at=str(row[8]),
    )
