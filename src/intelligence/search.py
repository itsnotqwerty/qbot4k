from __future__ import annotations

import json
import sqlite3
from typing import Mapping


def search_observations(
    connection: sqlite3.Connection, *, community_id: int, query: str = "", start_at: str | None = None,
    end_at: str | None = None, platform: str | None = None, event_type: str | None = None,
    user_id: int | None = None, container_id: str | None = None, context_id: str | None = None,
    entity_type: str | None = None, entity_value: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict[str, object]]:
    where: list[str] = ["o.community_id = ?"]
    params: list[object] = [community_id]
    use_fts = bool(query.strip())
    source = "observation_fts JOIN observations AS o ON o.id=observation_fts.rowid" if use_fts else "observations AS o"
    if use_fts:
        where.append("observation_fts MATCH ?")
        params.append(query.strip())
    filters = (
        ("o.occurred_at >= ?", start_at), ("o.occurred_at <= ?", end_at),
        ("o.platform = ?", platform), ("o.event_type = ?", event_type),
        ("o.container_id = ?", container_id), ("o.context_id = ?", context_id),
    )
    for clause, value in filters:
        if value not in (None, ""):
            where.append(clause)
            params.append(value)
    if user_id is not None:
        where.append("(actor.user_id = ? OR target.user_id = ?)")
        params.extend((user_id, user_id))
    if entity_type:
        where.append("EXISTS (SELECT 1 FROM content_entities ce WHERE ce.observation_id=o.id AND ce.entity_type=?)")
        params.append(entity_type)
    if entity_value:
        where.append("EXISTS (SELECT 1 FROM content_entities ce WHERE ce.observation_id=o.id AND ce.normalized_value=?)")
        params.append(entity_value.casefold())
    rank = "bm25(observation_fts)" if use_fts else "0.0"
    sql = f"""
        SELECT o.id, o.platform, o.event_type, o.external_event_id, o.container_id,
               o.context_id, o.text_raw, o.occurred_at, actor.user_id AS actor_user_id,
               target.user_id AS target_user_id, ca.language_code, ca.sentiment_label,
               ca.intent_label, ca.threat_level, {rank} AS rank
        FROM {source}
        LEFT JOIN platform_accounts actor ON actor.id=o.actor_platform_account_id
        LEFT JOIN platform_accounts target ON target.id=o.target_platform_account_id
        LEFT JOIN content_analysis ca ON ca.observation_id=o.id
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY rank ASC, o.occurred_at DESC LIMIT ? OFFSET ?
    """
    params.extend((max(1, min(int(limit), 500)), max(0, int(offset))))
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def save_query(connection: sqlite3.Connection, name: str, query_text: str, filters: Mapping[str, object], *, operator_id: int | None = None) -> int:
    if not name.strip():
        raise ValueError("saved query name must not be empty")
    effective_operator_id = operator_id if operator_id is not None else 0
    connection.execute(
        """INSERT INTO saved_queries(operator_id, name, query_text, filters_json)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(operator_id, name) DO UPDATE SET query_text=excluded.query_text,
             filters_json=excluded.filters_json, updated_at=CURRENT_TIMESTAMP""",
        (effective_operator_id, name.strip(), query_text.strip(), json.dumps(dict(filters), sort_keys=True)),
    )
    row = connection.execute("SELECT id FROM saved_queries WHERE operator_id = ? AND name=?", (effective_operator_id, name.strip())).fetchone()
    return int(row[0])


def list_saved_queries(connection: sqlite3.Connection, *, operator_id: int | None = None) -> list[dict[str, object]]:
    effective_operator_id = operator_id if operator_id is not None else 0
    rows = connection.execute(
        "SELECT * FROM saved_queries WHERE operator_id = ? ORDER BY name", (effective_operator_id,)
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["filters"] = json.loads(str(item.pop("filters_json")))
        result.append(item)
    return result


def observation_pivots(
    connection: sqlite3.Connection, observation_id: int, *, community_id: int
) -> dict[str, object]:
    row = connection.execute(
        """SELECT o.*, actor.user_id actor_user_id, target.user_id target_user_id
           FROM observations o
           LEFT JOIN platform_accounts actor ON actor.id=o.actor_platform_account_id
           LEFT JOIN platform_accounts target ON target.id=o.target_platform_account_id
           WHERE o.id=? AND o.community_id=?""", (observation_id, community_id),
    ).fetchone()
    if row is None:
        raise ValueError("observation not found")
    entities = [dict(item) for item in connection.execute(
        "SELECT entity_type, entity_value, normalized_value, confidence FROM content_entities WHERE observation_id=? ORDER BY entity_type",
        (observation_id,),
    ).fetchall()]
    pivots: dict[str, object] = {"observation_id": observation_id, "entities": entities}
    for key in ("actor_user_id", "target_user_id", "platform", "event_type", "container_id", "context_id"):
        if row[key] not in (None, ""):
            pivots[key] = row[key]
    pivots["related_observation_count"] = int(connection.execute(
        """SELECT COUNT(DISTINCT o2.id) FROM observations o2
           LEFT JOIN platform_accounts a2 ON a2.id=o2.actor_platform_account_id
           LEFT JOIN platform_accounts t2 ON t2.id=o2.target_platform_account_id
              WHERE o2.id<>? AND o2.community_id=?
                 AND (a2.user_id IN (?, ?) OR t2.user_id IN (?, ?) OR o2.context_id=?)""",
          (observation_id, community_id, row["actor_user_id"], row["target_user_id"], row["actor_user_id"], row["target_user_id"], row["context_id"]),
    ).fetchone()[0])
    filters: dict[str, object] = {}
    for key in ("platform", "event_type", "container_id", "context_id"):
        if row[key] not in (None, ""):
            filters[key] = row[key]
    pivots["search_links"] = {
        "actor": {"user_id": row["actor_user_id"]} if row["actor_user_id"] is not None else None,
        "target": {"user_id": row["target_user_id"]} if row["target_user_id"] is not None else None,
        "context": {"context_id": row["context_id"]} if row["context_id"] else None,
        "container": {"container_id": row["container_id"]} if row["container_id"] else None,
        "event": filters,
        "entities": [
            {"entity_type": item["entity_type"], "entity_value": item["normalized_value"]}
            for item in entities
        ],
    }
    return pivots
