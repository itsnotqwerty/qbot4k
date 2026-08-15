from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


PROFILE_VERSION = 1


@dataclass(frozen=True)
class CommunityProfile:
    community_id: int
    user_id: int
    trust: float
    risk: float
    engagement: float
    identity_confidence: float
    maturity: float
    confidence: float
    evidence_count: int


def refresh_community_profile(
    connection: sqlite3.Connection, *, community_id: int, user_id: int
) -> CommunityProfile:
    row = connection.execute(
        """SELECT COUNT(*) AS messages,
                  COUNT(DISTINCT m.channel_id) AS channels,
                  COUNT(DISTINCT m.platform) AS platforms,
                  SUM(CASE WHEN rm.severity IN ('high','critical') THEN 1 ELSE 0 END) AS severe,
                  SUM(CASE WHEN a.status='completed' THEN 1 ELSE 0 END) AS actions,
                  MIN(m.sent_at) AS first_seen, MAX(m.sent_at) AS last_seen
           FROM messages m
           LEFT JOIN rule_matches rm ON rm.message_id=m.id
           LEFT JOIN moderation_actions a ON a.message_id=m.id AND a.user_id=m.user_id
           WHERE m.community_id=? AND m.user_id=?""",
        (int(community_id), int(user_id)),
    ).fetchone()
    messages = int(row[0] or 0)
    channels = int(row[1] or 0)
    observed_platforms = int(row[2] or 0)
    severe = int(row[3] or 0)
    actions = int(row[4] or 0)
    account_count = int(connection.execute(
        "SELECT COUNT(*) FROM platform_accounts WHERE user_id=?", (int(user_id),)
    ).fetchone()[0])
    engagement = min(100.0, 18.0 * math.log1p(messages) + channels * 3.0)
    risk = min(100.0, severe * 22.0 + actions * 18.0)
    trust = max(0.0, min(100.0, 55.0 + min(messages, 100) * 0.25 - risk * 0.65))
    identity = min(100.0, 35.0 + account_count * 25.0 + observed_platforms * 10.0)
    age_days = 0.0
    if row[5]:
        first = datetime.fromisoformat(str(row[5]).replace("Z", "+00:00"))
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400.0)
    maturity = min(100.0, math.log1p(age_days) * 18.0 + min(messages, 50))
    evidence_count = messages + severe + actions
    confidence = min(1.0, evidence_count / 30.0)
    profile = CommunityProfile(int(community_id), int(user_id), trust, risk, engagement,
                               identity, maturity, confidence, evidence_count)
    explanation = {
        "message_count": messages, "channel_count": channels,
        "severe_findings": severe, "completed_actions": actions,
        "linked_accounts": account_count, "first_seen": row[5], "last_seen": row[6],
    }
    with connection:
        connection.execute(
            """INSERT INTO community_intelligence_profiles(
                   community_id,user_id,profile_version,trust_score,risk_score,
                   engagement_score,identity_confidence,maturity_score,confidence,
                   evidence_count,explanation_json,calculated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(community_id,user_id,profile_version) DO UPDATE SET
                   trust_score=excluded.trust_score,risk_score=excluded.risk_score,
                   engagement_score=excluded.engagement_score,
                   identity_confidence=excluded.identity_confidence,
                   maturity_score=excluded.maturity_score,confidence=excluded.confidence,
                   evidence_count=excluded.evidence_count,
                   explanation_json=excluded.explanation_json,
                   calculated_at=excluded.calculated_at""",
            (profile.community_id, profile.user_id, PROFILE_VERSION, profile.trust,
             profile.risk, profile.engagement, profile.identity_confidence,
             profile.maturity, profile.confidence, profile.evidence_count,
             json.dumps(explanation, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
    return profile
