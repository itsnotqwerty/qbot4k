from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contexts import TenantContext
from .community import require_installation_surface
from .quotas import consume_tenant_quota


AnnouncementSender = Callable[[str, str, str, str, dict[str, object]], str | None]
MAX_DELIVERY_ATTEMPTS = 3


def create_announcement(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform: str,
    target_external_id: str,
    body: str,
    created_by_operator_id: int,
    timezone_name: str | None = None,
    target_installation_id: int | None = None,
) -> int:
    normalized_platform = platform.strip().casefold()
    if normalized_platform not in {"discord", "twitch"}:
        raise ValueError("unsupported announcement platform")
    if not target_external_id.strip() or not body.strip():
        raise ValueError("announcement target and body are required")
    consume_tenant_quota(
        connection, tenant=TenantContext(int(community_id)), quota_type="announcements",
    )
    installation_id = _resolve_target_installation(
        connection, community_id=int(community_id), platform=normalized_platform,
        target_installation_id=target_installation_id,
    )
    resolved_timezone = timezone_name
    if resolved_timezone is None:
        row = connection.execute(
            "SELECT timezone FROM communities WHERE id=?", (int(community_id),)
        ).fetchone()
        if row is None:
            raise LookupError("community not found")
        resolved_timezone = str(row[0])
    _timezone(resolved_timezone)
    with connection:
        cursor = connection.execute(
            """INSERT INTO community_announcements(
                   community_id,target_installation_id,platform,target_external_id,body,
                   created_by_operator_id,timezone
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                int(community_id), installation_id, normalized_platform, target_external_id.strip(),
                body.strip(), int(created_by_operator_id), resolved_timezone.strip() or "UTC",
            ),
        )
        announcement_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'announcement.created','community_announcement',?,?)""",
            (
                int(created_by_operator_id), announcement_id,
                json.dumps({"community_id": int(community_id), "platform": normalized_platform}, sort_keys=True),
            ),
        )
    return announcement_id


def queue_system_announcement(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    target_installation_id: int,
    target_external_id: str,
    body: str,
    dedupe_key: str,
    source: dict[str, object],
    scheduled_at: str,
) -> int | None:
    scheduled = _parse_timestamp(scheduled_at)
    installation_id = _resolve_target_installation(
        connection, community_id=int(community_id), platform="discord",
        target_installation_id=int(target_installation_id),
    )
    with connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO community_announcements(
                   community_id,target_installation_id,platform,target_external_id,body,dedupe_key,
                   source_json,status,scheduled_at,timezone
               ) VALUES (?,?,'discord',?,?,?,?, 'scheduled',?,'UTC')""",
            (
                int(community_id), installation_id, target_external_id.strip(), body.strip(),
                dedupe_key.strip(), json.dumps(source, sort_keys=True), scheduled.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            return None
        announcement_id = int(cursor.lastrowid)
        _record_audit(
            connection, actor_type="system", actor_id=None,
            action_type="announcement.created", announcement_id=announcement_id,
            payload={"community_id": int(community_id), "source": source},
        )
    return announcement_id


def approve_announcement(
    connection: sqlite3.Connection,
    *,
    announcement_id: int,
    community_id: int,
    approved_by_operator_id: int,
    scheduled_at: str,
) -> None:
    with connection:
        row = connection.execute(
            "SELECT timezone FROM community_announcements WHERE id=? AND community_id=? AND status='draft'",
            (int(announcement_id), int(community_id)),
        ).fetchone()
        if row is None:
            raise LookupError("draft announcement not found")
        scheduled = _parse_timestamp(scheduled_at, timezone_name=str(row[0]))
        cursor = connection.execute(
            """UPDATE community_announcements
               SET status='scheduled',scheduled_at=?,approved_by_operator_id=?,
                   approved_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=? AND status='draft'""",
            (
                scheduled.isoformat(), int(approved_by_operator_id), datetime.now(UTC).isoformat(),
                int(announcement_id), int(community_id),
            ),
        )
        if cursor.rowcount == 1:
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('operator',?,'announcement.approved','community_announcement',?,?)""",
                (
                    int(approved_by_operator_id), int(announcement_id),
                    json.dumps({"community_id": int(community_id), "scheduled_at": scheduled.isoformat()}, sort_keys=True),
                ),
            )
    if cursor.rowcount != 1:
        raise LookupError("draft announcement not found")


def preview_announcement(
    connection: sqlite3.Connection, *, announcement_id: int, community_id: int
) -> dict[str, object]:
    row = connection.execute(
        """SELECT a.id,a.platform,a.target_external_id,a.body,a.status,a.scheduled_at,
                  i.external_community_id,i.display_name,
                  (SELECT COUNT(*) FROM community_announcement_deliveries d
               WHERE d.announcement_id=a.id),a.timezone
           FROM community_announcements a
                     LEFT JOIN community_installations i
                         ON i.id=a.target_installation_id AND i.community_id=a.community_id
                        AND i.platform=a.platform AND i.status='active'
           WHERE a.id=? AND a.community_id=?""",
        (int(announcement_id), int(community_id)),
    ).fetchone()
    if row is None:
        raise LookupError("announcement not found")
    scheduled_at = str(row[5]) if row[5] is not None else None
    timezone_name = str(row[9])
    return {
        "id": int(row[0]),
        "platform": str(row[1]),
        "target_external_id": str(row[2]),
        "body": str(row[3]),
        "status": str(row[4]),
        "scheduled_at": scheduled_at,
        "scheduled_local": _local_timestamp(scheduled_at, timezone_name),
        "timezone": timezone_name,
        "installation_external_id": str(row[6]) if row[6] is not None else None,
        "installation_name": str(row[7]) if row[7] is not None else None,
        "attempt_count": int(row[8]),
        "ready": row[6] is not None,
    }


def cancel_announcement(
    connection: sqlite3.Connection,
    *,
    announcement_id: int,
    community_id: int,
    operator_id: int,
) -> None:
    with connection:
        cursor = connection.execute(
            """UPDATE community_announcements
               SET status='cancelled',updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=? AND status IN ('draft','scheduled','failed')""",
            (int(announcement_id), int(community_id)),
        )
        if cursor.rowcount == 1:
            _record_audit(
                connection, actor_type="operator", actor_id=int(operator_id),
                action_type="announcement.cancelled", announcement_id=int(announcement_id),
                payload={"community_id": int(community_id)},
            )
    if cursor.rowcount != 1:
        raise LookupError("cancellable announcement not found")


def retry_announcement(
    connection: sqlite3.Connection,
    *,
    announcement_id: int,
    community_id: int,
    operator_id: int,
    scheduled_at: str,
) -> None:
    scheduled = _parse_timestamp(scheduled_at)
    with connection:
        row = connection.execute(
            """SELECT COUNT(d.id)
               FROM community_announcements a
               LEFT JOIN community_announcement_deliveries d ON d.announcement_id=a.id
               WHERE a.id=? AND a.community_id=? AND a.status='failed'
               GROUP BY a.id""",
            (int(announcement_id), int(community_id)),
        ).fetchone()
        if row is None:
            raise LookupError("failed announcement not found")
        if int(row[0]) >= MAX_DELIVERY_ATTEMPTS:
            raise ValueError("announcement delivery attempt limit reached")
        connection.execute(
            """UPDATE community_announcements
               SET status='scheduled',scheduled_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (scheduled.isoformat(), int(announcement_id)),
        )
        _record_audit(
            connection, actor_type="operator", actor_id=int(operator_id),
            action_type="announcement.retry_scheduled", announcement_id=int(announcement_id),
            payload={"community_id": int(community_id), "scheduled_at": scheduled.isoformat()},
        )


def dispatch_due_announcements(
    connection: sqlite3.Connection,
    sender: AnnouncementSender,
    *,
    now: datetime | None = None,
    limit: int = 50,
    per_community_limit: int = 5,
) -> int:
    due_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    rows = connection.execute(
        """WITH due AS (
               SELECT a.id,a.community_id,a.platform,a.target_external_id,a.body,
                      i.id AS installation_id,i.external_community_id,a.scheduled_at,a.source_json,
                      ROW_NUMBER() OVER (
                          PARTITION BY a.community_id ORDER BY a.scheduled_at,a.id
                      ) AS community_rank
               FROM community_announcements a
               JOIN community_installations i
                 ON i.id=a.target_installation_id AND i.community_id=a.community_id
                AND i.platform=a.platform AND i.status='active'
               WHERE a.status='scheduled' AND a.scheduled_at<=?
                 AND (SELECT COUNT(*) FROM community_announcement_deliveries d
                      WHERE d.announcement_id=a.id) < ?
           )
           SELECT id,community_id,platform,target_external_id,body,installation_id,
                  external_community_id,source_json
           FROM due WHERE community_rank<=?
            ORDER BY community_rank,scheduled_at,id LIMIT ?""",
        (
            due_at, MAX_DELIVERY_ATTEMPTS, max(1, min(int(per_community_limit), 100)),
            max(1, min(int(limit), 500)),
        ),
    ).fetchall()
    delivered = 0
    for row in rows:
        announcement_id = int(row[0])
        installation_id = int(row[5])
        with connection:
            claimed = connection.execute(
                """UPDATE community_announcements SET status='sending',updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='scheduled'""",
                (announcement_id,),
            )
            if claimed.rowcount != 1:
                continue
            attempt_number = int(connection.execute(
                "SELECT COUNT(*)+1 FROM community_announcement_deliveries WHERE announcement_id=?",
                (announcement_id,),
            ).fetchone()[0])
            delivery_id = int(connection.execute(
                """INSERT INTO community_announcement_deliveries(
                       announcement_id,installation_id,attempt_number
                   ) VALUES (?,?,?)""",
                (announcement_id, installation_id, attempt_number),
            ).lastrowid)
        try:
            tenant = TenantContext(
                community_id=int(row[1]), installation_id=installation_id,
            )
            require_installation_surface(
                connection, tenant=tenant, surface="provider:announcement",
            )
            source = json.loads(str(row[7] or "{}"))
            provider_message_id = sender(
                str(row[2]), str(row[6]), str(row[3]), str(row[4]),
                source if isinstance(source, dict) else {},
            )
        except Exception as exc:
            with connection:
                connection.execute(
                    """UPDATE community_announcement_deliveries
                       SET status='failed',error_message=?,completed_at=? WHERE id=?""",
                    (str(exc)[:500], datetime.now(UTC).isoformat(), delivery_id),
                )
                connection.execute(
                    """UPDATE community_announcements
                       SET status='failed',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (str(exc)[:500], announcement_id),
                )
                _record_audit(
                    connection, actor_type="system", actor_id=None,
                    action_type="announcement.delivery_failed", announcement_id=announcement_id,
                    payload={"attempt": attempt_number, "error": str(exc)[:500]},
                )
            continue
        with connection:
            completed_at = datetime.now(UTC).isoformat()
            connection.execute(
                """UPDATE community_announcement_deliveries
                   SET status='delivered',provider_message_id=?,completed_at=? WHERE id=?""",
                (provider_message_id, completed_at, delivery_id),
            )
            connection.execute(
                """UPDATE community_announcements
                   SET status='delivered',delivered_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (completed_at, announcement_id),
            )
            _record_audit(
                connection, actor_type="system", actor_id=None,
                action_type="announcement.delivered", announcement_id=announcement_id,
                payload={"attempt": attempt_number, "provider_message_id": provider_message_id},
            )
        delivered += 1
    return delivered


def _parse_timestamp(value: str, *, timezone_name: str | None = None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid announcement schedule") from exc
    if parsed.tzinfo is None:
        if timezone_name is None:
            raise ValueError("announcement schedule must include a timezone")
        parsed = parsed.replace(tzinfo=_timezone(timezone_name))
    return parsed.astimezone(UTC)


def _timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name.strip() or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid community timezone") from exc


def _local_timestamp(value: str | None, timezone_name: str) -> str | None:
    if value is None:
        return None
    return _parse_timestamp(value).astimezone(_timezone(timezone_name)).isoformat()


def _record_audit(
    connection: sqlite3.Connection,
    *,
    actor_type: str,
    actor_id: int | None,
    action_type: str,
    announcement_id: int,
    payload: dict[str, object],
) -> None:
    connection.execute(
        """INSERT INTO audit_log(
               actor_type,actor_id,action_type,entity_type,entity_id,payload_json
           ) VALUES (?,?,?,'community_announcement',?,?)""",
        (actor_type, actor_id, action_type, announcement_id, json.dumps(payload, sort_keys=True)),
    )


def _resolve_target_installation(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform: str,
    target_installation_id: int | None,
) -> int | None:
    if target_installation_id is not None:
        row = connection.execute(
            """SELECT id FROM community_installations
               WHERE id=? AND community_id=? AND platform=? AND status='active'""",
            (int(target_installation_id), int(community_id), platform),
        ).fetchone()
        if row is None:
            raise LookupError("active target installation not found")
        return int(row[0])
    rows = connection.execute(
        """SELECT id FROM community_installations
           WHERE community_id=? AND platform=? AND status='active' ORDER BY id LIMIT 2""",
        (int(community_id), platform),
    ).fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None