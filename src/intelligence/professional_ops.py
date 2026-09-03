from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..contexts import ActorAttribution, TenantContext


SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def project_stream_session(connection: sqlite3.Connection, observation: sqlite3.Row) -> int | None:
    event_type = str(observation["event_type"])
    if event_type not in {"stream.started", "stream.updated", "stream.ended"}:
        return None
    attributes = _json_object(observation["attributes_json"])
    community_id = TenantContext.require(observation["community_id"]).community_id
    platform = str(observation["platform"])
    stream_key = str(
        observation["context_id"] or observation["container_id"]
        or attributes.get("broadcaster_user_id") or attributes.get("channel_name") or "default"
    )
    occurred_at = str(observation["occurred_at"])
    external_stream_id = str(attributes.get("stream_id") or "").strip() or None
    active = connection.execute(
        """SELECT id FROM stream_sessions WHERE community_id=? AND platform=?
           AND stream_key=? AND status='live' ORDER BY started_at DESC LIMIT 1""",
        (community_id, platform, stream_key),
    ).fetchone()
    if active is None and event_type != "stream.ended":
        cursor = connection.execute(
            """INSERT INTO stream_sessions(
                   community_id,platform,stream_key,external_stream_id,title,category,
                   started_at,opening_observation_id
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (community_id, platform, stream_key, external_stream_id,
             attributes.get("title"), attributes.get("category") or attributes.get("game_name"),
             occurred_at, int(observation["id"])),
        )
        return int(cursor.lastrowid)
    if active is None:
        return None
    session_id = int(active[0])
    if event_type == "stream.ended":
        connection.execute(
            """UPDATE stream_sessions SET status='ended',ended_at=?,closing_observation_id=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (occurred_at, int(observation["id"]), session_id),
        )
        refresh_stream_cohorts(connection, session_id)
        generate_post_stream_briefing(connection, session_id)
    else:
        connection.execute(
            """UPDATE stream_sessions SET external_stream_id=COALESCE(?,external_stream_id),
                   title=COALESCE(?,title),category=COALESCE(?,category),updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (external_stream_id, attributes.get("title"),
             attributes.get("category") or attributes.get("game_name"), session_id),
        )
    return session_id


def active_stream_session_id(connection: sqlite3.Connection, community_id: int) -> int | None:
    row = connection.execute(
        """SELECT id FROM stream_sessions WHERE community_id=? AND status='live'
           ORDER BY started_at DESC LIMIT 1""",
        (int(community_id),),
    ).fetchone()
    return int(row[0]) if row is not None else None


def conversation_context(
    connection: sqlite3.Connection,
    observation_id: int,
    *,
    before: int = 12,
    after: int = 6,
) -> dict[str, Any]:
    finding = connection.execute(
        """SELECT id,community_id,platform,context_id,container_id,occurred_at,event_type,text_raw
           FROM observations WHERE id=?""",
        (int(observation_id),),
    ).fetchone()
    if finding is None:
        raise ValueError("finding observation was not found")
    context_key = str(finding[3] or finding[4] or "")
    common = """SELECT o.id,o.event_type,o.occurred_at,o.text_raw,o.attributes_json,
                       pa.username,pa.platform_user_id,pa.user_id,m.id AS message_id
                FROM observations o
                LEFT JOIN platform_accounts pa ON pa.id=o.actor_platform_account_id
                LEFT JOIN messages m ON m.observation_id=o.id
                WHERE o.community_id=? AND o.platform=?
                  AND COALESCE(o.context_id,o.container_id,'')=?"""
    prior = connection.execute(
        common + " AND (datetime(o.occurred_at)<datetime(?) OR (datetime(o.occurred_at)=datetime(?) AND o.id<=?)) ORDER BY datetime(o.occurred_at) DESC,o.id DESC LIMIT ?",
        (int(finding[1]), str(finding[2]), context_key, str(finding[5]), str(finding[5]),
         int(observation_id), max(1, int(before))),
    ).fetchall()
    following = connection.execute(
        common + " AND (datetime(o.occurred_at)>datetime(?) OR (datetime(o.occurred_at)=datetime(?) AND o.id>?)) ORDER BY datetime(o.occurred_at),o.id LIMIT ?",
        (int(finding[1]), str(finding[2]), context_key, str(finding[5]), str(finding[5]),
         int(observation_id), max(0, int(after))),
    ).fetchall()
    rows = list(reversed(prior)) + list(following)
    return {
        "finding_observation_id": int(observation_id),
        "community_id": int(finding[1]), "platform": str(finding[2]),
        "context_id": context_key,
        "items": [
            {
                "observation_id": int(row[0]), "event_type": str(row[1]),
                "occurred_at": str(row[2]), "text": str(row[3] or ""),
                "attributes": _json_object(row[4]), "username": str(row[5] or "system"),
                "platform_user_id": str(row[6] or ""),
                "user_id": int(row[7]) if row[7] is not None else None,
                "message_id": int(row[8]) if row[8] is not None else None,
                "is_finding": int(row[0]) == int(observation_id),
            }
            for row in rows
        ],
    }


def upsert_campaign_incident(connection: sqlite3.Connection, campaign_id: int) -> int:
    campaign = connection.execute(
        """SELECT id,community_id,severity,message_count,actor_count,confidence,
                  first_observed_at,last_observed_at,details_json
           FROM coordination_campaigns WHERE id=?""",
        (int(campaign_id),),
    ).fetchone()
    if campaign is None:
        raise ValueError("coordination campaign was not found")
    stream_session_id = active_stream_session_id(connection, int(campaign[1]))
    title = f"Coordinated spam campaign ({int(campaign[3])} messages)"
    summary = (
        f"{int(campaign[4])} actors produced {int(campaign[3])} matching messages; "
        f"campaign confidence {float(campaign[5]) * 100:.0f}%."
    )
    connection.execute(
        """INSERT INTO operations_incidents(
               community_id,stream_session_id,campaign_id,incident_type,severity,title,summary,
               opened_at,updated_at
           ) VALUES (?,?,?,'coordination_campaign',?,?,?,?,?)
           ON CONFLICT(community_id,campaign_id) DO UPDATE SET
               stream_session_id=COALESCE(operations_incidents.stream_session_id,excluded.stream_session_id),
               severity=excluded.severity,title=excluded.title,summary=excluded.summary,
               updated_at=excluded.updated_at""",
        (int(campaign[1]), stream_session_id, int(campaign[0]), str(campaign[2]), title, summary,
         str(campaign[6]), str(campaign[7])),
    )
    incident = connection.execute(
        "SELECT id FROM operations_incidents WHERE community_id=? AND campaign_id=?",
        (int(campaign[1]), int(campaign[0])),
    ).fetchone()
    assert incident is not None
    incident_id = int(incident[0])
    dedupe_key = f"operations:campaign:{int(campaign[0])}"
    connection.execute(
        """INSERT INTO intelligence_alerts(
               community_id,alert_type,severity,title,summary,confidence,dedupe_key
           ) VALUES (?,'coordination_campaign',?,?,?,?,?)
           ON CONFLICT(dedupe_key) DO UPDATE SET severity=excluded.severity,title=excluded.title,
               summary=excluded.summary,confidence=excluded.confidence,updated_at=CURRENT_TIMESTAMP""",
        (int(campaign[1]), str(campaign[2]), title, summary, float(campaign[5]), dedupe_key),
    )
    summary_alert = connection.execute(
        "SELECT id FROM intelligence_alerts WHERE dedupe_key=?", (dedupe_key,)
    ).fetchone()
    assert summary_alert is not None
    summary_alert_id = int(summary_alert[0])
    connection.execute(
        "INSERT OR IGNORE INTO incident_alerts(incident_id,alert_id) VALUES (?,?)",
        (incident_id, summary_alert_id),
    )
    related_alerts = connection.execute(
        """SELECT a.id FROM intelligence_alerts a
           JOIN coordination_campaign_members member ON member.observation_id=a.observation_id
           WHERE member.campaign_id=? AND a.id<>?""",
        (int(campaign_id), summary_alert_id),
    ).fetchall()
    connection.executemany(
        "INSERT OR IGNORE INTO incident_alerts(incident_id,alert_id) VALUES (?,?)",
        ((incident_id, int(row[0])) for row in related_alerts),
    )
    connection.execute(
        """UPDATE intelligence_alerts SET status='grouped',disposition='campaign_grouped',
               updated_at=CURRENT_TIMESTAMP
           WHERE id IN (
               SELECT alert_id FROM incident_alerts WHERE incident_id=? AND alert_id<>?
           ) AND status='open'""",
        (incident_id, summary_alert_id),
    )
    queue_incident_notifications(connection, incident_id)
    return incident_id


def assign_incident(
    connection: sqlite3.Connection, *, incident_id: int, operator_id: int, assigned_by: int
) -> None:
    with connection:
        cursor = connection.execute(
            """UPDATE operations_incidents SET assigned_operator_id=?,status='active',
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('closed','resolved')""",
            (int(operator_id), int(incident_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("open incident was not found")
        _incident_activity(connection, incident_id, assigned_by, "assigned", "",
                           {"assigned_operator_id": int(operator_id)})


def escalate_incident(
    connection: sqlite3.Connection, *, incident_id: int, operator_id: int, note: str = ""
) -> int:
    with connection:
        cursor = connection.execute(
            """UPDATE operations_incidents SET escalation_level=MIN(3,escalation_level+1),
                   severity=CASE WHEN severity IN ('info','low','medium') THEN 'high' ELSE 'critical' END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('closed','resolved')""",
            (int(incident_id),),
        )
        if cursor.rowcount != 1:
            raise ValueError("open incident was not found")
        row = connection.execute(
            "SELECT escalation_level FROM operations_incidents WHERE id=?", (int(incident_id),)
        ).fetchone()
        level = int(row[0])
        _incident_activity(connection, incident_id, operator_id, "escalated", note, {"level": level})
        queue_incident_notifications(connection, incident_id, force=True)
    return level


def handoff_shift(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    incoming_operator_id: int,
    note: str,
) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise PermissionError("shift handoff requires an operator actor")
    community_id = tenant.community_id
    outgoing_operator_id = actor.actor_id
    with connection:
        active = connection.execute(
            """SELECT id FROM moderation_shifts WHERE community_id=? AND status='active'
               ORDER BY started_at DESC LIMIT 1""",
            (int(community_id),),
        ).fetchone()
        if active is not None:
            connection.execute(
                """UPDATE moderation_shifts SET status='handed_off',ended_at=CURRENT_TIMESTAMP,
                       incoming_operator_id=?,handoff_note=?,handoff_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(incoming_operator_id), note.strip(), int(active[0])),
            )
        cursor = connection.execute(
            """INSERT INTO moderation_shifts(community_id,lead_operator_id,status,handoff_note)
               VALUES (?,?,'active',?)""",
            (int(community_id), int(incoming_operator_id), note.strip()),
        )
        connection.execute(
            """UPDATE operations_incidents SET assigned_operator_id=?,updated_at=CURRENT_TIMESTAMP
               WHERE community_id=? AND assigned_operator_id=? AND status IN ('open','active')""",
            (int(incoming_operator_id), int(community_id), int(outgoing_operator_id)),
        )
        for incident in connection.execute(
            """SELECT id FROM operations_incidents WHERE community_id=? AND assigned_operator_id=?
               AND status IN ('open','active')""",
            (int(community_id), int(incoming_operator_id)),
        ).fetchall():
            _incident_activity(connection, int(incident[0]), outgoing_operator_id, "shift_handoff",
                               note, {"incoming_operator_id": int(incoming_operator_id)})
    return int(cursor.lastrowid)


def schedule_moderation_shift(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    operator_id: int,
    starts_at: str,
    ends_at: str,
) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise PermissionError("shift scheduling requires an operator actor")
    community_id = tenant.community_id
    created_by_operator_id = actor.actor_id
    start = _aware_timestamp(starts_at)
    end = _aware_timestamp(ends_at)
    if end <= start:
        raise ValueError("shift end must be after its start")
    membership = connection.execute(
        "SELECT 1 FROM operator_community_roles WHERE operator_id=? AND community_id=?",
        (int(operator_id), int(community_id)),
    ).fetchone()
    if membership is None:
        raise ValueError("on-call operator is not assigned to this community")
    overlap = connection.execute(
        """SELECT 1 FROM moderation_shift_schedules
           WHERE community_id=? AND status IN ('scheduled','active')
             AND starts_at<? AND ends_at>?""",
        (int(community_id), end.isoformat(), start.isoformat()),
    ).fetchone()
    if overlap is not None:
        raise ValueError("shift overlaps an existing on-call schedule")
    with connection:
        cursor = connection.execute(
            """INSERT INTO moderation_shift_schedules(
                   community_id,operator_id,starts_at,ends_at,created_by_operator_id
               ) VALUES (?,?,?,?,?)""",
            (
                int(community_id), int(operator_id), start.isoformat(), end.isoformat(),
                int(created_by_operator_id),
            ),
        )
        schedule_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'operations.shift_scheduled','moderation_shift_schedule',?,?)""",
            (
                int(created_by_operator_id), schedule_id,
                json.dumps({
                    "community_id": int(community_id), "operator_id": int(operator_id),
                    "starts_at": start.isoformat(), "ends_at": end.isoformat(),
                }, sort_keys=True),
            ),
        )
    return schedule_id


def list_moderation_shift_schedule(
    connection: sqlite3.Connection, *, community_id: int
) -> list[sqlite3.Row]:
    return list(connection.execute(
        """SELECT s.id,s.operator_id,o.discord_username,s.starts_at,s.ends_at,s.status
           FROM moderation_shift_schedules s
           JOIN operator_accounts o ON o.id=s.operator_id
           WHERE s.community_id=? ORDER BY s.starts_at,s.id""",
        (int(community_id),),
    ).fetchall())


def activate_scheduled_on_call(
    connection: sqlite3.Connection, *, community_id: int, now: datetime | None = None
) -> int | None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with connection:
        connection.execute(
                """UPDATE moderation_shift_schedules SET status='completed',updated_at=CURRENT_TIMESTAMP
                    WHERE community_id=? AND status IN ('scheduled','active') AND ends_at<=?""",
            (int(community_id), current.isoformat()),
        )
        row = connection.execute(
            """SELECT id,operator_id FROM moderation_shift_schedules
               WHERE community_id=? AND status IN ('scheduled','active')
                 AND starts_at<=? AND ends_at>?
               ORDER BY starts_at DESC,id DESC LIMIT 1""",
            (int(community_id), current.isoformat(), current.isoformat()),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """UPDATE moderation_shift_schedules SET status='active',updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (int(row[0]),),
        )
        active_shift = connection.execute(
            """SELECT id,lead_operator_id FROM moderation_shifts
               WHERE community_id=? AND status='active' ORDER BY started_at DESC,id DESC LIMIT 1""",
            (int(community_id),),
        ).fetchone()
        if active_shift is None or int(active_shift[1]) != int(row[1]):
            if active_shift is not None:
                connection.execute(
                    """UPDATE moderation_shifts SET status='completed',ended_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (int(active_shift[0]),),
                )
            connection.execute(
                """INSERT INTO moderation_shifts(community_id,lead_operator_id,status,handoff_note)
                   VALUES (?,?,'active','Scheduled on-call activation')""",
                (int(community_id), int(row[1])),
            )
    return int(row[1])


def route_incident_to_on_call(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    incident_id: int,
    routed_by_operator_id: int,
    now: datetime | None = None,
) -> int:
    operator_id = activate_scheduled_on_call(
        connection, community_id=int(community_id), now=now
    )
    if operator_id is None:
        raise ValueError("no operator is currently on call")
    with connection:
        cursor = connection.execute(
            """UPDATE operations_incidents SET assigned_operator_id=?,status='active',
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=? AND status NOT IN ('closed','resolved')""",
            (int(operator_id), int(incident_id), int(community_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("open incident was not found")
        _incident_activity(
            connection, int(incident_id), int(routed_by_operator_id), "routed_on_call", "",
            {"assigned_operator_id": int(operator_id)},
        )
    return int(operator_id)


def _aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid shift timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("shift timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def activate_playbook(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    playbook_key: str,
    incident_id: int | None = None,
) -> dict[str, Any]:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise PermissionError("playbook activation requires an operator actor")
    community_id = tenant.community_id
    operator_id = actor.actor_id
    playbook = connection.execute(
        """SELECT name,severity,steps_json FROM raid_playbooks
           WHERE playbook_key=? AND enabled=1""",
        (playbook_key.strip(),),
    ).fetchone()
    if playbook is None:
        raise ValueError("enabled playbook was not found")
    if incident_id is not None and connection.execute(
        "SELECT 1 FROM operations_incidents WHERE id=? AND community_id=?",
        (int(incident_id), community_id),
    ).fetchone() is None:
        raise ValueError("tenant incident was not found")
    steps = json.loads(str(playbook[2]))
    with connection:
        cursor = connection.execute(
            """INSERT INTO raid_playbook_runs(
                   community_id,incident_id,playbook_key,activated_by_operator_id,state_json
               ) VALUES (?,?,?,?,?)""",
            (int(community_id), incident_id, playbook_key.strip(), int(operator_id),
             json.dumps({"steps": steps, "completed": []}, sort_keys=True)),
        )
        if incident_id is not None:
            connection.execute(
                """UPDATE operations_incidents SET playbook_key=?,status='active',
                       updated_at=CURRENT_TIMESTAMP WHERE id=? AND community_id=?""",
                (playbook_key.strip(), int(incident_id), community_id),
            )
            _incident_activity(connection, incident_id, operator_id, "playbook_activated",
                               str(playbook[0]), {"playbook_key": playbook_key.strip()})
    return {"run_id": int(cursor.lastrowid), "name": str(playbook[0]), "steps": steps}


def record_audience_event(connection: sqlite3.Connection, observation: sqlite3.Row) -> int | None:
    event_type = str(observation["event_type"])
    if event_type not in {"channel.raided", "channel.shared_chat"}:
        return None
    attributes = _json_object(observation["attributes_json"])
    source = str(
        attributes.get("from_broadcaster_user_id") or attributes.get("source_broadcaster_user_id")
        or attributes.get("host_broadcaster_user_id") or observation["actor_platform_account_id"] or ""
    ).strip()
    target = str(
        attributes.get("to_broadcaster_user_id") or attributes.get("broadcaster_user_id")
        or attributes.get("target_broadcaster_user_id") or observation["context_id"] or ""
    ).strip()
    if not source or not target or source == target:
        return None
    weight = float(attributes.get("viewers") or attributes.get("viewer_count") or 1)
    occurred = str(observation["occurred_at"])
    community_id = int(observation["community_id"])
    connection.execute(
        """INSERT INTO audience_edges(
               community_id,source_key,target_key,edge_type,weight,event_count,
               first_observed_at,last_observed_at,evidence_json
           ) VALUES (?,?,?,?,?,1,?,?,?)
           ON CONFLICT(community_id,source_key,target_key,edge_type) DO UPDATE SET
               weight=audience_edges.weight+excluded.weight,event_count=audience_edges.event_count+1,
               last_observed_at=excluded.last_observed_at,evidence_json=excluded.evidence_json""",
        (community_id, source, target,
         "raid" if event_type == "channel.raided" else "shared_audience", max(1.0, weight),
         occurred, occurred, json.dumps({"observation_id": int(observation["id"]), **attributes}, sort_keys=True)),
    )
    row = connection.execute(
        """SELECT id FROM audience_edges WHERE community_id=? AND source_key=?
           AND target_key=? AND edge_type=?""",
        (community_id, source, target,
         "raid" if event_type == "channel.raided" else "shared_audience"),
    ).fetchone()
    return int(row[0]) if row is not None else None


def confirm_moderation_from_event(connection: sqlite3.Connection, observation: sqlite3.Row) -> int | None:
    if str(observation["platform"]) != "twitch" or str(observation["event_type"]) != "moderation.action":
        return None
    attributes = _json_object(observation["attributes_json"])
    action = str(attributes.get("action") or attributes.get("moderation_action") or "").casefold()
    action = {"timeout": "timeout", "ban": "ban", "warn": "warn", "warning": "warn"}.get(action, action)
    target_user_id = str(attributes.get("target_user_id") or attributes.get("user_id") or "").strip()
    if action not in {"timeout", "ban", "warn"} or not target_user_id:
        return None
    row = connection.execute(
        """SELECT ma.id FROM moderation_actions ma
           JOIN platform_accounts pa ON pa.id=ma.target_platform_account_id
           WHERE ma.platform='twitch' AND pa.platform_user_id=? AND ma.action_type=?
             AND ma.created_at>=datetime(?, '-15 minutes')
           ORDER BY ma.created_at DESC LIMIT 1""",
        (target_user_id, action, str(observation["occurred_at"])),
    ).fetchone()
    if row is None:
        return None
    with connection:
        connection.execute(
            """UPDATE moderation_actions SET provider_confirmed_at=?,provider_event_id=?,
                   provider_status='eventsub_confirmed',status='completed' WHERE id=?""",
            (str(observation["occurred_at"]), str(observation["external_event_id"] or observation["id"]), int(row[0])),
        )
    return int(row[0])


def refresh_stream_cohorts(connection: sqlite3.Connection, stream_session_id: int) -> dict[str, int]:
    session = connection.execute(
        "SELECT community_id,started_at,COALESCE(ended_at,CURRENT_TIMESTAMP) FROM stream_sessions WHERE id=?",
        (int(stream_session_id),),
    ).fetchone()
    if session is None:
        raise ValueError("stream session was not found")
    rows = connection.execute(
        """SELECT pa.id,pa.user_id,COUNT(m.id),MIN(m.sent_at),
                  MAX(CASE WHEN json_extract(o.attributes_json,'$.subscriber') IN (1,'1',true) THEN 1 ELSE 0 END),
                  MAX(CASE WHEN lower(o.attributes_json) LIKE '%vip%' THEN 1 ELSE 0 END),
                  MAX(CASE WHEN json_extract(o.attributes_json,'$.is_moderator') IN (1,'1',true)
                            OR lower(o.attributes_json) LIKE '%moderator%' THEN 1 ELSE 0 END)
           FROM messages m JOIN platform_accounts pa ON pa.id=m.platform_account_id
           LEFT JOIN observations o ON o.id=m.observation_id
           WHERE m.community_id=? AND datetime(m.sent_at)>=datetime(?)
             AND datetime(m.sent_at)<=datetime(?)
           GROUP BY pa.id,pa.user_id""",
        (int(session[0]), str(session[1]), str(session[2])),
    ).fetchall()
    counts = {key: 0 for key in ("unique", "new", "returning", "subscriber", "vip", "moderator")}
    messages = {key: 0 for key in counts}
    for account_id, _user_id, message_count, first_message, subscriber, vip, moderator in rows:
        prior = connection.execute(
            """SELECT 1 FROM messages WHERE community_id=? AND platform_account_id=?
               AND datetime(sent_at)<datetime(?) LIMIT 1""",
            (int(session[0]), int(account_id), str(session[1])),
        ).fetchone()
        cohort_keys = ["unique", "returning" if prior else "new"]
        if int(subscriber or 0): cohort_keys.append("subscriber")
        if int(vip or 0): cohort_keys.append("vip")
        if int(moderator or 0): cohort_keys.append("moderator")
        for key in cohort_keys:
            counts[key] += 1
            messages[key] += int(message_count or 0)
    with connection:
        connection.executemany(
            """INSERT INTO stream_cohort_snapshots(stream_session_id,cohort_key,member_count,message_count)
               VALUES (?,?,?,?) ON CONFLICT(stream_session_id,cohort_key) DO UPDATE SET
                   member_count=excluded.member_count,message_count=excluded.message_count,
                   calculated_at=CURRENT_TIMESTAMP""",
            ((int(stream_session_id), key, counts[key], messages[key]) for key in counts),
        )
    return counts


def moderator_workload_report(
    connection: sqlite3.Connection, *, community_id: int, days: int = 7
) -> dict[str, Any]:
    operators = [dict(row) for row in connection.execute(
        """SELECT oa.id,oa.discord_username,
                  COUNT(DISTINCT ma.id) AS actions,
                  COUNT(DISTINCT CASE WHEN ma.action_type='warn' THEN ma.id END) AS warnings,
                  COUNT(DISTINCT CASE WHEN ma.action_type='timeout' THEN ma.id END) AS timeouts,
                  COUNT(DISTINCT CASE WHEN ma.action_type='ban' THEN ma.id END) AS bans,
                  COUNT(DISTINCT CASE WHEN rq.resolved_by_operator_id=oa.id THEN rq.id END) AS reviews,
                  COUNT(DISTINCT CASE WHEN oi.assigned_operator_id=oa.id THEN oi.id END) AS incidents
           FROM operator_accounts oa
           LEFT JOIN moderation_actions ma ON ma.actor_type='operator' AND ma.actor_id=oa.id
                AND ma.created_at>=datetime('now',?)
                AND EXISTS (SELECT 1 FROM messages scope_message
                            WHERE scope_message.id=ma.message_id AND scope_message.community_id=?)
           LEFT JOIN review_queue rq ON rq.resolved_by_operator_id=oa.id AND rq.resolved_at>=datetime('now',?)
                AND EXISTS (SELECT 1 FROM messages scope_review
                            WHERE scope_review.id=rq.message_id AND scope_review.community_id=?)
           LEFT JOIN operations_incidents oi ON oi.assigned_operator_id=oa.id
                AND oi.community_id=? AND oi.updated_at>=datetime('now',?)
           GROUP BY oa.id,oa.discord_username ORDER BY actions DESC,reviews DESC""",
        (f"-{max(1,int(days))} days", int(community_id),
         f"-{max(1,int(days))} days", int(community_id), int(community_id),
         f"-{max(1,int(days))} days"),
    ).fetchall()]
    action_mix = [dict(row) for row in connection.execute(
        """SELECT action_type,COUNT(*) AS count,
                  ROUND(AVG(duration_seconds),1) AS average_duration_seconds,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures,
                  SUM(CASE WHEN provider_confirmed_at IS NOT NULL THEN 1 ELSE 0 END) AS provider_confirmations
           FROM moderation_actions ma JOIN messages m ON m.id=ma.message_id
           WHERE m.community_id=? AND ma.created_at>=datetime('now',?)
           GROUP BY action_type ORDER BY count DESC""",
        (int(community_id), f"-{max(1,int(days))} days"),
    ).fetchall()]
    total = sum(int(row["actions"] or 0) for row in operators)
    shares = [int(row["actions"] or 0) / total for row in operators if total]
    consistency = 1.0 - (max(shares) - min(shares)) if len(shares) > 1 else 1.0
    mixes = [
        [int(row[key] or 0) / int(row["actions"]) for key in ("warnings", "timeouts", "bans")]
        for row in operators if int(row["actions"] or 0) > 0
    ]
    if len(mixes) > 1:
        enforcement_consistency = 1.0 - sum(
            max(mix[index] for mix in mixes) - min(mix[index] for mix in mixes)
            for index in range(3)
        ) / 3.0
    else:
        enforcement_consistency = 1.0
    return {"window_days": max(1, int(days)), "operators": operators,
            "action_mix": action_mix, "workload_balance_score": round(max(0.0, consistency), 3),
            "enforcement_consistency_score": round(max(0.0, enforcement_consistency), 3)}


def generate_post_stream_briefing(connection: sqlite3.Connection, stream_session_id: int) -> int:
    session = connection.execute(
        """SELECT id,community_id,title,started_at,COALESCE(ended_at,CURRENT_TIMESTAMP),stream_key
           FROM stream_sessions WHERE id=?""",
        (int(stream_session_id),),
    ).fetchone()
    if session is None:
        raise ValueError("stream session was not found")
    cohorts = refresh_stream_cohorts(connection, int(stream_session_id))
    metrics = connection.execute(
        """SELECT COUNT(*) AS messages,COUNT(DISTINCT platform_account_id) AS unique_chatters,
                  COUNT(DISTINCT channel_id) AS channels
           FROM messages WHERE community_id=? AND datetime(sent_at)>=datetime(?)
             AND datetime(sent_at)<=datetime(?)""",
        (int(session[1]), str(session[3]), str(session[4])),
    ).fetchone()
    incidents = [dict(row) for row in connection.execute(
        """SELECT id,incident_type,severity,status,title,summary,escalation_level
           FROM operations_incidents WHERE stream_session_id=? ORDER BY opened_at""",
        (int(stream_session_id),),
    ).fetchall()]
    actions = int(connection.execute(
        """SELECT COUNT(*) FROM moderation_actions ma JOIN messages m ON m.id=ma.message_id
           WHERE m.community_id=? AND datetime(ma.created_at)>=datetime(?)
             AND datetime(ma.created_at)<=datetime(?)""",
        (int(session[1]), str(session[3]), str(session[4])),
    ).fetchone()[0])
    duration_minutes = max(1.0, (
        _parse_time(str(session[4])) - _parse_time(str(session[3]))
    ).total_seconds() / 60.0)
    metric_payload = {
        "messages": int(metrics[0] or 0), "unique_chatters": int(metrics[1] or 0),
        "channels": int(metrics[2] or 0), "moderation_actions": actions,
        "duration_minutes": round(duration_minutes, 1),
        "average_messages_per_minute": round(int(metrics[0] or 0) / duration_minutes, 2),
    }
    recommendations: list[str] = []
    if incidents:
        recommendations.append("Review incident dispositions and preserve evidence for any escalated campaign.")
    if actions > max(10, int(metrics[0] or 0) * 0.05):
        recommendations.append("Review rule precision: enforcement volume exceeded five percent of chat messages.")
    if cohorts.get("new", 0) > cohorts.get("returning", 0):
        recommendations.append("Prioritize new-viewer onboarding and compare next-stream retention.")
    summary = (
        f"{metric_payload['unique_chatters']} unique chatters produced {metric_payload['messages']} messages "
        f"over {metric_payload['duration_minutes']} minutes. {len(incidents)} incidents and {actions} "
        "moderation actions were recorded."
    )
    with connection:
        connection.execute(
            """INSERT INTO post_stream_briefings(
                   stream_session_id,community_id,title,executive_summary,metrics_json,
                   incidents_json,cohorts_json,recommendations_json
               ) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(stream_session_id) DO UPDATE SET executive_summary=excluded.executive_summary,
                   metrics_json=excluded.metrics_json,incidents_json=excluded.incidents_json,
                   cohorts_json=excluded.cohorts_json,recommendations_json=excluded.recommendations_json,
                   generated_at=CURRENT_TIMESTAMP""",
            (int(session[0]), int(session[1]), f"Post-stream briefing: {session[2] or session[5]}",
             summary, json.dumps(metric_payload, sort_keys=True), json.dumps(incidents, sort_keys=True),
             json.dumps(cohorts, sort_keys=True), json.dumps(recommendations, sort_keys=True)),
        )
        row = connection.execute(
            "SELECT id FROM post_stream_briefings WHERE stream_session_id=?", (int(session[0]),)
        ).fetchone()
    assert row is not None
    return int(row[0])


def queue_incident_notifications(
    connection: sqlite3.Connection, incident_id: int, *, force: bool = False
) -> int:
    incident = connection.execute(
        """SELECT id,community_id,severity,title,summary,status,escalation_level
           FROM operations_incidents WHERE id=?""",
        (int(incident_id),),
    ).fetchone()
    if incident is None:
        return 0
    destinations = connection.execute(
        """SELECT id,minimum_severity FROM notification_destinations
           WHERE community_id=? AND enabled=1""",
        (int(incident[1]),),
    ).fetchall()
    count = 0
    for destination in destinations:
        if not force and SEVERITY_RANK.get(str(incident[2]), 0) < SEVERITY_RANK.get(str(destination[1]), 3):
            continue
        payload = {"incident_id": int(incident[0]), "severity": str(incident[2]),
                   "title": str(incident[3]), "summary": str(incident[4]),
                   "status": str(incident[5]), "escalation_level": int(incident[6])}
        cursor = connection.execute(
            """INSERT INTO notification_deliveries(destination_id,incident_id,payload_json)
               SELECT ?,?,? WHERE NOT EXISTS (
                   SELECT 1 FROM notification_deliveries WHERE destination_id=? AND incident_id=?
                     AND status IN ('pending','delivered')
               )""",
            (int(destination[0]), int(incident_id), json.dumps(payload, sort_keys=True),
             int(destination[0]), int(incident_id)),
        )
        count += max(0, cursor.rowcount)
    return count


def create_notification_destination(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    destination_type: str,
    name: str,
    target: str,
    minimum_severity: str = "high",
) -> int:
    normalized_type = destination_type.strip().casefold()
    severity = minimum_severity.strip().casefold()
    if normalized_type not in {"discord_webhook", "slack_webhook", "generic_webhook"}:
        raise ValueError("unsupported notification destination type")
    if not target.strip().startswith("https://"):
        raise ValueError("notification destinations must use HTTPS")
    if severity not in SEVERITY_RANK:
        raise ValueError("unsupported minimum severity")
    with connection:
        cursor = connection.execute(
            """INSERT INTO notification_destinations(
                   community_id,destination_type,name,target,minimum_severity
               ) VALUES (?,?,?,?,?)""",
            (int(community_id), normalized_type, name.strip(), target.strip(), severity),
        )
    return int(cursor.lastrowid)


def dispatch_pending_notifications(
    connection: sqlite3.Connection, *, tenant: TenantContext, limit: int = 25
) -> int:
    rows = connection.execute(
        """SELECT d.id,d.payload_json,n.destination_type,n.target
           FROM notification_deliveries d JOIN notification_destinations n ON n.id=d.destination_id
              WHERE d.status IN ('pending','retry') AND n.enabled=1 AND d.attempts<5
                 AND n.community_id=?
           ORDER BY d.created_at LIMIT ?""",
          (tenant.community_id, max(1, int(limit))),
    ).fetchall()
    delivered = 0
    for row in rows:
        payload = _json_object(row[1])
        destination_type = str(row[2])
        if destination_type == "discord_webhook":
            wire_payload: Mapping[str, object] = {
                "content": f"[{payload.get('severity','high').upper()}] {payload.get('title','Incident')}",
                "embeds": [{"description": str(payload.get("summary") or "")[:4000],
                            "fields": [{"name": "Incident", "value": str(payload.get("incident_id") or "unknown")}]}],
            }
        elif destination_type == "slack_webhook":
            wire_payload = {"text": f"[{payload.get('severity','high').upper()}] {payload.get('title','Incident')}\n{payload.get('summary','')}"}
        else:
            wire_payload = payload
        request = Request(
            str(row[3]), data=json.dumps(wire_payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "qbot4k/2.0"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                response.read(4096)
                provider_status = int(response.status)
            if provider_status < 200 or provider_status >= 300:
                raise RuntimeError(f"notification provider returned HTTP {provider_status}")
        except (HTTPError, URLError, RuntimeError) as exc:
            with connection:
                connection.execute(
                    """UPDATE notification_deliveries SET status='retry',attempts=attempts+1,
                           last_error=? WHERE id=?""",
                    (str(exc)[:1000], int(row[0])),
                )
            continue
        with connection:
            connection.execute(
                """UPDATE notification_deliveries SET status='delivered',attempts=attempts+1,
                       delivered_at=CURRENT_TIMESTAMP,last_error=NULL WHERE id=?""",
                (int(row[0]),),
            )
        delivered += 1
    return delivered


def _incident_activity(
    connection: sqlite3.Connection,
    incident_id: int,
    operator_id: int | None,
    activity_type: str,
    body: str,
    payload: Mapping[str, object],
) -> None:
    connection.execute(
        """INSERT INTO incident_activity(incident_id,operator_id,activity_type,body,payload_json)
           VALUES (?,?,?,?,?)""",
        (int(incident_id), operator_id, activity_type, body.strip(), json.dumps(dict(payload), sort_keys=True)),
    )


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
