from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..contexts import ActorAttribution, TenantContext
from ..models import NormalizedMessage


def configure_anti_abuse_policy(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    enabled: bool,
    enforcement_mode: str,
    message_burst_limit: int,
    message_burst_window_seconds: int,
    mention_limit: int,
    join_raid_limit: int,
    join_raid_window_seconds: int,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("anti-abuse policy changes require an operator actor")
    mode = enforcement_mode.strip().casefold()
    if mode not in {"shadow", "enforce"}:
        raise ValueError("anti-abuse enforcement mode must be shadow or enforce")
    bounds = {
        "message_burst_limit": (int(message_burst_limit), 2, 100),
        "message_burst_window_seconds": (int(message_burst_window_seconds), 1, 300),
        "mention_limit": (int(mention_limit), 1, 100),
        "join_raid_limit": (int(join_raid_limit), 2, 1000),
        "join_raid_window_seconds": (int(join_raid_window_seconds), 1, 3600),
    }
    for name, (value, minimum, maximum) in bounds.items():
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
    with connection:
        cursor = connection.execute(
            """UPDATE community_policy_settings SET
                   anti_abuse_enabled=?,anti_abuse_enforcement_mode=?,
                   message_burst_limit=?,message_burst_window_seconds=?,mention_limit=?,
                   join_raid_limit=?,join_raid_window_seconds=?,updated_by_operator_id=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE community_id=?""",
            (int(enabled), mode, bounds["message_burst_limit"][0],
             bounds["message_burst_window_seconds"][0], bounds["mention_limit"][0],
             bounds["join_raid_limit"][0], bounds["join_raid_window_seconds"][0],
             actor.actor_id, tenant.community_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("community policy settings not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'anti_abuse.policy_updated','community',?,?)""",
            (actor.actor_id, tenant.community_id, json.dumps({
                "enabled": bool(enabled), "enforcement_mode": mode,
                **{name: value[0] for name, value in bounds.items()},
            }, sort_keys=True)),
        )


def apply_message_abuse_policy(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    installation_id: int | None,
    message_id: int,
    platform_account_id: int,
    user_id: int,
    message: NormalizedMessage,
) -> tuple[str, ...]:
    policy = connection.execute(
        """SELECT anti_abuse_enabled,anti_abuse_enforcement_mode,
                  message_burst_limit,message_burst_window_seconds,mention_limit
           FROM community_policy_settings WHERE community_id=?""",
        (int(community_id),),
    ).fetchone()
    if policy is None or not bool(policy[0]) or message.is_moderator:
        return ()

    findings: list[tuple[str, str, int]] = []
    burst_limit = int(policy[2])
    burst_window = int(policy[3])
    cutoff = _timestamp(message.sent_at) - timedelta(seconds=burst_window)
    recent_count = int(connection.execute(
        """SELECT COUNT(*) FROM messages
           WHERE community_id=? AND platform_account_id=?
             AND datetime(sent_at)>=datetime(?) AND datetime(sent_at)<=datetime(?)""",
        (int(community_id), int(platform_account_id), cutoff.isoformat(), message.sent_at),
    ).fetchone()[0])
    if recent_count >= burst_limit:
        findings.append(("message_flood", "Message flood detected", burst_window))

    mentions = message.metadata.get("mentioned_user_ids")
    mention_count = len(mentions) if isinstance(mentions, (list, tuple)) else 0
    if mention_count >= int(policy[4]):
        findings.append(("mention_spam", "Mention spam detected", burst_window))

    created: list[str] = []
    enforcement_mode = str(policy[1]).strip().casefold()
    for reason_code, title, window_seconds in findings:
        dedupe_key = _window_key(
            reason_code, community_id, platform_account_id, message.sent_at, window_seconds
        )
        cursor = connection.execute(
            """INSERT INTO intelligence_alerts(
                   community_id,user_id,observation_id,alert_type,severity,title,summary,
                   confidence,dedupe_key
               ) VALUES (
                   ?,?,(SELECT observation_id FROM messages WHERE id=?),
                   'anti_abuse','high',?,?,1.0,?
               ) ON CONFLICT(dedupe_key) DO NOTHING""",
            (int(community_id), int(user_id), int(message_id), title,
             f"{reason_code} threshold exceeded by {message.username}", dedupe_key),
        )
        if cursor.rowcount != 1:
            continue
        connection.execute(
            """INSERT INTO review_queue(message_id,severity,queue_reason_code)
               VALUES (?,'high',?)""",
            (int(message_id), reason_code),
        )
        if enforcement_mode == "enforce":
            from .quotas import consume_tenant_quota
            consume_tenant_quota(
                connection, tenant=TenantContext(int(community_id)), quota_type="moderation",
            )
            connection.execute(
                """INSERT INTO moderation_actions(
                       community_id,installation_id,platform,message_id,
                       target_platform_account_id,user_id,action_type,actor_type,
                       reason,duration_seconds,status
                   ) VALUES (?,?,?,?,?,?,'timeout','system',?,600,'pending')""",
                (int(community_id), installation_id, message.platform, int(message_id),
                 int(platform_account_id), int(user_id), reason_code),
            )
        _audit_detection(connection, community_id, reason_code, dedupe_key)
        created.append(reason_code)
    return tuple(created)


def apply_join_raid_policy(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    observation_id: int,
    occurred_at: str,
) -> bool:
    policy = connection.execute(
        """SELECT anti_abuse_enabled,join_raid_limit,join_raid_window_seconds
           FROM community_policy_settings WHERE community_id=?""",
        (int(community_id),),
    ).fetchone()
    if policy is None or not bool(policy[0]):
        return False
    limit = int(policy[1])
    window_seconds = int(policy[2])
    cutoff = _timestamp(occurred_at) - timedelta(seconds=window_seconds)
    join_count = int(connection.execute(
        """SELECT COUNT(*) FROM observations
           WHERE community_id=? AND event_type='member.joined'
             AND datetime(occurred_at)>=datetime(?) AND datetime(occurred_at)<=datetime(?)""",
        (int(community_id), cutoff.isoformat(), occurred_at),
    ).fetchone()[0])
    if join_count < limit:
        return False
    dedupe_key = _window_key("join_raid", community_id, 0, occurred_at, window_seconds)
    cursor = connection.execute(
        """INSERT INTO intelligence_alerts(
               community_id,observation_id,alert_type,severity,title,summary,confidence,dedupe_key
           ) VALUES (? ,?,'anti_abuse','critical','Join raid detected',?,1.0,?)
           ON CONFLICT(dedupe_key) DO NOTHING""",
        (int(community_id), int(observation_id),
         f"{join_count} joins observed within {window_seconds} seconds", dedupe_key),
    )
    if cursor.rowcount != 1:
        return False
    _audit_detection(connection, community_id, "join_raid", dedupe_key)
    return True


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_key(
    reason_code: str, community_id: int, subject_id: int, occurred_at: str, window_seconds: int
) -> str:
    bucket = int(_timestamp(occurred_at).timestamp()) // max(1, int(window_seconds))
    return f"anti-abuse:{community_id}:{reason_code}:{subject_id}:{bucket}"


def _audit_detection(
    connection: sqlite3.Connection, community_id: int, reason_code: str, dedupe_key: str
) -> None:
    connection.execute(
        """INSERT INTO audit_log(actor_type,action_type,entity_type,entity_id,payload_json)
           VALUES ('system','anti_abuse.detected','community',?,?)""",
        (int(community_id), json.dumps({"reason_code": reason_code, "dedupe_key": dedupe_key}, sort_keys=True)),
    )