from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..contexts import TenantContext


@dataclass(frozen=True)
class TenantSloSample:
    metric_name: str
    value: float
    target_value: float
    status: str
    evidence_count: int


def collect_tenant_slo_snapshot(
    connection: sqlite3.Connection, *, tenant: TenantContext,
    observed_at: str | None = None,
) -> list[TenantSloSample]:
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    community_id = tenant.community_id
    samples = [
        _latency_sample(connection, "webhook_acceptance_ms", 1000.0, timestamp,
            """SELECT MAX(0,(julianday(ingested_at)-julianday(occurred_at))*86400000)
               FROM observations WHERE community_id=? AND ingested_at>=datetime(?,'-24 hours')""",
            (community_id, timestamp)),
        _latency_sample(connection, "event_to_alert_ms", 10000.0, timestamp,
            """SELECT MAX(0,(julianday(a.created_at)-julianday(o.ingested_at))*86400000)
               FROM intelligence_alerts a JOIN observations o ON o.id=a.observation_id
               WHERE a.community_id=? AND a.created_at>=datetime(?,'-24 hours')""",
            (community_id, timestamp)),
        _latency_sample(connection, "moderation_confirmation_ms", 30000.0, timestamp,
            """SELECT MAX(0,(julianday(provider_confirmed_at)-julianday(created_at))*86400000)
               FROM moderation_actions WHERE community_id=? AND provider_confirmed_at IS NOT NULL
                 AND created_at>=datetime(?,'-24 hours')""",
            (community_id, timestamp)),
        _latency_sample(connection, "queue_age_seconds", 900.0, timestamp,
            """SELECT MAX(0,(julianday(?)-julianday(created_at))*86400) FROM (
                 SELECT q.created_at FROM review_queue q JOIN messages m ON m.id=q.message_id
                  WHERE m.community_id=? AND q.status='open'
                 UNION ALL SELECT created_at FROM member_reports WHERE community_id=? AND status='open'
                 UNION ALL SELECT created_at FROM member_appeals WHERE community_id=? AND status='open')""",
            (timestamp, community_id, community_id, community_id)),
        _percentage_sample(connection, "connector_health_percent", 99.0,
            """SELECT CASE WHEN status='active' AND health_status='healthy' THEN 1 ELSE 0 END
               FROM community_installations WHERE community_id=?""", (community_id,)),
        _percentage_sample(connection, "dashboard_availability_percent", 99.5,
            """SELECT is_up FROM service_reliability_buckets
               WHERE service_name='web' AND bucket_start>=datetime(?,'-24 hours')""", (timestamp,)),
        _count_sample(connection, "open_dead_letters", 0.0,
            "SELECT COUNT(*) FROM dead_letter_events WHERE community_id=? AND status='open'",
            (community_id,)),
        _freshness_sample(connection, timestamp),
    ]
    with connection:
        connection.executemany(
            """INSERT INTO tenant_slo_samples(
                   community_id,metric_name,value,target_value,status,details_json,observed_at
               ) VALUES (?,?,?,?,?,?,?)""",
            [(community_id, sample.metric_name, sample.value, sample.target_value, sample.status,
              json.dumps({"evidence_count": sample.evidence_count}), timestamp) for sample in samples],
        )
    return samples


def list_tenant_slo_samples(
    connection: sqlite3.Connection, *, tenant: TenantContext,
) -> list[TenantSloSample]:
    rows = connection.execute(
        """SELECT s.metric_name,s.value,s.target_value,s.status,s.details_json
           FROM tenant_slo_samples s JOIN (
             SELECT metric_name,MAX(id) id FROM tenant_slo_samples
             WHERE community_id=? GROUP BY metric_name
           ) latest ON latest.id=s.id ORDER BY s.metric_name""",
        (tenant.community_id,),
    ).fetchall()
    return [TenantSloSample(
        metric_name=str(row[0]), value=float(row[1]), target_value=float(row[2]),
        status=str(row[3]), evidence_count=int(json.loads(str(row[4]))["evidence_count"]),
    ) for row in rows]


def _latency_sample(
    connection: sqlite3.Connection, name: str, target: float, timestamp: str,
    query: str, bindings: tuple[object, ...],
) -> TenantSloSample:
    values = sorted(float(row[0]) for row in connection.execute(query, bindings).fetchall()
                    if row[0] is not None)
    if not values:
        return TenantSloSample(name, 0.0, target, "no_data", 0)
    index = min(len(values) - 1, max(0, int((len(values) * 0.95) + 0.999999) - 1))
    value = values[index]
    return TenantSloSample(name, value, target, "met" if value <= target else "breached", len(values))


def _percentage_sample(
    connection: sqlite3.Connection, name: str, target: float,
    query: str, bindings: tuple[object, ...],
) -> TenantSloSample:
    values = [int(row[0]) for row in connection.execute(query, bindings).fetchall()]
    if not values:
        return TenantSloSample(name, 0.0, target, "no_data", 0)
    value = sum(values) * 100.0 / len(values)
    return TenantSloSample(name, value, target, "met" if value >= target else "breached", len(values))


def _count_sample(
    connection: sqlite3.Connection, name: str, target: float,
    query: str, bindings: tuple[object, ...],
) -> TenantSloSample:
    value = float(connection.execute(query, bindings).fetchone()[0])
    return TenantSloSample(name, value, target, "met" if value <= target else "breached", int(value))


def _freshness_sample(connection: sqlite3.Connection, timestamp: str) -> TenantSloSample:
    row = connection.execute(
        """SELECT MAX(0,(julianday(?)-julianday(MAX(observed_at)))*86400)
           FROM operational_metrics WHERE metric_name='backup.success'""", (timestamp,),
    ).fetchone()
    if row is None or row[0] is None:
        return TenantSloSample("backup_freshness_seconds", 0.0, 86400.0, "no_data", 0)
    value = float(row[0])
    return TenantSloSample("backup_freshness_seconds", value, 86400.0,
                           "met" if value <= 86400.0 else "breached", 1)
