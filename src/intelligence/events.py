from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from ..db import collect_observation, connect_database, get_observation, initialize_database
from ..models import CollectionResult, Observation, coerce_timestamp
from .content import analyze_observation_content


SUPPORTED_EVENT_TYPES = (
    "message.edited", "message.deleted", "member.joined", "member.left",
    "reaction.added", "reaction.removed", "member.roles_changed",
    "moderation.action", "moderation.ban_added", "moderation.ban_removed",
    "stream.started", "stream.ended", "stream.updated", "account.updated", "external.item",
)

_DISCORD_EVENTS = {
    "MESSAGE_UPDATE": "message.edited", "MESSAGE_DELETE": "message.deleted",
    "GUILD_MEMBER_ADD": "member.joined", "GUILD_MEMBER_REMOVE": "member.left",
    "MESSAGE_REACTION_ADD": "reaction.added", "MESSAGE_REACTION_REMOVE": "reaction.removed",
    "GUILD_MEMBER_UPDATE": "member.roles_changed", "GUILD_BAN_ADD": "moderation.ban_added",
    "GUILD_BAN_REMOVE": "moderation.ban_removed", "USER_UPDATE": "account.updated",
}


def observation_from_discord_event(gateway_event: str, data: Mapping[str, object]) -> Observation | None:
    event_type = _DISCORD_EVENTS.get(gateway_event.strip().upper())
    if event_type is None:
        return None
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    author = data.get("author") if isinstance(data.get("author"), Mapping) else {}
    member = data.get("member") if isinstance(data.get("member"), Mapping) else {}
    member_user = member.get("user") if isinstance(member.get("user"), Mapping) else {}
    actor_id = str(data.get("user_id") or author.get("id") or "").strip() or None
    actor_name = str(author.get("username") or author.get("global_name") or actor_id or "").strip() or None
    target_id = None
    if event_type in {"member.joined", "member.left", "member.roles_changed", "moderation.ban_added", "moderation.ban_removed", "account.updated"}:
        target_id = str(user.get("id") or member_user.get("id") or data.get("id") or "").strip() or None
        if event_type == "account.updated":
            actor_id, target_id = target_id, None
            actor_name = str(user.get("username") or data.get("username") or actor_id or "").strip()
    payload_digest = hashlib.sha256(json.dumps(_json_safe(data), sort_keys=True).encode("utf-8")).hexdigest()[:16]
    event_id_parts = [gateway_event, str(data.get("id") or data.get("message_id") or target_id or actor_id or ""), str(data.get("channel_id") or data.get("guild_id") or ""), payload_digest]
    external_id = ":".join(event_id_parts)
    attributes = dict(data)
    text = str(data.get("content") or "") or None
    return Observation(
        platform="discord", event_type=event_type, external_event_id=external_id,
        actor_platform_user_id=actor_id, actor_username=actor_name,
        target_platform_user_id=target_id, container_id=str(data.get("channel_id") or "") or None,
        context_id=str(data.get("guild_id") or data.get("channel_id") or "") or None,
        text=text, occurred_at=coerce_timestamp(data.get("timestamp") if isinstance(data.get("timestamp"), (str, datetime)) else None),
        attributes={"gateway_event": gateway_event, **_json_safe(attributes)},
    )


def observation_from_twitch_irc_event(raw_line: str) -> Observation | None:
    line = raw_line.strip()
    if not line or " PRIVMSG " in line:
        return None
    tags, remainder = _parse_irc_tags(line)
    event_type = None
    if " JOIN #" in remainder:
        event_type = "member.joined"
        command = " JOIN #"
    elif " PART #" in remainder:
        event_type = "member.left"
        command = " PART #"
    elif " CLEARCHAT #" in remainder or " CLEARMSG #" in remainder:
        event_type = "moderation.action"
        command = " CLEARCHAT #" if " CLEARCHAT #" in remainder else " CLEARMSG #"
    elif " USERNOTICE #" in remainder:
        event_type = "account.updated"
        command = " USERNOTICE #"
    else:
        return None
    prefix = remainder[1:].split("!", 1)[0] if remainder.startswith(":") else ""
    tail = remainder.split(command, 1)[1]
    channel = tail.split(" ", 1)[0].split(" :", 1)[0].strip().lstrip("#")
    target = tail.split(" :", 1)[1].strip() if " :" in tail else ""
    actor_id = tags.get("user-id") or prefix or None
    target_id = tags.get("target-user-id") or target or None if event_type == "moderation.action" else None
    return Observation(
        platform="twitch", event_type=event_type,
        external_event_id=tags.get("id") or f"{event_type}:{channel}:{actor_id or target_id}:{tags.get('tmi-sent-ts', '')}",
        actor_platform_user_id=actor_id, actor_username=tags.get("display-name") or prefix or actor_id,
        target_platform_user_id=target_id, container_id=channel, context_id=channel,
        text=tags.get("system-msg") or None,
        occurred_at=_millisecond_timestamp(tags.get("tmi-sent-ts")), attributes={"irc_tags": tags},
    )


def collect_external_feed_item(
    connection: sqlite3.Connection, *, source_key: str, external_event_id: str,
    text: str, occurred_at: str | datetime | None = None, display_name: str | None = None,
    source_type: str = "api", actor_id: str | None = None, context_id: str | None = None,
    attributes: Mapping[str, object] | None = None, trust_weight: float = 0.5,
):
    key = source_key.strip().casefold()
    if not key or not external_event_id.strip():
        raise ValueError("source_key and external_event_id are required")
    connection.execute(
        """INSERT INTO external_feed_sources(source_key, display_name, source_type, trust_weight)
           VALUES (?, ?, ?, ?) ON CONFLICT(source_key) DO UPDATE SET display_name=excluded.display_name,
           source_type=excluded.source_type, trust_weight=excluded.trust_weight, updated_at=CURRENT_TIMESTAMP""",
        (key, (display_name or source_key).strip(), source_type.strip(), max(0.0, min(1.0, trust_weight))),
    )
    result = collect_observation(connection, Observation(
        platform=f"external:{key}", event_type="external.item", external_event_id=external_event_id.strip(),
        actor_platform_user_id=actor_id, actor_username=actor_id, context_id=context_id or key,
        text=text, occurred_at=coerce_timestamp(occurred_at), attributes=dict(attributes or {}),
    ))
    connection.execute("UPDATE external_feed_sources SET last_observed_at=? WHERE source_key=?", (coerce_timestamp(occurred_at), key))
    connection.commit()
    return result


class GenericEventAnalysisPipeline:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def analyze_event(self, connection: sqlite3.Connection, job) -> None:
        if job.observation_id is None:
            raise ValueError("event analysis requires an observation")
        observation = get_observation(connection, int(job.observation_id))
        if observation is None or str(observation["event_type"]) not in SUPPORTED_EVENT_TYPES:
            raise ValueError("unsupported or missing event observation")
        with connection:
            analyze_observation_content(connection, int(job.observation_id))
            _ensure_event_users(connection, observation)
            _upsert_event_relationship(connection, observation)


def ingest_event(database_path: Path, observation: Observation) -> CollectionResult:
    connection = connect_database(database_path)
    try:
        initialize_database(connection)
        result = collect_observation(connection, observation)
        return CollectionResult(status=result.status, platform=observation.platform,
                                observation_id=result.observation_id, analysis_job_id=result.analysis_job_id)
    finally:
        connection.close()


def _ensure_event_users(connection: sqlite3.Connection, observation: sqlite3.Row) -> None:
    for column in ("actor_platform_account_id", "target_platform_account_id"):
        account_id = observation[column]
        if account_id is None:
            continue
        row = connection.execute("SELECT user_id, username FROM platform_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None or row[0] is not None:
            continue
        cursor = connection.execute("INSERT INTO users(primary_display_name) VALUES (?)", (str(row[1]),))
        connection.execute("UPDATE platform_accounts SET user_id=? WHERE id=?", (int(cursor.lastrowid), int(account_id)))


def _upsert_event_relationship(connection: sqlite3.Connection, observation: sqlite3.Row) -> None:
    actor_id, target_id = observation["actor_platform_account_id"], observation["target_platform_account_id"]
    if actor_id is None or target_id is None:
        return
    users = connection.execute(
        "SELECT a.user_id, t.user_id FROM platform_accounts a, platform_accounts t WHERE a.id=? AND t.id=?",
        (actor_id, target_id),
    ).fetchone()
    if users is None or users[0] is None or users[1] is None or users[0] == users[1]:
        return
    relationship = str(observation["event_type"])
    context = str(observation["context_id"] or observation["container_id"] or "")
    occurred = str(observation["occurred_at"])
    connection.execute(
        """INSERT INTO entity_relationships(source_user_id, target_user_id, relationship_type, context_key,
             strength, evidence_count, first_observed_at, last_observed_at, evidence_json)
           VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?)
           ON CONFLICT(source_user_id, target_user_id, relationship_type, context_key) DO UPDATE SET
             strength=entity_relationships.strength+1, evidence_count=entity_relationships.evidence_count+1,
             last_observed_at=excluded.last_observed_at, evidence_json=excluded.evidence_json""",
        (int(users[0]), int(users[1]), relationship, context, occurred, occurred,
         json.dumps({"latest_observation_id": int(observation["id"])})),
    )


def _parse_irc_tags(line: str) -> tuple[dict[str, str], str]:
    if not line.startswith("@"):
        return {}, line
    blob, remainder = line.split(" ", 1)
    return {key: value for key, _, value in (item.partition("=") for item in blob[1:].split(";"))}, remainder


def _millisecond_timestamp(value: str | None) -> str:
    if value and value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value, default=str))
