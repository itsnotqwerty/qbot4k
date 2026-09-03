from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from ..contexts import TenantContext
from .policy import (
    AUTO_EXPIRED_DISPOSITION,
    COORDINATION_ALERT_MEDIUM_EVIDENCE,
    COORDINATION_ALERT_MIN_EVIDENCE,
)
from .signals import SIGNAL_ANALYZER_VERSION
from .scoring import calculate_social_score, get_current_social_score


REPORT_GENERATOR_VERSION = 2
WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
    ("30d", timedelta(days=30)),
    ("lifetime", None),
)
_URL_DOMAIN = re.compile(r"https?://([^/\s?#]+)", re.IGNORECASE)


@dataclass(frozen=True)
class IntelligenceSummary:
    open_alerts: int
    open_cases: int
    relationships: int
    reports: int


def process_intelligence_observation(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    user_id: int,
    observation_id: int,
) -> None:
    community_row = connection.execute(
        "SELECT community_id FROM observations WHERE id=? AND community_id=?",
        (observation_id, tenant.community_id),
    ).fetchone()
    if community_row is None:
        raise ValueError("observation not found")
    record_community_temporal_signal_run(
        connection,
        community_id=tenant.community_id,
        user_id=user_id,
        trigger_observation_id=observation_id,
    )
    run_id = record_temporal_signal_run(
        connection,
        community_id=tenant.community_id,
        user_id=user_id,
        trigger_observation_id=observation_id,
    )
    if run_id is None:
        return
    extract_observation_relationships(
        connection, tenant=tenant, observation_id=observation_id
    )
    evaluate_signal_alerts(
        connection,
        user_id=user_id,
        calculation_run_id=run_id,
        community_id=tenant.community_id,
    )
    calculate_social_score(
        connection,
        user_id,
        trigger_signal_run_id=run_id,
        calculated_at=_observation_time(connection, observation_id),
    )


def record_temporal_signal_run(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    user_id: int,
    trigger_observation_id: int | None = None,
    calculated_at: str | None = None,
) -> int | None:
    timestamp = calculated_at or _observation_time(connection, trigger_observation_id) or _utcnow()
    cursor = connection.execute(
        """
        INSERT INTO signal_calculation_runs (
            user_id, trigger_observation_id, analyzer_version, calculated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, trigger_observation_id, analyzer_version) DO NOTHING
        """,
        (user_id, trigger_observation_id, SIGNAL_ANALYZER_VERSION, timestamp),
    )
    if cursor.rowcount == 0:
        return None
    run_id = int(cursor.lastrowid)
    run_evidence = _trigger_evidence(
        connection,
        user_id=user_id,
        end_at=timestamp,
        trigger_observation_id=trigger_observation_id,
    )

    window_values: dict[str, dict[str, float]] = {}
    for window_name, duration in WINDOWS:
        values = _calculate_window_values(
            connection, user_id, timestamp, duration, community_id=community_id
        )
        window_values[window_name] = values
        evidence_count = int(values["activity.message_count"])
        confidence = round(min(1.0, evidence_count / 20.0), 4)
        window_start = (
            (datetime.fromisoformat(timestamp).astimezone(timezone.utc) - duration).isoformat()
            if duration is not None
            else values.get("window_start")
        )
        window_end = timestamp
        for signal_key in (
            "activity.message_count",
            "behavior.positive_message_ratio",
            "behavior.negative_message_ratio",
            "moderation.finding_rate",
            "moderation.severity_index",
            "risk.composite",
        ):
            value = float(values[signal_key])
            details = {
                "window": window_name,
                "message_count": int(values["activity.message_count"]),
                "negative_count": int(values["negative_count"]),
                "positive_count": int(values["positive_count"]),
                "finding_count": int(values["finding_count"]),
                "formula_version": SIGNAL_ANALYZER_VERSION,
            }
            connection.execute(
                """
                INSERT INTO derived_signal_windows (
                    user_id, signal_key, window_name, analyzer_version,
                    value_real, value_json, confidence, evidence_count,
                    window_start, window_end, calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, signal_key, window_name, analyzer_version) DO UPDATE SET
                    value_real = excluded.value_real,
                    value_json = excluded.value_json,
                    confidence = excluded.confidence,
                    evidence_count = excluded.evidence_count,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    calculated_at = excluded.calculated_at
                """,
                (user_id, signal_key, window_name, SIGNAL_ANALYZER_VERSION, value, json.dumps(details, sort_keys=True), confidence, evidence_count, window_start, window_end, timestamp),
            )
            history_cursor = connection.execute(
                """
                INSERT INTO derived_signal_history (
                    calculation_run_id, user_id, signal_key, window_name,
                    analyzer_version, value_real, value_json, confidence,
                    evidence_count, window_start, window_end, calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, user_id, signal_key, window_name, SIGNAL_ANALYZER_VERSION, value, json.dumps(details, sort_keys=True), confidence, evidence_count, window_start, window_end, timestamp),
            )
            history_id = int(history_cursor.lastrowid)
            for observation_evidence_id, message_id in run_evidence:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO derived_signal_evidence (
                        signal_history_id, observation_id, message_id, contribution, weight
                    ) VALUES (?, ?, ?, 'supporting', 1.0)
                    """,
                    (history_id, observation_evidence_id, message_id),
                )

    velocity = window_values["24h"]["behavior.negative_message_ratio"] - window_values["7d"]["behavior.negative_message_ratio"]
    evidence_count = int(window_values["7d"]["activity.message_count"])
    confidence = round(min(1.0, evidence_count / 20.0), 4)
    details = {"current_24h": window_values["24h"]["behavior.negative_message_ratio"], "baseline_7d": window_values["7d"]["behavior.negative_message_ratio"]}
    connection.execute(
        """
        INSERT INTO derived_signal_windows (
            user_id, signal_key, window_name, analyzer_version, value_real,
            value_json, confidence, evidence_count, window_start, window_end, calculated_at
        ) VALUES (?, 'behavior.negative_velocity', '24h_vs_7d', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, signal_key, window_name, analyzer_version) DO UPDATE SET
            value_real=excluded.value_real, value_json=excluded.value_json,
            confidence=excluded.confidence, evidence_count=excluded.evidence_count,
            window_start=excluded.window_start, window_end=excluded.window_end,
            calculated_at=excluded.calculated_at
        """,
        (user_id, SIGNAL_ANALYZER_VERSION, velocity, json.dumps(details, sort_keys=True), confidence, evidence_count, (datetime.fromisoformat(timestamp) - timedelta(days=7)).isoformat(), timestamp, timestamp),
    )
    velocity_history = connection.execute(
        """
        INSERT INTO derived_signal_history (
            calculation_run_id, user_id, signal_key, window_name, analyzer_version,
            value_real, value_json, confidence, evidence_count, window_start,
            window_end, calculated_at
        ) VALUES (?, ?, 'behavior.negative_velocity', '24h_vs_7d', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, user_id, SIGNAL_ANALYZER_VERSION, velocity, json.dumps(details, sort_keys=True), confidence, evidence_count, (datetime.fromisoformat(timestamp) - timedelta(days=7)).isoformat(), timestamp, timestamp),
    )
    velocity_history_id = int(velocity_history.lastrowid)
    for observation_evidence_id, message_id in run_evidence:
        connection.execute(
            """
            INSERT OR IGNORE INTO derived_signal_evidence (
                signal_history_id, observation_id, message_id, contribution, weight
            ) VALUES (?, ?, ?, 'supporting', 1.0)
            """,
            (velocity_history_id, observation_evidence_id, message_id),
        )
    return run_id


def record_community_temporal_signal_run(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    user_id: int,
    trigger_observation_id: int | None = None,
    calculated_at: str | None = None,
) -> int | None:
    timestamp = calculated_at or _observation_time(connection, trigger_observation_id) or _utcnow()
    cursor = connection.execute(
        """INSERT INTO community_signal_calculation_runs(
               community_id,user_id,trigger_observation_id,analyzer_version,calculated_at
           ) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(community_id,user_id,trigger_observation_id,analyzer_version)
           DO NOTHING""",
        (community_id, user_id, trigger_observation_id, SIGNAL_ANALYZER_VERSION, timestamp),
    )
    if cursor.rowcount == 0:
        return None
    run_id = int(cursor.lastrowid)
    window_values: dict[str, dict[str, float | str | None]] = {}
    for window_name, duration in WINDOWS:
        values = _calculate_window_values(
            connection, user_id, timestamp, duration, community_id=community_id
        )
        window_values[window_name] = values
        evidence_count = int(values["activity.message_count"])
        confidence = round(min(1.0, evidence_count / 20.0), 4)
        window_start = (
            (datetime.fromisoformat(timestamp).astimezone(timezone.utc) - duration).isoformat()
            if duration is not None else values.get("window_start")
        )
        for signal_key in (
            "activity.message_count", "behavior.positive_message_ratio",
            "behavior.negative_message_ratio", "moderation.finding_rate",
            "moderation.severity_index", "risk.composite",
        ):
            value = float(values[signal_key])
            details = {
                "window": window_name,
                "message_count": evidence_count,
                "negative_count": int(values["negative_count"]),
                "positive_count": int(values["positive_count"]),
                "finding_count": int(values["finding_count"]),
                "formula_version": SIGNAL_ANALYZER_VERSION,
                "community_id": community_id,
            }
            parameters = (
                community_id, user_id, signal_key, window_name, SIGNAL_ANALYZER_VERSION,
                value, json.dumps(details, sort_keys=True), confidence, evidence_count,
                window_start, timestamp, timestamp,
            )
            connection.execute(
                """INSERT INTO community_derived_signal_windows(
                       community_id,user_id,signal_key,window_name,analyzer_version,
                       value_real,value_json,confidence,evidence_count,
                       window_start,window_end,calculated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(community_id,user_id,signal_key,window_name,analyzer_version)
                   DO UPDATE SET value_real=excluded.value_real,value_json=excluded.value_json,
                       confidence=excluded.confidence,evidence_count=excluded.evidence_count,
                       window_start=excluded.window_start,window_end=excluded.window_end,
                       calculated_at=excluded.calculated_at""",
                parameters,
            )
            connection.execute(
                """INSERT INTO community_derived_signal_history(
                       community_id,calculation_run_id,user_id,signal_key,window_name,
                       analyzer_version,value_real,value_json,confidence,evidence_count,
                       window_start,window_end,calculated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (community_id, run_id, *parameters[1:]),
            )

    velocity = float(window_values["24h"]["behavior.negative_message_ratio"]) - float(
        window_values["7d"]["behavior.negative_message_ratio"]
    )
    evidence_count = int(window_values["7d"]["activity.message_count"])
    confidence = round(min(1.0, evidence_count / 20.0), 4)
    details = json.dumps({
        "current_24h": window_values["24h"]["behavior.negative_message_ratio"],
        "baseline_7d": window_values["7d"]["behavior.negative_message_ratio"],
        "community_id": community_id,
    }, sort_keys=True)
    window_start = (datetime.fromisoformat(timestamp) - timedelta(days=7)).isoformat()
    connection.execute(
        """INSERT INTO community_derived_signal_windows(
               community_id,user_id,signal_key,window_name,analyzer_version,value_real,
               value_json,confidence,evidence_count,window_start,window_end,calculated_at
           ) VALUES (?, ?,'behavior.negative_velocity','24h_vs_7d',?,?,?,?,?,?,?,?)
           ON CONFLICT(community_id,user_id,signal_key,window_name,analyzer_version)
           DO UPDATE SET value_real=excluded.value_real,value_json=excluded.value_json,
               confidence=excluded.confidence,evidence_count=excluded.evidence_count,
               window_start=excluded.window_start,window_end=excluded.window_end,
               calculated_at=excluded.calculated_at""",
        (community_id, user_id, SIGNAL_ANALYZER_VERSION, velocity, details, confidence,
         evidence_count, window_start, timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO community_derived_signal_history(
               community_id,calculation_run_id,user_id,signal_key,window_name,
               analyzer_version,value_real,value_json,confidence,evidence_count,
               window_start,window_end,calculated_at
           ) VALUES (?, ?, ?,'behavior.negative_velocity','24h_vs_7d',?,?,?,?,?,?,?,?)""",
        (community_id, run_id, user_id, SIGNAL_ANALYZER_VERSION, velocity, details,
         confidence, evidence_count, window_start, timestamp, timestamp),
    )
    return run_id


def evaluate_signal_alerts(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    calculation_run_id: int,
    community_id: int,
) -> int:
    tenant = TenantContext.require(community_id)
    rows = connection.execute(
        """
        SELECT id, signal_key, window_name, value_real, confidence, evidence_count, calculated_at
        FROM derived_signal_history
        WHERE calculation_run_id = ?
        """,
        (calculation_run_id,),
    ).fetchall()
    created = 0
    for row in rows:
        history_id, key, window_name = int(row[0]), str(row[1]), str(row[2])
        value, confidence, evidence_count = float(row[3]), float(row[4]), int(row[5])
        alert: tuple[str, str, str] | None = None
        if key == "risk.composite" and window_name == "24h" and value >= 50:
            alert = ("risk_threshold", "high" if value >= 75 else "medium", f"24-hour risk reached {value:.1f}")
        elif key == "behavior.negative_velocity" and value >= 0.20 and evidence_count >= 3:
            alert = ("negative_velocity", "medium", f"Negative behavior increased by {value * 100:.1f} points")
        if alert is None:
            continue
        alert_type, severity, summary = alert
        bucket = str(row[6])[:10]
        cursor = connection.execute(
            """
            INSERT INTO intelligence_alerts (
                community_id, user_id, signal_history_id, alert_type, severity, title,
                summary, confidence, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (tenant.community_id, user_id, history_id, alert_type, severity, alert_type.replace("_", " ").title(), summary, confidence, f"{tenant.community_id}:{user_id}:{alert_type}:{bucket}"),
        )
        created += int(cursor.rowcount == 1)
    return created


def extract_observation_relationships(
    connection: sqlite3.Connection, *, tenant: TenantContext, observation_id: int
) -> int:
    observation = connection.execute(
        """
        SELECT observations.*, actor.user_id AS actor_user_id
        FROM observations
        LEFT JOIN platform_accounts actor ON actor.id = observations.actor_platform_account_id
        WHERE observations.id = ? AND observations.community_id = ?
        """,
        (observation_id, tenant.community_id),
    ).fetchone()
    if observation is None or observation["actor_user_id"] is None:
        return 0
    actor_user_id = int(observation["actor_user_id"])
    occurred_at = str(observation["occurred_at"])
    attributes = json.loads(str(observation["attributes_json"] or "{}"))
    created = 0

    for platform_user_id in attributes.get("mentioned_user_ids", []):
        target = connection.execute(
            "SELECT user_id FROM platform_accounts WHERE platform = ? AND platform_user_id = ?",
            (str(observation["platform"]), str(platform_user_id)),
        ).fetchone()
        if target is not None and target[0] is not None and int(target[0]) != actor_user_id:
            _upsert_relationship(connection, actor_user_id, int(target[0]), "mention", "", occurred_at, {"observation_id": observation_id})
            created += 1

    peers = connection.execute(
        """
        SELECT DISTINCT COALESCE(messages.user_id, peer.user_id)
        FROM messages
        INNER JOIN platform_accounts peer ON peer.id = messages.platform_account_id
        WHERE messages.channel_id = ? AND messages.platform = ?
          AND COALESCE(messages.user_id, peer.user_id) IS NOT NULL
          AND COALESCE(messages.user_id, peer.user_id) != ?
          AND datetime(messages.sent_at) BETWEEN datetime(?, '-10 minutes') AND datetime(?, '+10 minutes')
        """,
        (observation["container_id"], observation["platform"], actor_user_id, occurred_at, occurred_at),
    ).fetchall()
    for peer in peers:
        source, target = sorted((actor_user_id, int(peer[0])))
        _upsert_relationship(connection, source, target, "channel_coactivity", str(observation["container_id"] or ""), occurred_at, {"observation_id": observation_id})
        created += 1

    domains = {match.casefold() for match in _URL_DOMAIN.findall(str(observation["text_raw"] or ""))}
    for domain in domains:
        domain_peers = connection.execute(
            """
            SELECT DISTINCT COALESCE(messages.user_id, peer.user_id)
            FROM messages
            INNER JOIN platform_accounts peer ON peer.id = messages.platform_account_id
            WHERE COALESCE(messages.user_id, peer.user_id) IS NOT NULL
              AND COALESCE(messages.user_id, peer.user_id) != ?
              AND lower(messages.content_raw) LIKE ?
              AND datetime(messages.sent_at) >= datetime(?, '-1 day')
            """,
            (actor_user_id, f"%{domain}%", occurred_at),
        ).fetchall()
        for peer in domain_peers:
            source, target = sorted((actor_user_id, int(peer[0])))
            _upsert_relationship(connection, source, target, "shared_domain", domain, occurred_at, {"observation_id": observation_id, "domain": domain})
            created += 1
    return created


def create_case_from_alert(
    connection: sqlite3.Connection, alert_id: int, *, community_id: int,
    operator_id: int | None = None,
) -> int:
    existing = connection.execute(
        """SELECT case_evidence.case_id FROM case_evidence
           JOIN investigation_cases ON investigation_cases.id=case_evidence.case_id
           WHERE case_evidence.alert_id=? AND investigation_cases.community_id=?
           ORDER BY case_evidence.id LIMIT 1""",
        (alert_id, community_id),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    alert = connection.execute(
        """SELECT user_id, title, summary, severity, signal_history_id
           FROM intelligence_alerts WHERE id=? AND community_id=?""",
        (alert_id, community_id),
    ).fetchone()
    if alert is None:
        raise ValueError("alert not found")
    cursor = connection.execute(
        """INSERT INTO investigation_cases(
               community_id,title,summary,priority,owner_operator_id
           ) VALUES (?, ?, ?, ?, ?)""",
        (community_id, str(alert[1]), str(alert[2]), str(alert[3]), operator_id),
    )
    case_id = int(cursor.lastrowid)
    if alert[0] is not None:
        connection.execute("INSERT INTO case_entities (case_id, user_id, role) VALUES (?, ?, 'subject')", (case_id, int(alert[0])))
    connection.execute(
        "INSERT INTO case_evidence (case_id, signal_history_id, alert_id, note) VALUES (?, ?, ?, 'Originating alert and signal')",
        (case_id, alert[4], alert_id),
    )
    if alert[4] is not None:
        evidence_rows = connection.execute(
            "SELECT observation_id, message_id FROM derived_signal_evidence WHERE signal_history_id = ?",
            (alert[4],),
        ).fetchall()
        for evidence in evidence_rows:
            connection.execute(
                "INSERT INTO case_evidence (case_id, observation_id, message_id, signal_history_id, alert_id, note) VALUES (?, ?, ?, ?, ?, 'Supporting signal evidence')",
                (case_id, evidence[0], evidence[1], alert[4], alert_id),
            )
    connection.execute("UPDATE intelligence_alerts SET status='in_case', updated_at=CURRENT_TIMESTAMP WHERE id = ?", (alert_id,))
    connection.execute(
        "INSERT INTO audit_log (actor_type, actor_id, action_type, entity_type, entity_id, payload_json) VALUES (?, ?, 'case.created_from_alert', 'investigation_case', ?, ?)",
        ("operator" if operator_id is not None else "system", operator_id, case_id, json.dumps({"alert_id": alert_id}, sort_keys=True)),
    )
    return case_id


def update_case(
    connection: sqlite3.Connection,
    case_id: int,
    *,
    title: str | None = None,
    summary: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    owner_operator_id: int | None = None,
    operator_id: int | None = None,
    community_id: int,
) -> None:
    """Update case workflow state and append an immutable activity record."""
    row = connection.execute(
        "SELECT * FROM investigation_cases WHERE id=? AND community_id=?",
        (case_id, community_id),
    ).fetchone()
    if row is None:
        raise ValueError("case not found")
    normalized_priority = (priority or str(row["priority"])).strip().casefold()
    normalized_status = (status or str(row["status"])).strip().casefold()
    if normalized_priority not in {"low", "medium", "high", "critical"}:
        raise ValueError("invalid case priority")
    if normalized_status not in {"open", "active", "pending", "closed"}:
        raise ValueError("invalid case status")
    next_title = (title if title is not None else str(row["title"])).strip()
    if not next_title:
        raise ValueError("case title must not be empty")
    next_summary = summary if summary is not None else str(row["summary"])
    owner = owner_operator_id if owner_operator_id is not None else row["owner_operator_id"]
    connection.execute(
        """UPDATE investigation_cases SET title=?, summary=?, priority=?, status=?,
           owner_operator_id=?, updated_at=CURRENT_TIMESTAMP,
           closed_at=CASE WHEN ?='closed' THEN COALESCE(closed_at,CURRENT_TIMESTAMP) ELSE NULL END
           WHERE id=?""",
        (next_title, next_summary, normalized_priority, normalized_status, owner,
         normalized_status, case_id),
    )
    _record_case_activity(connection, case_id, operator_id, "case.updated", "", {
        "title": next_title, "priority": normalized_priority, "status": normalized_status,
        "owner_operator_id": owner,
    })


def add_case_entity(connection: sqlite3.Connection, case_id: int, user_id: int, *,
                    community_id: int, role: str = "subject",
                    operator_id: int | None = None) -> None:
    _require_case(connection, case_id, community_id=community_id)
    if connection.execute(
        """SELECT 1 FROM users WHERE id=? AND (
               EXISTS (SELECT 1 FROM messages WHERE messages.user_id=users.id AND messages.community_id=?)
               OR EXISTS (SELECT 1 FROM intelligence_alerts
                          WHERE intelligence_alerts.user_id=users.id
                            AND intelligence_alerts.community_id=?)
           )""",
        (user_id, community_id, community_id),
    ).fetchone() is None:
        raise ValueError("user not found")
    normalized_role = role.strip().casefold() or "subject"
    connection.execute(
        """INSERT INTO case_entities(case_id,user_id,role) VALUES (?,?,?)
           ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role""",
        (case_id, user_id, normalized_role),
    )
    _record_case_activity(connection, case_id, operator_id, "entity.added", "", {
        "user_id": user_id, "role": normalized_role,
    })


def add_case_evidence(connection: sqlite3.Connection, case_id: int, *,
                      community_id: int,
                      observation_id: int | None = None, message_id: int | None = None,
                      alert_id: int | None = None, signal_history_id: int | None = None,
                      note: str = "", operator_id: int | None = None) -> int:
    _require_case(connection, case_id, community_id=community_id)
    if all(value is None for value in (observation_id, message_id, alert_id, signal_history_id)):
        raise ValueError("case evidence requires an evidence reference")
    if observation_id is not None and connection.execute(
        "SELECT 1 FROM observations WHERE id=? AND community_id=?", (observation_id, community_id)
    ).fetchone() is None:
        raise ValueError("observation not found")
    if message_id is not None and connection.execute(
        "SELECT 1 FROM messages WHERE id=? AND community_id=?", (message_id, community_id)
    ).fetchone() is None:
        raise ValueError("message not found")
    if alert_id is not None and connection.execute(
        "SELECT 1 FROM intelligence_alerts WHERE id=? AND community_id=?", (alert_id, community_id)
    ).fetchone() is None:
        raise ValueError("alert not found")
    cursor = connection.execute(
        """INSERT INTO case_evidence(case_id,observation_id,message_id,signal_history_id,alert_id,note)
           VALUES (?,?,?,?,?,?)""",
        (case_id, observation_id, message_id, signal_history_id, alert_id, note.strip()),
    )
    evidence_id = int(cursor.lastrowid)
    _record_case_activity(connection, case_id, operator_id, "evidence.added", note.strip(), {
        "case_evidence_id": evidence_id, "observation_id": observation_id,
        "message_id": message_id, "alert_id": alert_id, "signal_history_id": signal_history_id,
    })
    return evidence_id


def add_case_note(connection: sqlite3.Connection, case_id: int, body: str, *,
                  community_id: int, operator_id: int | None = None) -> int:
    _require_case(connection, case_id, community_id=community_id)
    cleaned = body.strip()
    if not cleaned:
        raise ValueError("case note must not be empty")
    return _record_case_activity(connection, case_id, operator_id, "note.added", cleaned, {})


def update_alert_workflow(connection: sqlite3.Connection, alert_id: int, *,
                          community_id: int,
                          status: str | None = None, assigned_operator_id: int | None = None,
                          suppress_until: str | None = None,
                          operator_id: int | None = None) -> None:
    normalized = status.strip().casefold() if status else None
    if normalized not in {None, "open", "acknowledged", "in_case", "resolved", "suppressed"}:
        raise ValueError("invalid alert status")
    cursor = connection.execute(
        """UPDATE intelligence_alerts SET status=COALESCE(?,status),
           assigned_operator_id=COALESCE(?,assigned_operator_id),
           acknowledged_at=CASE WHEN ?='acknowledged' THEN COALESCE(acknowledged_at,CURRENT_TIMESTAMP) ELSE acknowledged_at END,
           suppressed_until=CASE WHEN ?='suppressed' THEN ? ELSE suppressed_until END,
              updated_at=CURRENT_TIMESTAMP WHERE id=? AND community_id=?""",
          (normalized, assigned_operator_id, normalized, normalized, suppress_until, alert_id, community_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("alert not found")
    connection.execute(
        """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
           VALUES (?,?, 'alert.workflow_updated','intelligence_alert',?,?)""",
        ("operator" if operator_id is not None else "system", operator_id, alert_id,
         json.dumps({"status": normalized, "assigned_operator_id": assigned_operator_id,
                     "suppress_until": suppress_until}, sort_keys=True)),
    )


def _require_case(
    connection: sqlite3.Connection, case_id: int, *, community_id: int
) -> None:
    if connection.execute(
        "SELECT 1 FROM investigation_cases WHERE id=? AND community_id=?",
        (case_id, community_id),
    ).fetchone() is None:
        raise ValueError("case not found")


def _record_case_activity(connection: sqlite3.Connection, case_id: int, operator_id: int | None,
                          activity_type: str, body: str, payload: Mapping[str, object]) -> int:
    cursor = connection.execute(
        """INSERT INTO case_activity(case_id,operator_id,activity_type,body,payload_json)
           VALUES (?,?,?,?,?)""",
        (case_id, operator_id, activity_type, body, json.dumps(dict(payload), sort_keys=True)),
    )
    connection.execute("UPDATE investigation_cases SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (case_id,))
    return int(cursor.lastrowid)


def dispose_alert(
    connection: sqlite3.Connection, alert_id: int, disposition: str, *, community_id: int,
    operator_id: int | None = None,
) -> None:
    normalized = disposition.strip().casefold()
    if normalized not in {"confirmed", "benign", "unresolved", "escalated"}:
        raise ValueError("invalid disposition")
    alert = connection.execute(
        "SELECT user_id FROM intelligence_alerts WHERE id=? AND community_id=?",
        (alert_id, community_id),
    ).fetchone()
    cursor = connection.execute(
        """
        UPDATE intelligence_alerts
        SET status='resolved', disposition=?, assigned_operator_id=?,
            resolved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND community_id=?
        """,
        (normalized, operator_id, alert_id, community_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("alert not found")
    connection.execute(
        "INSERT INTO audit_log (actor_type, actor_id, action_type, entity_type, entity_id, payload_json) VALUES (?, ?, 'alert.disposed', 'intelligence_alert', ?, ?)",
        ("operator" if operator_id is not None else "system", operator_id, alert_id, json.dumps({"disposition": normalized}, sort_keys=True)),
    )
    from .analytics import record_evaluation_label
    record_evaluation_label(
        connection,
        alert_id=alert_id,
        user_id=int(alert[0]) if alert is not None and alert[0] is not None else None,
        label_key="alert.disposition",
        label_value="positive" if normalized in {"confirmed", "escalated"} else "negative" if normalized == "benign" else "uncertain",
        operator_id=operator_id,
        source="alert_disposition",
    )
    if alert is not None and alert[0] is not None:
        calculate_social_score(connection, int(alert[0]))


def generate_intelligence_report(
    connection: sqlite3.Connection, *, user_id: int | None = None,
    report_type: str = "entity_profile", community_id: int,
) -> int:
    tenant = TenantContext.require(community_id)
    community_id = tenant.community_id
    tenant_clause = " AND community_id=?"
    tenant_params: tuple[object, ...] = (community_id,)
    if user_id is None:
        title = "Daily Intelligence Summary"
        alerts = [dict(row) for row in connection.execute(
            "SELECT id,severity,title,summary,user_id,created_at FROM intelligence_alerts "
            "WHERE status='open'" + tenant_clause + " ORDER BY created_at DESC LIMIT 50",
            tenant_params,
        ).fetchall()]
        cases = [dict(row) for row in connection.execute(
            "SELECT id,title,priority,status,updated_at FROM investigation_cases "
            "WHERE status!='closed'" + tenant_clause + " ORDER BY updated_at DESC LIMIT 50",
            tenant_params,
        ).fetchall()]
        relationship_count = int(connection.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE community_id=?",
            tenant_params,
        ).fetchone()[0])
        content: Mapping[str, object] = {
            "alerts": alerts, "cases": cases, "relationship_count": relationship_count,
        }
        summary = f"{len(alerts)} untriaged alerts, {len(cases)} open cases, {content['relationship_count']} relationships."
        evidence = [{"alert_id": item["id"]} for item in alerts]
    else:
        user = connection.execute(
            """SELECT primary_display_name,current_reputation_score FROM users WHERE id=?
               AND (? IS NULL OR EXISTS (
                   SELECT 1 FROM messages WHERE messages.user_id=users.id AND messages.community_id=?
               ) OR EXISTS (
                   SELECT 1 FROM intelligence_alerts
                   WHERE intelligence_alerts.user_id=users.id AND intelligence_alerts.community_id=?
               ))""",
            (user_id, community_id, community_id, community_id),
        ).fetchone()
        if user is None:
            raise ValueError("user not found")
        title = f"Entity Profile: {user[0]}"
        signals_table = (
            "derived_signal_windows" if community_id is None
            else "community_derived_signal_windows"
        )
        signal_scope = "" if community_id is None else " AND community_id=?"
        signals = [dict(row) for row in connection.execute(
            f"""SELECT signal_key,window_name,value_real,confidence,evidence_count,calculated_at
                FROM {signals_table} WHERE user_id=?{signal_scope}
                ORDER BY signal_key,window_name""",
            (user_id,) if community_id is None else (user_id, community_id),
        ).fetchall()]
        relationships = [dict(row) for row in connection.execute(
            """SELECT relationship_type,source_user_id,target_user_id,context_key,
                      strength,evidence_count,last_observed_at FROM entity_relationships
               WHERE (source_user_id=? OR target_user_id=?)""" + tenant_clause
            + " ORDER BY strength DESC LIMIT 50",
            (user_id, user_id, *tenant_params),
        ).fetchall()]
        alerts = [dict(row) for row in connection.execute(
            "SELECT id,severity,title,summary,status,created_at FROM intelligence_alerts "
            "WHERE user_id=?" + tenant_clause + " ORDER BY created_at DESC LIMIT 50",
            (user_id, *tenant_params),
        ).fetchall()]
        score = None
        content = {
            "user_id": user_id,
            "display_name": str(user[0]),
            "social_score": asdict(score) if score is not None else None,
            "signals": signals,
            "relationships": relationships,
            "alerts": alerts,
        }
        summary = f"Profile contains {len(signals)} windowed signals, {len(relationships)} relationships, and {len(alerts)} alerts."
        evidence = [{"alert_id": item["id"]} for item in alerts]
    cursor = connection.execute(
        """
        INSERT INTO intelligence_reports (
            community_id, report_type, subject_user_id, title, summary, content_json,
            evidence_json, generator_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (community_id, report_type, user_id, title, summary, json.dumps(content, sort_keys=True),
         json.dumps(evidence, sort_keys=True), REPORT_GENERATOR_VERSION),
    )
    report_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO audit_log (actor_type, action_type, entity_type, entity_id, payload_json) VALUES ('system', 'report.generated', 'intelligence_report', ?, ?)",
        (report_id, json.dumps({"report_type": report_type, "subject_user_id": user_id}, sort_keys=True)),
    )
    return report_id


def intelligence_summary(
    connection: sqlite3.Connection, *, tenant: TenantContext
) -> IntelligenceSummary:
    params = (tenant.community_id,)
    return IntelligenceSummary(
        open_alerts=int(connection.execute(
            "SELECT COUNT(*) FROM intelligence_alerts WHERE status='open' AND community_id=?", params
        ).fetchone()[0]),
        open_cases=int(connection.execute(
            "SELECT COUNT(*) FROM investigation_cases WHERE status!='closed' AND community_id=?", params
        ).fetchone()[0]),
        relationships=int(connection.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE community_id=?", params,
        ).fetchone()[0]),
        reports=int(connection.execute(
            "SELECT COUNT(*) FROM intelligence_reports WHERE community_id=?", params,
        ).fetchone()[0]),
    )


def _calculate_window_values(
    connection: sqlite3.Connection, user_id: int, end_at: str,
    duration: timedelta | None, *, community_id: int,
) -> dict[str, float | str | None]:
    cutoff = (datetime.fromisoformat(end_at).astimezone(timezone.utc) - duration).isoformat() if duration else None
    condition = "AND messages.sent_at >= ? AND messages.sent_at <= ?" if cutoff else "AND messages.sent_at <= ?"
    community_condition = " AND messages.community_id=?"
    bindings: tuple[object, ...] = (
        (user_id, cutoff, end_at) if cutoff else (user_id, end_at)
    ) + (community_id,)
    activity = connection.execute(
        f"""
        SELECT COUNT(DISTINCT messages.id), MIN(messages.sent_at)
        FROM platform_accounts
        INNER JOIN messages ON messages.platform_account_id = platform_accounts.id
                WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
                    {condition}{community_condition}
        """,
        bindings,
    ).fetchone()
    message_count = int(activity[0] or 0)
    behavior = connection.execute(
        f"""
        SELECT
            SUM(CASE WHEN reputation_events.reason_code = 'positive_message' THEN 1 ELSE 0 END),
            SUM(CASE WHEN reputation_events.reason_code = 'very_negative_content' THEN 1 ELSE 0 END)
        FROM reputation_events
        INNER JOIN messages ON messages.id = reputation_events.source_id
        INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
        WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
          AND reputation_events.source_type = 'message'
          {condition}{community_condition}
        """,
        bindings,
    ).fetchone()
    positive_count = int(behavior[0] or 0)
    negative_count = int(behavior[1] or 0)
    moderation = connection.execute(
        f"""
        SELECT COUNT(*),
               COALESCE(SUM(CASE rule_matches.severity WHEN 'high' THEN 1.0 WHEN 'medium' THEN 0.6 ELSE 0.25 END), 0.0)
        FROM rule_matches
        INNER JOIN messages ON messages.id=rule_matches.message_id
        INNER JOIN platform_accounts ON platform_accounts.id=messages.platform_account_id
                WHERE COALESCE(messages.user_id, platform_accounts.user_id)=?
                    {condition}{community_condition}
        """,
        bindings,
    ).fetchone()
    finding_count = int(moderation[0] or 0)
    severity_points = float(moderation[1] or 0.0)
    positive_ratio = positive_count / message_count if message_count else 0.0
    negative_ratio = negative_count / message_count if message_count else 0.0
    finding_rate = min(1.0, finding_count / message_count) if message_count else 0.0
    severity_rate = min(1.0, severity_points / message_count) if message_count else 0.0
    risk = round(min(100.0, negative_ratio * 45 + finding_rate * 35 + severity_rate * 20), 2)
    return {
        "activity.message_count": float(message_count),
        "behavior.positive_message_ratio": positive_ratio,
        "behavior.negative_message_ratio": negative_ratio,
        "moderation.finding_rate": finding_rate,
        "moderation.severity_index": severity_rate,
        "risk.composite": risk,
        "positive_count": float(positive_count),
        "negative_count": float(negative_count),
        "finding_count": float(finding_count),
        "window_start": str(activity[1]) if activity[1] is not None else None,
    }


def _trigger_evidence(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    end_at: str,
    trigger_observation_id: int | None,
) -> list[tuple[int | None, int]]:
    if trigger_observation_id is not None:
        row = connection.execute(
            """
            SELECT messages.observation_id, messages.id
            FROM messages
            INNER JOIN platform_accounts
                ON platform_accounts.id = messages.platform_account_id
            WHERE messages.observation_id = ?
              AND COALESCE(messages.user_id, platform_accounts.user_id) = ?
            """,
            (trigger_observation_id, user_id),
        ).fetchone()
        if row is not None:
            return [(int(row[0]), int(row[1]))]

    row = connection.execute(
        """
        SELECT messages.observation_id, messages.id
        FROM messages
        INNER JOIN platform_accounts
            ON platform_accounts.id = messages.platform_account_id
        WHERE COALESCE(messages.user_id, platform_accounts.user_id) = ?
          AND messages.sent_at <= ?
        ORDER BY messages.sent_at DESC, messages.id DESC
        LIMIT 1
        """,
        (user_id, end_at),
    ).fetchone()
    if row is None:
        return []
    return [
        (
            int(row[0]) if row[0] is not None else None,
            int(row[1]),
        )
    ]


def _upsert_relationship(connection: sqlite3.Connection, source: int, target: int, kind: str, context: str, occurred_at: str, evidence: Mapping[str, object]) -> None:
    if evidence.get("observation_id") is not None:
        scope = connection.execute(
            "SELECT community_id FROM observations WHERE id=?",
            (int(evidence["observation_id"]),),
        ).fetchone()
        if scope is None:
            raise ValueError("relationship observation not found")
        community_id = TenantContext.require(scope[0]).community_id
    else:
        community_id = TenantContext.require(evidence.get("community_id")).community_id
    connection.execute(
        """
        INSERT INTO entity_relationships (
            community_id, source_user_id, target_user_id, relationship_type, context_key,
            strength, evidence_count, first_observed_at, last_observed_at, evidence_json
        ) VALUES (?, ?, ?, ?, ?, 1.0, 1, ?, ?, ?)
        ON CONFLICT(community_id, source_user_id, target_user_id, relationship_type, context_key) DO UPDATE SET
            strength = min(100.0, entity_relationships.strength + 1.0),
            evidence_count = entity_relationships.evidence_count + 1,
            last_observed_at = excluded.last_observed_at,
            evidence_json = excluded.evidence_json
        """,
        (community_id, source, target, kind, context, occurred_at, occurred_at, json.dumps(dict(evidence), sort_keys=True)),
    )
    relationship = connection.execute(
        "SELECT id, evidence_count FROM entity_relationships WHERE community_id=? AND source_user_id=? AND target_user_id=? AND relationship_type=? AND context_key=?",
        (community_id, source, target, kind, context),
    ).fetchone()
    relationship_id, evidence_count = int(relationship[0]), int(relationship[1])
    observation_id = evidence.get("observation_id")
    if observation_id is not None:
        connection.execute(
            """INSERT OR IGNORE INTO relationship_evidence(
                 relationship_id,observation_id,occurred_at,attributes_json
               ) VALUES (?,?,?,?)""",
            (relationship_id, int(observation_id), occurred_at,
             json.dumps(dict(evidence), sort_keys=True)),
        )
    if evidence_count >= COORDINATION_ALERT_MIN_EVIDENCE:
        connection.execute(
            """
            INSERT INTO intelligence_alerts (
                community_id, user_id, alert_type, severity, title, summary, confidence, dedupe_key
            ) VALUES (?, ?, 'coordination_pattern', ?, 'Coordination Pattern', ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                severity=excluded.severity,
                summary=excluded.summary,
                confidence=excluded.confidence,
                status=CASE WHEN intelligence_alerts.status='resolved'
                                  AND intelligence_alerts.disposition=? THEN 'open'
                            ELSE intelligence_alerts.status END,
                disposition=CASE WHEN intelligence_alerts.status='resolved'
                                       AND intelligence_alerts.disposition=? THEN NULL
                                 ELSE intelligence_alerts.disposition END,
                resolved_at=CASE WHEN intelligence_alerts.status='resolved'
                                      AND intelligence_alerts.disposition=? THEN NULL
                                 ELSE intelligence_alerts.resolved_at END,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                community_id,
                source,
                "medium" if evidence_count >= COORDINATION_ALERT_MEDIUM_EVIDENCE else "low",
                f"{kind.replace('_', ' ').title()} with entity {target} has {evidence_count} supporting observations.",
                min(1.0, evidence_count / COORDINATION_ALERT_MEDIUM_EVIDENCE),
                f"relationship:{relationship_id}:coordination",
                AUTO_EXPIRED_DISPOSITION,
                AUTO_EXPIRED_DISPOSITION,
                AUTO_EXPIRED_DISPOSITION,
            ),
        )


def _observation_time(connection: sqlite3.Connection, observation_id: int | None) -> str | None:
    if observation_id is None:
        return None
    row = connection.execute("SELECT occurred_at FROM observations WHERE id=?", (observation_id,)).fetchone()
    return str(row[0]) if row is not None else None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
