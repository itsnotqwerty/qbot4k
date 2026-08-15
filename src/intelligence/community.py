from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable


ROLE_PERMISSIONS = {
    "viewer": frozenset({"community.read", "alerts.read", "live_ops.read"}),
    "analyst": frozenset({
        "community.read", "alerts.read", "alerts.manage", "cases.manage",
        "intelligence.read", "live_ops.read",
    }),
    "moderator": frozenset({
        "community.read", "alerts.read", "alerts.manage", "cases.manage",
        "intelligence.read", "live_ops.read", "moderation.manage",
    }),
    "admin": frozenset({"*"}),
    "owner": frozenset({"*"}),
}


def create_organization(connection: sqlite3.Connection, *, name: str, slug: str) -> int:
    with connection:
        cursor = connection.execute(
            "INSERT INTO organizations(name, slug) VALUES (?, ?)",
            (name.strip(), _slug(slug)),
        )
    return int(cursor.lastrowid)


def create_workspace(
    connection: sqlite3.Connection, *, organization_id: int, name: str, slug: str
) -> int:
    with connection:
        cursor = connection.execute(
            "INSERT INTO workspaces(organization_id, name, slug) VALUES (?, ?, ?)",
            (int(organization_id), name.strip(), _slug(slug)),
        )
    return int(cursor.lastrowid)


def create_community(
    connection: sqlite3.Connection,
    *,
    workspace_id: int,
    name: str,
    slug: str,
    timezone_name: str = "UTC",
) -> int:
    with connection:
        cursor = connection.execute(
            "INSERT INTO communities(workspace_id, name, slug, timezone) VALUES (?, ?, ?, ?)",
            (int(workspace_id), name.strip(), _slug(slug), timezone_name.strip() or "UTC"),
        )
        community_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO community_policy_settings(community_id) VALUES (?)",
            (community_id,),
        )
    return community_id


def register_installation(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform: str,
    external_community_id: str,
    display_name: str,
    scopes: Iterable[str] = (),
    status: str = "active",
) -> int:
    with connection:
        connection.execute(
            """INSERT INTO community_installations(
                   community_id, platform, external_community_id, display_name, status, scopes_json
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, external_community_id) DO UPDATE SET
                   community_id=excluded.community_id, display_name=excluded.display_name,
                   status=excluded.status, scopes_json=excluded.scopes_json,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), platform.strip().casefold(), external_community_id.strip(),
                display_name.strip(), status.strip().casefold(),
                json.dumps(sorted({str(scope).strip() for scope in scopes if str(scope).strip()})),
            ),
        )
        row = connection.execute(
            "SELECT id FROM community_installations WHERE platform=? AND external_community_id=?",
            (platform.strip().casefold(), external_community_id.strip()),
        ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("installation was not persisted")
    return int(row[0])


def resolve_community_id(
    connection: sqlite3.Connection, *, platform: str, external_community_id: str
) -> int:
    row = connection.execute(
        """SELECT community_id FROM community_installations
           WHERE platform=? AND external_community_id=? AND status='active'""",
        (platform.strip().casefold(), external_community_id.strip()),
    ).fetchone()
    return int(row[0]) if row is not None else 1


def grant_operator_role(
    connection: sqlite3.Connection, *, operator_id: int, community_id: int, role: str
) -> None:
    normalized = role.strip().casefold()
    if normalized not in ROLE_PERMISSIONS:
        raise ValueError(f"unsupported community role: {role}")
    with connection:
        connection.execute(
            """INSERT INTO operator_community_roles(operator_id, community_id, role)
               VALUES (?, ?, ?)
               ON CONFLICT(operator_id, community_id) DO UPDATE SET role=excluded.role""",
            (int(operator_id), int(community_id), normalized),
        )


def operator_has_permission(
    connection: sqlite3.Connection, *, operator_id: int, community_id: int, permission: str
) -> bool:
    row = connection.execute(
        "SELECT role FROM operator_community_roles WHERE operator_id=? AND community_id=?",
        (int(operator_id), int(community_id)),
    ).fetchone()
    if row is None:
        return False
    permissions = ROLE_PERMISSIONS.get(str(row[0]).casefold(), frozenset())
    return "*" in permissions or permission in permissions


def list_operator_communities(connection: sqlite3.Connection, operator_id: int) -> list[sqlite3.Row]:
    return list(connection.execute(
        """SELECT c.id, c.name, c.slug, r.role
           FROM operator_community_roles r
           JOIN communities c ON c.id=r.community_id
           WHERE r.operator_id=? AND c.status='active' ORDER BY c.name COLLATE NOCASE""",
        (int(operator_id),),
    ).fetchall())


def _slug(value: str) -> str:
    normalized = "-".join(value.strip().casefold().replace("_", "-").split())
    if not normalized or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized):
        raise ValueError("slug must contain only lowercase letters, numbers, and hyphens")
    return normalized
