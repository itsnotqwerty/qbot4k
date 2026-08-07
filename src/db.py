from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .intelligence.powerusers import (
    POWERUSER_THRESHOLD,
    apply_reputation_event,
    default_social_score_for_name,
    enforced_social_score_for_name,
    score_delta_for_message,
    score_delta_for_moderation,
)
from .models import IngestionResult, NormalizedMessage
from .moderation import ModerationFinding, ModerationRule, evaluate_message_moderation

RESERVED_COMMAND_NAMES = {"addcom", "delcom", "editcom"}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    primary_display_name TEXT NOT NULL,
    current_reputation_score INTEGER NOT NULL DEFAULT 500,
    candidate_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_accounts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    guild_or_channel_context TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, platform_user_id)
);

CREATE TABLE IF NOT EXISTS operator_accounts (
    id INTEGER PRIMARY KEY,
    discord_user_id TEXT NOT NULL UNIQUE,
    discord_username TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_message_id TEXT,
    platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
    channel_id TEXT NOT NULL,
    content_raw TEXT NOT NULL,
    content_normalized TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, platform_message_id)
);

CREATE TABLE IF NOT EXISTS twitch_channels (
    id INTEGER PRIMARY KEY,
    channel_name TEXT NOT NULL UNIQUE,
    requested_by_platform_account_id INTEGER REFERENCES platform_accounts(id) ON DELETE SET NULL,
    request_source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    join_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS moderation_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    severity TEXT NOT NULL,
    auto_enforce_action TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rule_matches (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    moderation_rule_id INTEGER NOT NULL REFERENCES moderation_rules(id),
    severity TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    confidence REAL,
    recommended_action TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    target_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
    action_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id INTEGER,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reputation_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    delta INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_notes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operator_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'open',
    severity TEXT NOT NULL,
    queue_reason_code TEXT NOT NULL,
    assigned_operator_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS server_boost_requests (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    requester_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requester_platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
    command_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fulfilled_at TEXT
);

CREATE TABLE IF NOT EXISTS command_definitions (
    command_name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description_template TEXT NOT NULL,
    footer_template TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS simple_command_definitions (
    command_name TEXT PRIMARY KEY,
    response_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discord_channels (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    channel_type INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twitch_live_announcements (
    id INTEGER PRIMARY KEY,
    twitch_channel_name TEXT NOT NULL,
    twitch_stream_id TEXT NOT NULL,
    discord_guild_id TEXT NOT NULL,
    discord_channel_id TEXT NOT NULL,
    announced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(twitch_channel_name, twitch_stream_id, discord_guild_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id INTEGER,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics_rollups (
    id INTEGER PRIMARY KEY,
    metric_name TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    bucket_size TEXT NOT NULL,
    dimension_json TEXT NOT NULL DEFAULT '{}',
    value REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(metric_name, bucket_start, bucket_size, dimension_json)
);

CREATE INDEX IF NOT EXISTS idx_platform_accounts_user_id
    ON platform_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at
    ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_platform_account
    ON messages(platform_account_id);
CREATE INDEX IF NOT EXISTS idx_twitch_channels_status
    ON twitch_channels(status, channel_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_rules_name
    ON moderation_rules(name);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_created_at
    ON moderation_actions(created_at);
CREATE INDEX IF NOT EXISTS idx_reputation_events_user_id
    ON reputation_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_queue_status_created_at
    ON review_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_server_boost_requests_lookup
    ON server_boost_requests(platform, channel_id, command_name, status, requested_at);
CREATE INDEX IF NOT EXISTS idx_server_boost_requests_expires_at
    ON server_boost_requests(expires_at);
CREATE INDEX IF NOT EXISTS idx_command_definitions_enabled
    ON command_definitions(enabled, command_name);
CREATE INDEX IF NOT EXISTS idx_simple_command_definitions_enabled
    ON simple_command_definitions(enabled, command_name);
CREATE INDEX IF NOT EXISTS idx_discord_channels_guild_name
    ON discord_channels(guild_id, channel_name);
CREATE INDEX IF NOT EXISTS idx_twitch_live_announcements_lookup
    ON twitch_live_announcements(twitch_channel_name, twitch_stream_id, discord_guild_id);
"""

DEFAULT_COMMAND_DEFINITIONS = (
    {
        "command_name": "credit",
        "title": "Social Credit Profile",
        "description_template": "Profile for {display_name}",
        "footer_template": "{platform} user: {author_username}",
        "enabled": 1,
    },
)


def connect_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(SCHEMA_SQL)
        _backfill_fixed_social_scores(connection)
        _seed_default_command_definitions(connection)


def _backfill_fixed_social_scores(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT id, primary_display_name, current_reputation_score, candidate_flag
        FROM users
        """
    ).fetchall()
    for row in rows:
        user_id = int(row[0])
        display_name = str(row[1])
        current_score = int(row[2])
        current_candidate_flag = int(row[3])
        enforced_score = enforced_social_score_for_name(display_name, current_score)
        enforced_candidate_flag = int(enforced_score >= POWERUSER_THRESHOLD)
        if enforced_score == current_score and enforced_candidate_flag == current_candidate_flag:
            continue
        connection.execute(
            """
            UPDATE users
            SET current_reputation_score = ?,
                candidate_flag = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (enforced_score, enforced_candidate_flag, user_id),
        )


def _seed_default_command_definitions(connection: sqlite3.Connection) -> None:
    for definition in DEFAULT_COMMAND_DEFINITIONS:
        connection.execute(
            """
            INSERT INTO command_definitions (
                command_name,
                title,
                description_template,
                footer_template,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_name) DO NOTHING
            """,
            (
                definition["command_name"],
                definition["title"],
                definition["description_template"],
                definition["footer_template"],
                definition["enabled"],
            ),
        )


def list_simple_command_definitions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT command_name, response_template, enabled, created_at, updated_at
        FROM simple_command_definitions
        WHERE command_name NOT IN ('addcom', 'delcom', 'editcom')
        ORDER BY command_name
        """
    ).fetchall()
    return list(rows)


def get_simple_command_definition(connection: sqlite3.Connection, command_name: str) -> sqlite3.Row | None:
    command_key = command_name.strip().casefold()
    if command_key in RESERVED_COMMAND_NAMES:
        return None
    return connection.execute(
        """
        SELECT command_name, response_template, enabled, created_at, updated_at
        FROM simple_command_definitions
        WHERE command_name = ?
        """,
        (command_key,),
    ).fetchone()


def upsert_simple_command_definition(
    connection: sqlite3.Connection,
    *,
    command_name: str,
    response_template: str,
    enabled: bool,
) -> None:
    command_key = command_name.strip().casefold()
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    if not response_template.strip():
        raise ValueError("response_template must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO simple_command_definitions (
                command_name,
                response_template,
                enabled
            ) VALUES (?, ?, ?)
            ON CONFLICT(command_name)
            DO UPDATE SET
                response_template = excluded.response_template,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                command_key,
                response_template.strip(),
                int(enabled),
            ),
        )


def delete_simple_command_definition(connection: sqlite3.Connection, command_name: str) -> bool:
    command_key = command_name.strip().casefold().lstrip("!")
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    with connection:
        result = connection.execute(
            "DELETE FROM simple_command_definitions WHERE command_name = ?",
            (command_key,),
        )
    return result.rowcount > 0


def list_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def database_health(database_path: Path) -> dict[str, object]:
    connection = connect_database(database_path)
    try:
        initialize_database(connection)
        table_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        pragma_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        return {
            "status": "ready",
            "path": str(database_path),
            "table_count": table_count,
            "journal_mode": pragma_mode,
        }
    finally:
        connection.close()


def ensure_platform_account(
    connection: sqlite3.Connection,
    *,
    platform: str,
    platform_user_id: str,
    username: str,
    guild_or_channel_context: str | None,
) -> int:
    with connection:
        connection.execute(
            """
            INSERT INTO platform_accounts (
                platform,
                platform_user_id,
                username,
                guild_or_channel_context
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_user_id)
            DO UPDATE SET
                username = excluded.username,
                guild_or_channel_context = COALESCE(
                    excluded.guild_or_channel_context,
                    platform_accounts.guild_or_channel_context
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, platform_user_id, username, guild_or_channel_context),
        )
        row = connection.execute(
            """
            SELECT id
            FROM platform_accounts
            WHERE platform = ? AND platform_user_id = ?
            """,
            (platform, platform_user_id),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve platform account after upsert")

    return int(row[0])


def upsert_operator_account(
    connection: sqlite3.Connection,
    *,
    discord_user_id: str,
    discord_username: str,
    role: str,
) -> int:
    with connection:
        connection.execute(
            """
            INSERT INTO operator_accounts (
                discord_user_id,
                discord_username,
                role
            ) VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id)
            DO UPDATE SET
                discord_username = excluded.discord_username,
                role = excluded.role,
                updated_at = CURRENT_TIMESTAMP
            """,
            (discord_user_id, discord_username, role),
        )
        row = connection.execute(
            """
            SELECT id
            FROM operator_accounts
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve operator account after upsert")

    return int(row[0])


def get_operator_account_by_discord_user_id(
    connection: sqlite3.Connection,
    discord_user_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, discord_user_id, discord_username, role
        FROM operator_accounts
        WHERE discord_user_id = ?
        """,
        (discord_user_id,),
    ).fetchone()


def list_command_definitions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT command_name, title, description_template, footer_template, enabled, created_at, updated_at
        FROM command_definitions
        WHERE command_name NOT IN ('addcom', 'delcom', 'editcom')
        ORDER BY command_name
        """
    ).fetchall()
    return list(rows)


def get_command_definition(connection: sqlite3.Connection, command_name: str) -> sqlite3.Row | None:
    command_key = command_name.strip().casefold()
    if command_key in RESERVED_COMMAND_NAMES:
        return None
    return connection.execute(
        """
        SELECT command_name, title, description_template, footer_template, enabled, created_at, updated_at
        FROM command_definitions
        WHERE command_name = ?
        """,
        (command_key,),
    ).fetchone()


def upsert_command_definition(
    connection: sqlite3.Connection,
    *,
    command_name: str,
    title: str,
    description_template: str,
    footer_template: str | None,
    enabled: bool,
) -> None:
    command_key = command_name.strip().casefold()
    if not command_key:
        raise ValueError("command_name must not be empty")
    if command_key in RESERVED_COMMAND_NAMES:
        raise ValueError(f"{command_key} is reserved")
    if not title.strip():
        raise ValueError("title must not be empty")
    if not description_template.strip():
        raise ValueError("description_template must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO command_definitions (
                command_name,
                title,
                description_template,
                footer_template,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_name)
            DO UPDATE SET
                title = excluded.title,
                description_template = excluded.description_template,
                footer_template = excluded.footer_template,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                command_key,
                title.strip(),
                description_template.strip(),
                footer_template.strip() if footer_template and footer_template.strip() else None,
                int(enabled),
            ),
        )


def persist_normalized_message(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
) -> IngestionResult:
    platform_account_id = ensure_platform_account(
        connection,
        platform=message.platform,
        platform_user_id=message.platform_user_id,
        username=message.username,
        guild_or_channel_context=message.guild_or_channel_context,
    )

    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    platform,
                    platform_message_id,
                    platform_account_id,
                    channel_id,
                    content_raw,
                    content_normalized,
                    sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.platform,
                    message.platform_message_id,
                    platform_account_id,
                    message.channel_id,
                    message.content_raw,
                    message.content_normalized,
                    message.sent_at,
                ),
            )
            message_id = int(cursor.lastrowid)
            canonical_user_id = _ensure_canonical_user_for_platform_account(
                connection,
                platform_account_id=platform_account_id,
                preferred_display_name=message.username,
            )

            message_delta = score_delta_for_message(message.content_raw)
            if message_delta is not None:
                delta, reason_code = message_delta
                apply_reputation_event(
                    connection,
                    user_id=canonical_user_id,
                    delta=delta,
                    reason_code=reason_code,
                    source_type="message",
                    source_id=message_id,
                )

            moderation_rules = load_enabled_moderation_rules(connection)
            if moderation_rules:
                findings = evaluate_message_moderation(message, moderation_rules)
                record_moderation_findings(
                    connection,
                    message_id=message_id,
                    platform=message.platform,
                    findings=findings,
                )

                for finding in findings:
                    penalty_delta, penalty_reason = score_delta_for_moderation(
                        severity=finding.severity,
                        action_type=finding.auto_enforce_action,
                    )
                    apply_reputation_event(
                        connection,
                        user_id=canonical_user_id,
                        delta=penalty_delta,
                        reason_code=penalty_reason,
                        source_type="moderation",
                        source_id=message_id,
                    )
        return IngestionResult(
            status="persisted",
            platform=message.platform,
            platform_account_id=platform_account_id,
            message_id=message_id,
        )
    except sqlite3.IntegrityError:
        if message.platform_message_id is None:
            raise

        row = connection.execute(
            """
            SELECT id
            FROM messages
            WHERE platform = ? AND platform_message_id = ?
            """,
            (message.platform, message.platform_message_id),
        ).fetchone()
        if row is None:
            raise

        return IngestionResult(
            status="duplicate",
            platform=message.platform,
            platform_account_id=platform_account_id,
            message_id=int(row[0]),
            reason="message_already_ingested",
        )


def _ensure_canonical_user_for_platform_account(
    connection: sqlite3.Connection,
    *,
    platform_account_id: int,
    preferred_display_name: str,
) -> int:
    row = connection.execute(
        """
        SELECT user_id
        FROM platform_accounts
        WHERE id = ?
        """,
        (platform_account_id,),
    ).fetchone()
    if row is None:
        raise ValueError("platform account not found")

    existing_user_id = row[0]
    if existing_user_id is not None:
        return int(existing_user_id)

    display_name = preferred_display_name.strip() or f"user_{platform_account_id}"
    initial_score = default_social_score_for_name(display_name)
    connection.execute(
        """
        INSERT INTO users (
            primary_display_name,
            current_reputation_score,
            candidate_flag
        ) VALUES (?, ?, ?)
        """,
        (display_name, initial_score, int(initial_score >= POWERUSER_THRESHOLD)),
    )
    created_user = connection.execute(
        "SELECT id FROM users WHERE rowid = last_insert_rowid()"
    ).fetchone()
    if created_user is None:
        raise sqlite3.IntegrityError("Failed to resolve canonical user after insert")

    created_user_id = int(created_user[0])
    connection.execute(
        """
        UPDATE platform_accounts
        SET user_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (created_user_id, platform_account_id),
    )
    connection.execute(
        """
        INSERT INTO audit_log (
            actor_type,
            actor_id,
            action_type,
            entity_type,
            entity_id,
            payload_json
        ) VALUES (
            'system',
            NULL,
            'auto_user_create',
            'user',
            ?,
            json_object('platform_account_id', ?)
        )
        """,
        (created_user_id, platform_account_id),
    )
    return created_user_id


def upsert_twitch_channel(
    connection: sqlite3.Connection,
    *,
    channel_name: str,
    requested_by_platform_account_id: int | None,
    request_source_message_id: int | None,
    join_source: str,
    status: str = "requested",
) -> int:
    normalized_channel_name = channel_name.strip().casefold()
    if not normalized_channel_name:
        raise ValueError("channel_name must not be empty")

    with connection:
        connection.execute(
            """
            INSERT INTO twitch_channels (
                channel_name,
                requested_by_platform_account_id,
                request_source_message_id,
                join_source,
                status
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_name)
            DO UPDATE SET
                requested_by_platform_account_id = excluded.requested_by_platform_account_id,
                request_source_message_id = excluded.request_source_message_id,
                join_source = excluded.join_source,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                normalized_channel_name,
                requested_by_platform_account_id,
                request_source_message_id,
                join_source,
                status,
            ),
        )
        row = connection.execute(
            "SELECT id FROM twitch_channels WHERE channel_name = ?",
            (normalized_channel_name,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve twitch channel after upsert")

    return int(row[0])


def list_twitch_channels(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[sqlite3.Row]:
    if status is None:
        rows = connection.execute(
            "SELECT * FROM twitch_channels ORDER BY channel_name"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM twitch_channels WHERE status = ? ORDER BY channel_name",
            (status,),
        ).fetchall()
    return list(rows)


def update_twitch_channel_status(
    connection: sqlite3.Connection,
    *,
    channel_name: str,
    status: str,
) -> None:
    normalized_channel_name = channel_name.strip().casefold()
    if not normalized_channel_name:
        raise ValueError("channel_name must not be empty")

    with connection:
        connection.execute(
            """
            UPDATE twitch_channels
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE channel_name = ?
            """,
            (status, normalized_channel_name),
        )


def upsert_moderation_rule(
    connection: sqlite3.Connection,
    *,
    name: str,
    rule_type: str,
    pattern: str,
    severity: str,
    auto_enforce_action: str | None = None,
    enabled: bool = True,
) -> int:
    with connection:
        connection.execute(
            """
            INSERT INTO moderation_rules (
                name,
                rule_type,
                pattern,
                severity,
                auto_enforce_action,
                enabled
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name)
            DO UPDATE SET
                rule_type = excluded.rule_type,
                pattern = excluded.pattern,
                severity = excluded.severity,
                auto_enforce_action = excluded.auto_enforce_action,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                name,
                rule_type,
                pattern,
                severity,
                auto_enforce_action,
                int(enabled),
            ),
        )
        row = connection.execute(
            "SELECT id FROM moderation_rules WHERE name = ?",
            (name,),
        ).fetchone()

    if row is None:
        raise sqlite3.IntegrityError("Failed to resolve moderation rule after upsert")

    return int(row[0])


def load_enabled_moderation_rules(connection: sqlite3.Connection) -> list[ModerationRule]:
    rows = connection.execute(
        """
        SELECT id, name, rule_type, pattern, severity, auto_enforce_action, enabled
        FROM moderation_rules
        WHERE enabled = 1
        ORDER BY id
        """
    ).fetchall()
    return [
        ModerationRule(
            id=int(row[0]),
            name=str(row[1]),
            rule_type=str(row[2]),
            pattern=str(row[3]),
            severity=str(row[4]),
            auto_enforce_action=str(row[5]) if row[5] is not None else None,
            enabled=bool(row[6]),
        )
        for row in rows
    ]


def record_moderation_findings(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    platform: str,
    findings: list[ModerationFinding],
) -> None:
    if not findings:
        return

    for finding in findings:
        connection.execute(
            """
            INSERT INTO rule_matches (
                message_id,
                moderation_rule_id,
                severity,
                reason_code,
                confidence,
                recommended_action
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                finding.rule_id,
                finding.severity,
                finding.reason_code,
                1.0,
                finding.auto_enforce_action,
            ),
        )
        if finding.auto_enforce_action:
            connection.execute(
                """
                INSERT INTO moderation_actions (
                    platform,
                    message_id,
                    target_platform_account_id,
                    action_type,
                    actor_type,
                    actor_id,
                    reason,
                    status
                ) VALUES (
                    ?,
                    ?,
                    (SELECT platform_account_id FROM messages WHERE id = ?),
                    ?,
                    'system',
                    NULL,
                    ?,
                    'pending'
                )
                """,
                (
                    platform,
                    message_id,
                    message_id,
                    finding.auto_enforce_action,
                    finding.reason_code,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO review_queue (
                    message_id,
                    status,
                    severity,
                    queue_reason_code,
                    assigned_operator_id,
                    created_at,
                    resolved_at
                ) VALUES (?, 'open', ?, ?, NULL, CURRENT_TIMESTAMP, NULL)
                """,
                (message_id, finding.severity, finding.reason_code),
            )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_server_boost_request(
    connection: sqlite3.Connection,
    *,
    platform: str,
    channel_id: str,
    requester_platform_account_id: int,
    command_name: str,
    requested_at: str | None = None,
    expires_in_minutes: int = 30,
) -> int:
    account = connection.execute(
        """
        SELECT user_id
        FROM platform_accounts
        WHERE id = ?
        """,
        (requester_platform_account_id,),
    ).fetchone()
    if account is None or account[0] is None:
        raise ValueError("requesting platform account is not linked to a canonical user")

    now = requested_at or _utcnow_iso()
    expires_at = (datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(minutes=expires_in_minutes)).isoformat()
    requester_user_id = int(account[0])
    with connection:
        row = connection.execute(
            """
            SELECT id
            FROM server_boost_requests
            WHERE platform = ?
              AND channel_id = ?
              AND requester_user_id = ?
              AND command_name = ?
              AND status = 'pending'
            ORDER BY requested_at DESC, id DESC
            LIMIT 1
            """,
            (platform, channel_id, requester_user_id, command_name),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO server_boost_requests (
                    platform,
                    channel_id,
                    requester_user_id,
                    requester_platform_account_id,
                    command_name,
                    status,
                    requested_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    platform,
                    channel_id,
                    requester_user_id,
                    requester_platform_account_id,
                    command_name,
                    now,
                    expires_at,
                ),
            )
            request_id = int(cursor.lastrowid)
        else:
            request_id = int(row[0])
            connection.execute(
                """
                UPDATE server_boost_requests
                SET requested_at = ?,
                    expires_at = ?,
                    status = 'pending',
                    fulfilled_at = NULL
                WHERE id = ?
                """,
                (now, expires_at, request_id),
            )

    return request_id


def reward_server_boost_request(
    connection: sqlite3.Connection,
    *,
    platform: str,
    channel_id: str,
    command_names: tuple[str, ...],
    reward_delta: int = 2,
    reason_code: str = "server_boost_success",
) -> int | None:
    command_names = tuple(command_name.strip().casefold() for command_name in command_names if command_name.strip())
    if not command_names:
        return None

    now = _utcnow_iso()
    row = connection.execute(
        f"""
        SELECT id, requester_user_id, command_name
        FROM server_boost_requests
        WHERE platform = ?
          AND channel_id = ?
          AND status = 'pending'
          AND expires_at > ?
          AND command_name IN ({','.join('?' for _ in command_names)})
        ORDER BY requested_at ASC, id ASC
        LIMIT 1
        """,
        (platform, channel_id, now, *command_names),
    ).fetchone()
    if row is None:
        return None

    request_id = int(row[0])
    requester_user_id = int(row[1])
    command_name = str(row[2])
    apply_reputation_event(
        connection,
        user_id=requester_user_id,
        delta=reward_delta,
        reason_code=reason_code,
        source_type="server_boost",
        source_id=request_id,
    )
    with connection:
        connection.execute(
            """
            UPDATE server_boost_requests
            SET status = 'fulfilled',
                fulfilled_at = ?
            WHERE id = ?
            """,
            (now, request_id),
        )

    return request_id


def upsert_discord_channel(
    connection: sqlite3.Connection,
    *,
    guild_id: str,
    channel_id: str,
    channel_name: str,
    channel_type: int,
) -> None:
    guild_key = guild_id.strip()
    channel_key = channel_id.strip()
    channel_label = channel_name.strip()
    if not guild_key or not channel_key or not channel_label:
        raise ValueError("guild_id, channel_id, and channel_name must not be empty")
    with connection:
        connection.execute(
            """
            INSERT INTO discord_channels (
                channel_id,
                guild_id,
                channel_name,
                channel_type
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id)
            DO UPDATE SET
                guild_id = excluded.guild_id,
                channel_name = excluded.channel_name,
                channel_type = excluded.channel_type,
                updated_at = CURRENT_TIMESTAMP
            """,
            (channel_key, guild_key, channel_label, int(channel_type)),
        )


def get_discord_channel_name(connection: sqlite3.Connection, channel_id: str) -> str | None:
    channel_key = channel_id.strip()
    if not channel_key:
        return None
    row = connection.execute(
        """
        SELECT channel_name
        FROM discord_channels
        WHERE channel_id = ?
        """,
        (channel_key,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def has_twitch_live_announcement(
    connection: sqlite3.Connection,
    *,
    twitch_channel_name: str,
    twitch_stream_id: str,
    discord_guild_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM twitch_live_announcements
        WHERE twitch_channel_name = ?
          AND twitch_stream_id = ?
          AND discord_guild_id = ?
        LIMIT 1
        """,
        (
            twitch_channel_name.strip().casefold(),
            twitch_stream_id.strip(),
            discord_guild_id.strip(),
        ),
    ).fetchone()
    return row is not None


def record_twitch_live_announcement(
    connection: sqlite3.Connection,
    *,
    twitch_channel_name: str,
    twitch_stream_id: str,
    discord_guild_id: str,
    discord_channel_id: str,
    announced_at: str | None = None,
) -> None:
    timestamp = announced_at or _utcnow_iso()
    with connection:
        connection.execute(
            """
            INSERT INTO twitch_live_announcements (
                twitch_channel_name,
                twitch_stream_id,
                discord_guild_id,
                discord_channel_id,
                announced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(twitch_channel_name, twitch_stream_id, discord_guild_id)
            DO NOTHING
            """,
            (
                twitch_channel_name.strip().casefold(),
                twitch_stream_id.strip(),
                discord_guild_id.strip(),
                discord_channel_id.strip(),
                timestamp,
            ),
        )