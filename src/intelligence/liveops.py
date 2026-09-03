from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .professional_ops import moderator_workload_report


def live_operations_snapshot(connection: sqlite3.Connection, *, community_id: int) -> dict[str, Any]:
    community = connection.execute(
        "SELECT id,name,slug FROM communities WHERE id=?", (int(community_id),)
    ).fetchone()
    if community is None:
        raise ValueError(f"community {community_id} was not found")
    live_sessions = [dict(row) for row in connection.execute(
        """SELECT id,platform,stream_key,external_stream_id,title,category,status,
                  started_at,ended_at,updated_at
           FROM stream_sessions WHERE community_id=? AND status='live'
           ORDER BY started_at DESC LIMIT 20""",
        (int(community_id),),
    ).fetchall()]
    if not live_sessions:
        live_sessions = [dict(row) for row in connection.execute(
            """SELECT NULL AS id,platform,stream_key,NULL AS external_stream_id,title,category,
                      status,started_at,ended_at,updated_at
               FROM stream_states WHERE status='live' ORDER BY updated_at DESC LIMIT 20"""
        ).fetchall()]
    metrics = connection.execute(
        """SELECT COUNT(*) AS messages,COUNT(DISTINCT platform_account_id) AS chatters,
                  COUNT(DISTINCT channel_id) AS channels
           FROM messages WHERE community_id=? AND datetime(sent_at)>=datetime('now','-5 minutes')""",
        (int(community_id),),
    ).fetchone()
    velocity = _velocity_series(connection, int(community_id), minutes=30)
    alerts = [dict(row) for row in connection.execute(
        """SELECT a.id,a.observation_id,a.alert_type,a.severity,a.status,a.title,a.summary,
                  a.confidence,a.assigned_operator_id,a.created_at,
                  COALESCE(u.primary_display_name,pa.username,'System') AS subject,
                  m.id AS message_id,m.platform_account_id
           FROM intelligence_alerts a
           LEFT JOIN users u ON u.id=a.user_id
           LEFT JOIN observations o ON o.id=a.observation_id
           LEFT JOIN platform_accounts pa ON pa.id=o.actor_platform_account_id
           LEFT JOIN messages m ON m.observation_id=a.observation_id
           WHERE a.community_id=? AND a.status IN ('open','triaged','acknowledged','in_case')
             AND (a.suppressed_until IS NULL OR a.suppressed_until<CURRENT_TIMESTAMP)
           ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END,a.created_at DESC LIMIT 50""",
        (int(community_id),),
    ).fetchall()]
    incidents = [dict(row) for row in connection.execute(
        """SELECT oi.id,oi.incident_type,oi.severity,oi.status,oi.title,oi.summary,
                  oi.escalation_level,oi.assigned_operator_id,oa.discord_username AS assignee,
                  oi.playbook_key,oi.campaign_id,oi.opened_at,oi.updated_at,
                  COALESCE(c.message_count,COUNT(ia.alert_id)) AS finding_count,
                  COALESCE(c.actor_count,0) AS actor_count,COALESCE(c.confidence,0) AS confidence
           FROM operations_incidents oi
           LEFT JOIN operator_accounts oa ON oa.id=oi.assigned_operator_id
           LEFT JOIN coordination_campaigns c ON c.id=oi.campaign_id
           LEFT JOIN incident_alerts ia ON ia.incident_id=oi.id
           WHERE oi.community_id=? AND oi.status IN ('open','active','monitoring')
           GROUP BY oi.id ORDER BY CASE oi.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                    oi.updated_at DESC LIMIT 30""",
        (int(community_id),),
    ).fetchall()]
    active_session_id = live_sessions[0]["id"] if live_sessions and live_sessions[0]["id"] else None
    timeline = _stream_timeline(connection, int(community_id), active_session_id)
    action_counts = connection.execute(
        """SELECT SUM(CASE WHEN ma.status='pending' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ma.status='failed' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN ma.provider_confirmed_at IS NOT NULL THEN 1 ELSE 0 END)
           FROM moderation_actions ma LEFT JOIN messages m ON m.id=ma.message_id
           WHERE COALESCE(m.community_id,?)=? AND ma.created_at>=datetime('now','-1 day')""",
        (int(community_id), int(community_id)),
    ).fetchone()
    open_reviews = int(connection.execute(
        """SELECT COUNT(*) FROM review_queue q JOIN messages m ON m.id=q.message_id
           WHERE q.status='open' AND m.community_id=?""",
        (int(community_id),),
    ).fetchone()[0])
    dead_letters = int(connection.execute(
        "SELECT COUNT(*) FROM dead_letter_events WHERE community_id=? AND status='open'",
        (int(community_id),),
    ).fetchone()[0])
    controls = [dict(row) for row in connection.execute(
        """SELECT id,control_type,status,provider_status,requested_json,requested_at,confirmed_at,error_message
           FROM twitch_control_actions WHERE community_id=? ORDER BY requested_at DESC LIMIT 12""",
        (int(community_id),),
    ).fetchall()]
    destinations = [dict(row) for row in connection.execute(
        """SELECT id,destination_type,name,minimum_severity,enabled,created_at
           FROM notification_destinations WHERE community_id=? ORDER BY name""",
        (int(community_id),),
    ).fetchall()]
    playbooks = [dict(row) for row in connection.execute(
        """SELECT playbook_key,name,description,severity,steps_json
           FROM raid_playbooks WHERE enabled=1 ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,name"""
    ).fetchall()]
    briefings = [dict(row) for row in connection.execute(
        """SELECT id,stream_session_id,status,title,executive_summary,metrics_json,cohorts_json,
                  recommendations_json,generated_at FROM post_stream_briefings
           WHERE community_id=? ORDER BY generated_at DESC LIMIT 5""",
        (int(community_id),),
    ).fetchall()]
    audience_graph = [dict(row) for row in connection.execute(
        """SELECT source_key,target_key,edge_type,weight,event_count,last_observed_at
           FROM audience_edges WHERE community_id=? ORDER BY weight DESC,last_observed_at DESC LIMIT 40""",
        (int(community_id),),
    ).fetchall()]
    cohorts = {}
    if active_session_id is not None:
        cohorts = {str(row[0]): {"members": int(row[1]), "messages": int(row[2])}
                   for row in connection.execute(
                       """SELECT cohort_key,member_count,message_count FROM stream_cohort_snapshots
                          WHERE stream_session_id=?""", (int(active_session_id),)).fetchall()}
    messages = int(metrics[0] or 0)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watermark": _watermark(connection, int(community_id)),
        "community": dict(community), "live_streams": live_sessions,
        "last_5_minutes": {
            "messages": messages, "unique_chatters": int(metrics[1] or 0),
            "channels": int(metrics[2] or 0), "messages_per_minute": round(messages / 5.0, 1),
            "current_velocity": velocity[-1]["messages"] if velocity else 0,
        },
        "velocity": velocity, "timeline": timeline, "open_alerts": alerts,
        "active_incidents": incidents, "active_campaigns": incidents,
        "controls": controls, "playbooks": playbooks, "briefings": briefings,
        "notification_destinations": destinations,
        "cohorts": cohorts, "audience_graph": audience_graph,
        "moderator_workload": moderator_workload_report(connection, community_id=int(community_id), days=7),
        "operations": {
            "pending_actions": int(action_counts[0] or 0),
            "failed_actions": int(action_counts[1] or 0),
            "provider_confirmed_actions": int(action_counts[2] or 0),
            "open_reviews": open_reviews, "dead_letters": dead_letters,
        },
    }


def _velocity_series(connection: sqlite3.Connection, community_id: int, *, minutes: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    values = {str(row[0]): (int(row[1]), int(row[2])) for row in connection.execute(
        """SELECT strftime('%Y-%m-%dT%H:%M:00+00:00',sent_at) AS bucket,
                  COUNT(*),COUNT(DISTINCT platform_account_id)
           FROM messages WHERE community_id=? AND datetime(sent_at)>=datetime('now',?)
           GROUP BY bucket""",
        (int(community_id), f"-{max(1,int(minutes))} minutes"),
    ).fetchall()}
    result = []
    for offset in range(max(1, int(minutes)) - 1, -1, -1):
        bucket = (now - timedelta(minutes=offset)).isoformat()
        messages, chatters = values.get(bucket, (0, 0))
        result.append({"minute": bucket, "messages": messages, "unique_chatters": chatters})
    return result


def _stream_timeline(
    connection: sqlite3.Connection, community_id: int, session_id: int | None
) -> list[dict[str, Any]]:
    if session_id is not None:
        session = connection.execute(
            "SELECT started_at,COALESCE(ended_at,CURRENT_TIMESTAMP) FROM stream_sessions WHERE id=?",
            (int(session_id),),
        ).fetchone()
        start, end = str(session[0]), str(session[1])
    else:
        start, end = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(), None
    rows = connection.execute(
        """SELECT id,event_type,occurred_at,text_raw,attributes_json
           FROM observations WHERE community_id=? AND datetime(occurred_at)>=datetime(?)
             AND (? IS NULL OR datetime(occurred_at)<=datetime(?))
             AND (event_type LIKE 'stream.%' OR event_type LIKE 'moderation.%'
                  OR event_type IN ('channel.raided','channel.shared_chat','channel.shield_mode'))
           ORDER BY occurred_at,id LIMIT 150""",
        (int(community_id), start, end, end),
    ).fetchall()
    return [{"observation_id": int(row[0]), "event_type": str(row[1]),
             "occurred_at": str(row[2]), "text": str(row[3] or ""),
             "attributes": _safe_json(row[4])} for row in rows]


def _watermark(connection: sqlite3.Connection, community_id: int) -> str:
    row = connection.execute(
        """SELECT
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),COALESCE(MAX(ingested_at),''))
                FROM observations WHERE community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),COALESCE(MAX(updated_at),''))
                FROM intelligence_alerts WHERE community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),COALESCE(MAX(updated_at),''))
                FROM operations_incidents WHERE community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(ma.id),0),
                     COALESCE(MAX(COALESCE(ma.provider_confirmed_at,ma.completed_at,ma.created_at)),''))
                FROM moderation_actions ma LEFT JOIN messages m ON m.id=ma.message_id
                WHERE m.community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),
                     COALESCE(MAX(COALESCE(confirmed_at,requested_at)),''))
                FROM twitch_control_actions WHERE community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),COALESCE(MAX(generated_at),''))
                FROM post_stream_briefings WHERE community_id=?),
             (SELECT printf('%d:%s',COALESCE(MAX(id),0),COALESCE(MAX(updated_at),''))
                FROM notification_destinations WHERE community_id=?)""",
        (community_id,) * 7,
    ).fetchone()
    return "|".join(str(value or "0") for value in row)


def _safe_json(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
