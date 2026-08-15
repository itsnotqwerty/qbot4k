from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from urllib.parse import urlparse
from .professional_ops import upsert_campaign_incident


_TOKEN = re.compile(r"[a-z0-9]{3,}")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def analyze_coordination_campaign(
    connection: sqlite3.Connection, observation_id: int, *, window_minutes: int = 15
) -> int | None:
    current = connection.execute(
        """SELECT o.id,o.community_id,o.text_raw,o.occurred_at,pa.user_id
           FROM observations o LEFT JOIN platform_accounts pa ON pa.id=o.actor_platform_account_id
           WHERE o.id=? AND o.event_type='message.created'""",
        (int(observation_id),),
    ).fetchone()
    if current is None or not str(current[2] or "").strip():
        return None
    tokens, domains = _features(str(current[2]))
    if len(tokens) < 4 and not domains:
        return None
    candidates = connection.execute(
        """SELECT o.id,o.text_raw,pa.user_id FROM observations o
           LEFT JOIN platform_accounts pa ON pa.id=o.actor_platform_account_id
           WHERE o.community_id=? AND o.event_type='message.created' AND o.id<>?
             AND o.occurred_at >= datetime(?, ?)
           ORDER BY o.occurred_at DESC LIMIT 250""",
        (int(current[1]), int(current[0]), str(current[3]), f"-{int(window_minutes)} minutes"),
    ).fetchall()
    matches: list[tuple[int, int | None, float]] = [(int(current[0]), current[4], 1.0)]
    for candidate in candidates:
        other_tokens, other_domains = _features(str(candidate[1] or ""))
        union = tokens | other_tokens
        similarity = len(tokens & other_tokens) / len(union) if union else 0.0
        if domains & other_domains:
            similarity = max(similarity, 0.75)
        if similarity >= 0.72:
            matches.append((int(candidate[0]), candidate[2], similarity))
    actors = {int(item[1]) for item in matches if item[1] is not None}
    if len(matches) < 3 or len(actors) < 2:
        return None
    key_material = "|".join(sorted(domains) or sorted(tokens)[:12])
    campaign_key = hashlib.sha256(key_material.encode()).hexdigest()[:24]
    confidence = min(0.99, 0.55 + len(matches) * 0.06 + len(actors) * 0.04)
    severity = "high" if len(actors) >= 5 or len(matches) >= 10 else "medium"
    with connection:
        connection.execute(
            """INSERT INTO coordination_campaigns(
                   community_id,campaign_key,campaign_type,severity,message_count,
                   actor_count,confidence,first_observed_at,last_observed_at,details_json
               ) VALUES (?,?,'near_duplicate',?,?,?,?,?,?,?)
               ON CONFLICT(community_id,campaign_key) DO UPDATE SET
                   severity=excluded.severity,message_count=MAX(message_count,excluded.message_count),
                   actor_count=MAX(actor_count,excluded.actor_count),confidence=MAX(confidence,excluded.confidence),
                   last_observed_at=excluded.last_observed_at,details_json=excluded.details_json""",
            (int(current[1]), campaign_key, severity, len(matches), len(actors), confidence,
             str(current[3]), str(current[3]), json.dumps({"domains": sorted(domains), "tokens": sorted(tokens)[:20]})),
        )
        campaign = connection.execute(
            "SELECT id FROM coordination_campaigns WHERE community_id=? AND campaign_key=?",
            (int(current[1]), campaign_key),
        ).fetchone()
        assert campaign is not None
        campaign_id = int(campaign[0])
        connection.executemany(
            """INSERT OR IGNORE INTO coordination_campaign_members(
                   campaign_id,observation_id,user_id,similarity) VALUES (?,?,?,?)""",
            ((campaign_id, item[0], item[1], item[2]) for item in matches),
        )
    upsert_campaign_incident(connection, campaign_id)
    return campaign_id


def _features(text: str) -> tuple[set[str], set[str]]:
    tokens = set(_TOKEN.findall(text.casefold()))
    domains: set[str] = set()
    for raw_url in _URL.findall(text):
        domain = (urlparse(raw_url).hostname or "").casefold().removeprefix("www.")
        if domain:
            domains.add(domain)
    return tokens, domains
