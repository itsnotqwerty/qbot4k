from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from .announcements import queue_system_announcement


WELCOME_RATE_LIMIT = 20
ROLE_ASSIGNMENT_MAX_ATTEMPTS = 3


def list_onboarding_resources(
    connection: sqlite3.Connection, *, community_id: int
) -> list[sqlite3.Row]:
    return list(connection.execute(
        """SELECT id,title,resource_url,message_template,enabled,sort_order
           FROM community_onboarding_resources WHERE community_id=?
           ORDER BY sort_order,title COLLATE NOCASE,id""",
        (int(community_id),),
    ).fetchall())


def save_onboarding_resource(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    operator_id: int,
    title: str,
    resource_url: str,
    message_template: str,
    enabled: bool = True,
    sort_order: int = 0,
    resource_id: int | None = None,
) -> int:
    normalized_title = title.strip()
    normalized_url = resource_url.strip()
    normalized_template = message_template.strip()
    parsed_url = urlparse(normalized_url)
    if not normalized_title or len(normalized_title) > 120:
        raise ValueError("resource title is required and must be 120 characters or fewer")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("resource URL must be an absolute HTTP or HTTPS URL")
    if "{mention}" not in normalized_template or "{resource_url}" not in normalized_template:
        raise ValueError("resource template must include {mention} and {resource_url}")
    normalized_order = max(-1000, min(int(sort_order), 1000))
    with connection:
        if resource_id is None:
            cursor = connection.execute(
                """INSERT INTO community_onboarding_resources(
                       community_id,title,resource_url,message_template,enabled,sort_order,
                       created_by_operator_id
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    int(community_id), normalized_title, normalized_url, normalized_template,
                    int(enabled), normalized_order, int(operator_id),
                ),
            )
            saved_id = int(cursor.lastrowid)
            action_type = "onboarding.resource_created"
        else:
            cursor = connection.execute(
                """UPDATE community_onboarding_resources
                   SET title=?,resource_url=?,message_template=?,enabled=?,sort_order=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND community_id=?""",
                (
                    normalized_title, normalized_url, normalized_template, int(enabled),
                    normalized_order, int(resource_id), int(community_id),
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("onboarding resource not found")
            saved_id = int(resource_id)
            action_type = "onboarding.resource_updated"
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,?,'onboarding_resource',?,?)""",
            (
                int(operator_id), action_type, saved_id,
                json.dumps({"community_id": int(community_id), "enabled": bool(enabled)}, sort_keys=True),
            ),
        )
    return saved_id


def delete_onboarding_resource(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    operator_id: int,
    resource_id: int,
) -> None:
    with connection:
        cursor = connection.execute(
            "DELETE FROM community_onboarding_resources WHERE id=? AND community_id=?",
            (int(resource_id), int(community_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError("onboarding resource not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'onboarding.resource_deleted','onboarding_resource',?,?)""",
            (
                int(operator_id), int(resource_id),
                json.dumps({"community_id": int(community_id)}, sort_keys=True),
            ),
        )


def configure_welcome(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    discord_installation_id: int,
    welcome_channel_id: str,
    welcome_template: str,
    enabled: bool,
    operator_id: int,
    newcomer_role_id: str | None = None,
    newcomer_role_enabled: bool = False,
    checkpoint_due_hours: int = 24,
    checkpoint_reminder_enabled: bool = False,
    checkpoint_reminder_template: str = "Reminder {mention}: please complete community verification.",
    verification_resource_enabled: bool = False,
    verification_resource_url: str | None = None,
    verification_resource_template: str = "You are verified, {mention}. Community resources: {resource_url}",
    verification_evidence_required: bool = False,
    self_service_verification_enabled: bool = False,
) -> None:
    template = welcome_template.strip()
    if not welcome_channel_id.strip() or not template:
        raise ValueError("welcome channel and template are required")
    if "{mention}" not in template:
        raise ValueError("welcome template must include {mention}")
    installation = connection.execute(
        """SELECT id FROM community_installations
           WHERE id=? AND community_id=? AND platform='discord' AND status='active'""",
        (int(discord_installation_id), int(community_id)),
    ).fetchone()
    if installation is None:
        raise LookupError("active Discord installation not found")
    normalized_role_id = str(newcomer_role_id or "").strip() or None
    if newcomer_role_enabled and normalized_role_id is None:
        raise ValueError("newcomer role is required when role routing is enabled")
    normalized_due_hours = max(1, min(int(checkpoint_due_hours), 720))
    reminder_template = checkpoint_reminder_template.strip()
    if checkpoint_reminder_enabled and "{mention}" not in reminder_template:
        raise ValueError("checkpoint reminder template must include {mention}")
    resource_url = str(verification_resource_url or "").strip() or None
    resource_template = verification_resource_template.strip()
    if verification_resource_enabled:
        if resource_url is None:
            raise ValueError("resource URL is required when verified-member resources are enabled")
        if "{mention}" not in resource_template or "{resource_url}" not in resource_template:
            raise ValueError("resource template must include {mention} and {resource_url}")
    with connection:
        connection.execute(
            """INSERT INTO community_onboarding_settings(
                   community_id,discord_installation_id,welcome_channel_id,welcome_template,
                   welcome_enabled,newcomer_role_id,newcomer_role_enabled,updated_by_operator_id
                   ,checkpoint_due_hours,checkpoint_reminder_enabled,checkpoint_reminder_template,
                   verification_resource_enabled,verification_resource_url,verification_resource_template
                   ,verification_evidence_required,self_service_verification_enabled
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(community_id) DO UPDATE SET
                   discord_installation_id=excluded.discord_installation_id,
                   welcome_channel_id=excluded.welcome_channel_id,
                   welcome_template=excluded.welcome_template,
                   welcome_enabled=excluded.welcome_enabled,
                   newcomer_role_id=excluded.newcomer_role_id,
                   newcomer_role_enabled=excluded.newcomer_role_enabled,
                   checkpoint_due_hours=excluded.checkpoint_due_hours,
                   checkpoint_reminder_enabled=excluded.checkpoint_reminder_enabled,
                   checkpoint_reminder_template=excluded.checkpoint_reminder_template,
                   verification_resource_enabled=excluded.verification_resource_enabled,
                   verification_resource_url=excluded.verification_resource_url,
                   verification_resource_template=excluded.verification_resource_template,
                   verification_evidence_required=excluded.verification_evidence_required,
                   self_service_verification_enabled=excluded.self_service_verification_enabled,
                   updated_by_operator_id=excluded.updated_by_operator_id,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), int(discord_installation_id), welcome_channel_id.strip(),
                template, int(enabled), normalized_role_id, int(newcomer_role_enabled), int(operator_id),
                normalized_due_hours, int(checkpoint_reminder_enabled), reminder_template,
                int(verification_resource_enabled), resource_url, resource_template,
                int(verification_evidence_required),
                int(self_service_verification_enabled),
            ),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'onboarding.welcome_configured','community',?,?)""",
            (
                int(operator_id), int(community_id),
                json.dumps({
                    "enabled": bool(enabled), "installation_id": int(discord_installation_id),
                    "newcomer_role_enabled": bool(newcomer_role_enabled),
                    "checkpoint_due_hours": normalized_due_hours,
                    "checkpoint_reminder_enabled": bool(checkpoint_reminder_enabled),
                    "verification_resource_enabled": bool(verification_resource_enabled),
                    "verification_evidence_required": bool(verification_evidence_required),
                    "self_service_verification_enabled": bool(self_service_verification_enabled),
                }, sort_keys=True),
            ),
        )


def queue_member_welcome(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    guild_id: str,
    user_id: str,
    username: str,
    event_id: str,
    occurred_at: str,
) -> int | None:
    row = connection.execute(
        """SELECT s.discord_installation_id,s.welcome_channel_id,s.welcome_template,
                  s.newcomer_role_id,s.newcomer_role_enabled,s.welcome_enabled,
                  s.checkpoint_due_hours
           FROM community_onboarding_settings s
           JOIN community_installations i
             ON i.id=s.discord_installation_id AND i.community_id=s.community_id
            AND i.platform='discord' AND i.status='active'
           WHERE s.community_id=? AND i.external_community_id=?""",
        (int(community_id), guild_id.strip()),
    ).fetchone()
    if row is None:
        return None
    role_id = str(row[3] or "").strip() or None
    role_status = "pending" if bool(row[4]) and role_id else "disabled"
    joined_at = _parse_timestamp(occurred_at)
    checkpoint_due_at = (joined_at + timedelta(hours=int(row[6]))).isoformat()
    with connection:
        connection.execute(
            """INSERT INTO community_onboarding_members(
                   community_id,discord_installation_id,platform_user_id,username,status,
                   newcomer_role_id,role_assignment_status,joined_at,checkpoint_due_at
               ) VALUES (?,?,?,?,'newcomer',?,?,?,?)
               ON CONFLICT(community_id,platform_user_id) DO UPDATE SET
                   discord_installation_id=excluded.discord_installation_id,
                   username=excluded.username,newcomer_role_id=excluded.newcomer_role_id,
                   role_assignment_status=CASE
                       WHEN community_onboarding_members.status='verified' THEN community_onboarding_members.role_assignment_status
                       ELSE excluded.role_assignment_status END,
                   joined_at=excluded.joined_at,
                   checkpoint_due_at=CASE
                       WHEN community_onboarding_members.status='verified' THEN community_onboarding_members.checkpoint_due_at
                       ELSE excluded.checkpoint_due_at END,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), int(row[0]), user_id.strip(), username.strip(),
                role_id, role_status, joined_at.isoformat(), checkpoint_due_at,
            ),
        )
    if not bool(row[5]):
        return None
    recent_count = int(connection.execute(
        """SELECT COUNT(*) FROM community_announcements
           WHERE community_id=? AND json_extract(source_json,'$.type')='member_welcome'
             AND created_at>=datetime('now','-5 minutes')""",
        (int(community_id),),
    ).fetchone()[0])
    if recent_count >= WELCOME_RATE_LIMIT:
        with connection:
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('system','onboarding.welcome_rate_limited','community',?,?)""",
                (
                    int(community_id),
                    json.dumps({"event_id": event_id, "user_id": user_id.strip()}, sort_keys=True),
                ),
            )
        return None
    mention = f"<@{user_id.strip()}>"
    body = str(row[2]).replace("{mention}", mention).replace("{username}", username.strip())
    return queue_system_announcement(
        connection, community_id=int(community_id), target_installation_id=int(row[0]),
        target_external_id=str(row[1]), body=body,
        dedupe_key=f"member-welcome:{event_id}",
        source={
            "type": "member_welcome", "user_id": user_id.strip(), "event_id": event_id,
            "occurred_at": occurred_at,
        },
        scheduled_at=datetime.now(UTC).isoformat(),
    )


def mark_onboarding_member_departed(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform_user_id: str,
) -> bool:
    with connection:
        cursor = connection.execute(
            """UPDATE community_onboarding_members
               SET status='departed',role_assignment_status=CASE
                       WHEN role_assignment_status='pending' THEN 'cancelled'
                       ELSE role_assignment_status END,
                   updated_at=CURRENT_TIMESTAMP
               WHERE community_id=? AND platform_user_id=? AND status='newcomer'""",
            (int(community_id), platform_user_id.strip()),
        )
        if cursor.rowcount == 1:
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('system','onboarding.member_departed','community',?,?)""",
                (
                    int(community_id),
                    json.dumps({"platform_user_id": platform_user_id.strip()}, sort_keys=True),
                ),
            )
    return cursor.rowcount == 1


def verify_onboarding_member(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform_user_id: str,
    operator_id: int,
    evidence: str | None = None,
) -> None:
    verified_at = datetime.now(UTC).isoformat()
    normalized_evidence = str(evidence or "").strip()
    if len(normalized_evidence) > 2000:
        raise ValueError("verification evidence must be 2000 characters or fewer")
    with connection:
        resource = connection.execute(
            """SELECT m.discord_installation_id,m.username,m.joined_at,
                      s.welcome_channel_id,s.verification_resource_enabled,
                      s.verification_resource_url,s.verification_resource_template,
                      s.verification_evidence_required
               FROM community_onboarding_members m
               JOIN community_onboarding_settings s ON s.community_id=m.community_id
               WHERE m.community_id=? AND m.platform_user_id=? AND m.status='newcomer'""",
            (int(community_id), platform_user_id.strip()),
        ).fetchone()
        if resource is not None and bool(resource[7]) and not normalized_evidence:
            raise ValueError("verification evidence is required")
        cursor = connection.execute(
            """UPDATE community_onboarding_members
               SET status='verified',verification_evidence=?,verified_at=?,
                   verified_by_operator_id=?,updated_at=CURRENT_TIMESTAMP
               WHERE community_id=? AND platform_user_id=? AND status='newcomer'""",
            (
                normalized_evidence or None, verified_at, int(operator_id),
                int(community_id), platform_user_id.strip(),
            ),
        )
        if cursor.rowcount == 1:
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('operator',?,'onboarding.member_verified','community',?,?)""",
                (
                    int(operator_id), int(community_id),
                    json.dumps({
                        "platform_user_id": platform_user_id.strip(),
                        "evidence": normalized_evidence or None,
                    }, sort_keys=True),
                ),
            )
            _queue_verification_resources(
                connection, community_id=int(community_id),
                platform_user_id=platform_user_id.strip(), resource=resource,
                verified_at=verified_at,
            )
    if cursor.rowcount != 1:
        raise LookupError("newcomer checkpoint not found")


def self_verify_onboarding_member(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform_user_id: str,
) -> None:
    verified_at = datetime.now(UTC).isoformat()
    with connection:
        resource = connection.execute(
            """SELECT m.discord_installation_id,m.username,m.joined_at,
                      s.welcome_channel_id,s.verification_resource_enabled,
                      s.verification_resource_url,s.verification_resource_template,
                      s.verification_evidence_required,s.self_service_verification_enabled
               FROM community_onboarding_members m
               JOIN community_onboarding_settings s ON s.community_id=m.community_id
               WHERE m.community_id=? AND m.platform_user_id=? AND m.status='newcomer'""",
            (int(community_id), platform_user_id.strip()),
        ).fetchone()
        if resource is None:
            raise LookupError("newcomer checkpoint not found")
        if not bool(resource[8]):
            raise PermissionError("self-service verification is disabled")
        if bool(resource[7]):
            raise PermissionError("operator verification evidence is required")
        cursor = connection.execute(
            """UPDATE community_onboarding_members
               SET status='verified',verification_evidence='Self-service Discord verification',
                   verified_at=?,verified_by_operator_id=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE community_id=? AND platform_user_id=? AND status='newcomer'""",
            (verified_at, int(community_id), platform_user_id.strip()),
        )
        if cursor.rowcount != 1:
            raise LookupError("newcomer checkpoint not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('member',?,'onboarding.member_self_verified','community',?,?)""",
            (
                platform_user_id.strip(), int(community_id),
                json.dumps({"platform_user_id": platform_user_id.strip()}, sort_keys=True),
            ),
        )
        _queue_verification_resources(
            connection, community_id=int(community_id),
            platform_user_id=platform_user_id.strip(), resource=resource,
            verified_at=verified_at,
        )


def _queue_verification_resources(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform_user_id: str,
    resource: sqlite3.Row | None,
    verified_at: str,
) -> None:
    if resource is None:
        return
    mention = f"<@{platform_user_id}>"
    if bool(resource[4]):
        body = (
            str(resource[6]).replace("{mention}", mention)
            .replace("{username}", str(resource[1]))
            .replace("{resource_url}", str(resource[5]))
        )
        queue_system_announcement(
            connection, community_id=community_id, target_installation_id=int(resource[0]),
            target_external_id=str(resource[3]), body=body,
            dedupe_key=f"member-verification:{platform_user_id}:{resource[2]}",
            source={"type": "member_verification_resource", "user_id": platform_user_id},
            scheduled_at=verified_at,
        )
    catalog = connection.execute(
        """SELECT id,title,resource_url,message_template
           FROM community_onboarding_resources
           WHERE community_id=? AND enabled=1 ORDER BY sort_order,id""",
        (community_id,),
    ).fetchall()
    for catalog_resource in catalog:
        body = (
            str(catalog_resource[3]).replace("{mention}", mention)
            .replace("{username}", str(resource[1]))
            .replace("{title}", str(catalog_resource[1]))
            .replace("{resource_url}", str(catalog_resource[2]))
        )
        queue_system_announcement(
            connection, community_id=community_id, target_installation_id=int(resource[0]),
            target_external_id=str(resource[3]), body=body,
            dedupe_key=(
                f"member-verification-resource:{catalog_resource[0]}:"
                f"{platform_user_id}:{resource[2]}"
            ),
            source={
                "type": "member_verification_catalog_resource",
                "resource_id": int(catalog_resource[0]), "user_id": platform_user_id,
            },
            scheduled_at=verified_at,
        )


def dispatch_newcomer_roles(
    connection: sqlite3.Connection,
    sender,
    *,
    limit: int = 20,
) -> int:
    rows = connection.execute(
        """SELECT m.community_id,m.platform_user_id,m.newcomer_role_id,m.role_assignment_attempts,
                  i.external_community_id
           FROM community_onboarding_members m
           JOIN community_installations i
             ON i.id=m.discord_installation_id AND i.community_id=m.community_id
            AND i.platform='discord' AND i.status='active'
           WHERE m.status='newcomer' AND m.role_assignment_status='pending'
             AND m.role_assignment_attempts<?
           ORDER BY m.joined_at,m.community_id LIMIT ?""",
        (ROLE_ASSIGNMENT_MAX_ATTEMPTS, max(1, min(int(limit), 200))),
    ).fetchall()
    assigned = 0
    for row in rows:
        community_id = int(row[0])
        user_id = str(row[1])
        role_id = str(row[2])
        attempts = int(row[3]) + 1
        try:
            sender(str(row[4]), user_id, role_id)
        except Exception as exc:
            with connection:
                connection.execute(
                    """UPDATE community_onboarding_members
                       SET role_assignment_status=?,role_assignment_attempts=?,role_assignment_error=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE community_id=? AND platform_user_id=?""",
                    (
                        "failed" if attempts >= ROLE_ASSIGNMENT_MAX_ATTEMPTS else "pending",
                        attempts, str(exc)[:500], community_id, user_id,
                    ),
                )
            continue
        with connection:
            connection.execute(
                """UPDATE community_onboarding_members
                   SET role_assignment_status='assigned',role_assignment_attempts=?,
                       role_assignment_error=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE community_id=? AND platform_user_id=?""",
                (attempts, community_id, user_id),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('system','onboarding.newcomer_role_assigned','community',?,?)""",
                (
                    community_id,
                    json.dumps({"platform_user_id": user_id, "role_id": role_id}, sort_keys=True),
                ),
            )
        assigned += 1
    return assigned


def queue_due_checkpoint_reminders(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    due_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    rows = connection.execute(
        """SELECT m.community_id,m.discord_installation_id,m.platform_user_id,m.username,
                  s.welcome_channel_id,s.checkpoint_reminder_template
           FROM community_onboarding_members m
           JOIN community_onboarding_settings s ON s.community_id=m.community_id
           JOIN community_installations i
             ON i.id=m.discord_installation_id AND i.community_id=m.community_id
            AND i.platform='discord' AND i.status='active'
           WHERE m.status='newcomer' AND m.checkpoint_due_at<=? AND m.reminder_sent_at IS NULL
             AND s.checkpoint_reminder_enabled=1
           ORDER BY m.checkpoint_due_at,m.community_id LIMIT ?""",
        (due_at, max(1, min(int(limit), 500))),
    ).fetchall()
    queued = 0
    for row in rows:
        user_id = str(row[2])
        body = str(row[5]).replace("{mention}", f"<@{user_id}>").replace("{username}", str(row[3]))
        announcement_id = queue_system_announcement(
            connection, community_id=int(row[0]), target_installation_id=int(row[1]),
            target_external_id=str(row[4]), body=body,
            dedupe_key=f"onboarding-checkpoint-reminder:{user_id}",
            source={"type": "onboarding_checkpoint_reminder", "user_id": user_id},
            scheduled_at=due_at,
        )
        if announcement_id is None:
            continue
        with connection:
            connection.execute(
                """UPDATE community_onboarding_members
                   SET reminder_sent_at=?,updated_at=CURRENT_TIMESTAMP
                   WHERE community_id=? AND platform_user_id=? AND reminder_sent_at IS NULL""",
                (due_at, int(row[0]), user_id),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('system','onboarding.checkpoint_reminder_queued','community',?,?)""",
                (int(row[0]), json.dumps({"platform_user_id": user_id}, sort_keys=True)),
            )
        queued += 1
    return queued


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)