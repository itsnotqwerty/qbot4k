from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contexts import ActorAttribution, TenantContext
from ..surface_policy import INSTALLATION_CAPABILITY_BY_SURFACE, require_non_http_surface


DASHBOARD_CAPABILITIES = frozenset({
    "dashboard.access", "community.read", "members.read", "moderation.queues.read",
    "moderation.manage", "moderation.bulk", "rules.manage", "appeals.manage",
    "evidence.sensitive.read", "alerts.read", "alerts.manage", "cases.manage",
    "intelligence.read", "analytics.read", "analytics.export", "exports.create",
    "announcements.manage", "integrations.manage", "settings.manage", "operators.manage",
    "audit.read", "live_ops.read", "live_ops.manage",
})

TWITCH_INSTALL_OAUTH_SCOPES = frozenset({
    "channel:read:subscriptions",
    "moderator:manage:banned_users",
    "moderator:manage:chat_settings",
    "moderator:manage:shield_mode",
    "moderator:read:followers",
})

ROLE_PERMISSIONS = {
    "viewer": frozenset({
        "dashboard.access", "community.read", "members.read", "alerts.read",
        "analytics.read", "live_ops.read",
    }),
    "analyst": frozenset({
        "dashboard.access", "community.read", "members.read", "moderation.queues.read",
        "alerts.read", "alerts.manage", "cases.manage", "intelligence.read",
        "analytics.read", "exports.create", "live_ops.read",
    }),
    "moderator": frozenset({
        "dashboard.access", "community.read", "members.read", "moderation.queues.read",
        "moderation.manage", "rules.manage", "appeals.manage", "evidence.sensitive.read",
        "alerts.read", "alerts.manage", "cases.manage", "intelligence.read",
        "analytics.read", "exports.create", "live_ops.read", "live_ops.manage",
    }),
    "admin": frozenset({"*"}),
    "owner": frozenset({"*"}),
}
INSTALLATION_CAPABILITIES = frozenset({
    "events", "moderation_actions", "member_lifecycle", "announcements", "live_controls",
})
PLATFORM_CAPABILITIES = {
    "discord": frozenset({"events", "moderation_actions", "member_lifecycle", "announcements"}),
    "twitch": frozenset({"events", "moderation_actions", "member_lifecycle", "announcements", "live_controls"}),
}

_QUEUE_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


def platform_capabilities(platform: str) -> frozenset[str]:
    return PLATFORM_CAPABILITIES.get(platform.strip().casefold(), frozenset())


def create_member_report(
    connection: sqlite3.Connection, *, community_id: int,
    subject_platform_account_id: int, category: str, summary: str,
    reporter_platform_account_id: int | None = None,
    evidence: Mapping[str, object] | None = None, severity: str = "medium",
) -> int:
    normalized_severity = severity.strip().casefold()
    if normalized_severity not in _QUEUE_SEVERITIES:
        raise ValueError("invalid report severity")
    if not category.strip() or not summary.strip():
        raise ValueError("report category and summary are required")
    account_ids = [int(subject_platform_account_id)]
    if reporter_platform_account_id is not None:
        account_ids.append(int(reporter_platform_account_id))
    placeholders = ",".join("?" for _ in account_ids)
    count = int(connection.execute(
        f"""SELECT COUNT(DISTINCT pa.id) FROM platform_accounts pa
            JOIN messages m ON m.platform_account_id=pa.id
            WHERE m.community_id=? AND pa.id IN ({placeholders})""",
        (int(community_id), *account_ids),
    ).fetchone()[0])
    if count != len(set(account_ids)):
        raise LookupError("report account not found in community")
    with connection:
        cursor = connection.execute(
            """INSERT INTO member_reports(
                   community_id,reporter_platform_account_id,subject_platform_account_id,
                   category,summary,evidence_json,severity
               ) VALUES (?,?,?,?,?,?,?)""",
            (int(community_id), reporter_platform_account_id, int(subject_platform_account_id),
             category.strip(), summary.strip(), json.dumps(evidence or {}, sort_keys=True),
             normalized_severity),
        )
        report_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO audit_log(actor_type,action_type,entity_type,entity_id,payload_json)
               VALUES ('member','member_report.created','member_report',?,?)""",
            (report_id, json.dumps({"community_id": int(community_id)}, sort_keys=True)),
        )
    return report_id


def create_member_appeal(
    connection: sqlite3.Connection, *, community_id: int, moderation_action_id: int,
    appellant_platform_account_id: int, reason: str,
    evidence: Mapping[str, object] | None = None, severity: str = "medium",
) -> int:
    normalized_severity = severity.strip().casefold()
    if normalized_severity not in _QUEUE_SEVERITIES:
        raise ValueError("invalid appeal severity")
    if not reason.strip():
        raise ValueError("appeal reason is required")
    action = connection.execute(
        """SELECT actor_type,actor_id,target_platform_account_id
           FROM moderation_actions WHERE id=? AND community_id=?""",
        (int(moderation_action_id), int(community_id)),
    ).fetchone()
    if action is None or int(action[2]) != int(appellant_platform_account_id):
        raise LookupError("moderation action not found for appellant")
    assigned_operator_id = None
    if normalized_severity in {"high", "critical"}:
        original_actor_id = int(action[1]) if action[0] == "operator" and action[1] is not None else -1
        reviewer = connection.execute(
            """SELECT r.operator_id,COUNT(a.id) AS open_count
               FROM operator_community_roles r
               JOIN operator_accounts o ON o.id=r.operator_id AND o.status='active'
               LEFT JOIN member_appeals a ON a.assigned_operator_id=r.operator_id AND a.status='open'
               WHERE r.community_id=? AND r.role IN ('moderator','admin','owner') AND r.operator_id<>?
               GROUP BY r.operator_id ORDER BY open_count,r.operator_id LIMIT 1""",
            (int(community_id), original_actor_id),
        ).fetchone()
        assigned_operator_id = int(reviewer[0]) if reviewer is not None else None
    with connection:
        cursor = connection.execute(
            """INSERT INTO member_appeals(
                   community_id,moderation_action_id,appellant_platform_account_id,
                   reason,evidence_json,severity,assigned_operator_id
               ) VALUES (?,?,?,?,?,?,?)""",
            (int(community_id), int(moderation_action_id), int(appellant_platform_account_id),
             reason.strip(), json.dumps(evidence or {}, sort_keys=True), normalized_severity,
             assigned_operator_id),
        )
        appeal_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO audit_log(actor_type,action_type,entity_type,entity_id,payload_json)
               VALUES ('member','member_appeal.created','member_appeal',?,?)""",
            (appeal_id, json.dumps({"community_id": int(community_id),
                                    "assigned_operator_id": assigned_operator_id}, sort_keys=True)),
        )
    return appeal_id


def resolve_member_queue_item(
    connection: sqlite3.Connection, *, tenant: TenantContext, actor: ActorAttribution,
    queue_type: str, item_id: int, resolution: str, note: str,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise PermissionError("member queue resolution requires an operator actor")
    community_id = tenant.community_id
    operator_id = actor.actor_id
    normalized_type = queue_type.strip().casefold()
    if normalized_type not in {"report", "appeal"}:
        raise ValueError("invalid member queue type")
    table = "member_reports" if normalized_type == "report" else "member_appeals"
    resolution_column = "resolution" if normalized_type == "report" else "disposition"
    row = connection.execute(
        f"SELECT status,assigned_operator_id,moderation_action_id FROM {table} WHERE id=? AND community_id=?"
        if normalized_type == "appeal" else
        f"SELECT status,assigned_operator_id,NULL FROM {table} WHERE id=? AND community_id=?",
        (int(item_id), int(community_id)),
    ).fetchone()
    if row is None:
        raise LookupError(f"member {normalized_type} not found")
    if str(row[0]) != "open":
        raise ValueError(f"member {normalized_type} is already resolved")
    if normalized_type == "appeal":
        action = connection.execute(
            "SELECT actor_type,actor_id FROM moderation_actions WHERE id=?", (int(row[2]),)
        ).fetchone()
        alternate_staffed = row[1] is not None
        if alternate_staffed and action[0] == "operator" and int(action[1]) == int(operator_id):
            raise PermissionError("high-severity appeal requires a different reviewer")
    with connection:
        connection.execute(
            f"""UPDATE {table} SET status='resolved',{resolution_column}=?,resolution_note=?,
                   resolved_by_operator_id=?,resolved_at=CURRENT_TIMESTAMP WHERE id=? AND community_id=?""",
            (resolution.strip(), note.strip(), int(operator_id), int(item_id), int(community_id)),
        )
        connection.execute(
            """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
               VALUES ('operator',?,?,?,?,?)""",
            (int(operator_id), f"member_{normalized_type}.resolved", f"member_{normalized_type}",
             int(item_id), json.dumps({"community_id": int(community_id),
                                      "resolution": resolution.strip()}, sort_keys=True)),
        )


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


def configure_community_profile(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    name: str,
    locale: str,
    timezone_name: str,
    description: str,
    guidelines: str,
    notifications_enabled: bool,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("community settings changes require an operator actor")
    normalized_name = name.strip()
    normalized_locale = locale.strip()
    normalized_timezone = timezone_name.strip()
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("community name must be between 1 and 120 characters")
    if not normalized_locale or len(normalized_locale) > 35:
        raise ValueError("locale must be between 1 and 35 characters")
    try:
        ZoneInfo(normalized_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone is not recognized") from exc
    normalized_description = description.strip()
    normalized_guidelines = guidelines.strip()
    if len(normalized_description) > 1000:
        raise ValueError("community description must not exceed 1000 characters")
    if len(normalized_guidelines) > 10_000:
        raise ValueError("community guidelines must not exceed 10000 characters")
    with connection:
        cursor = connection.execute(
            """UPDATE communities
               SET name=?,locale=?,timezone=?,description=?,guidelines=?,
                   notifications_enabled=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status='active'""",
            (normalized_name, normalized_locale, normalized_timezone,
             normalized_description, normalized_guidelines,
             int(notifications_enabled), tenant.community_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("active community not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'community.settings_updated','community',?,?)""",
            (actor.actor_id, tenant.community_id, json.dumps({
                "locale": normalized_locale,
                "timezone": normalized_timezone,
                "notifications_enabled": bool(notifications_enabled),
            }, sort_keys=True)),
        )


def register_installation(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    platform: str,
    external_community_id: str,
    display_name: str,
    scopes: Iterable[str] = (),
    metadata: Mapping[str, object] | None = None,
    capabilities: Iterable[str] = (),
    status: str = "active",
) -> int:
    normalized_status = status.strip().casefold()
    if normalized_status not in {"pending", "active", "degraded", "revoked"}:
        raise ValueError("unsupported installation status")
    requested_capabilities = {
        str(capability).strip() for capability in capabilities if str(capability).strip()
    }
    normalized_capabilities = sorted(
        requested_capabilities or platform_capabilities(platform)
    )
    unsupported = set(normalized_capabilities) - INSTALLATION_CAPABILITIES
    if unsupported:
        raise ValueError(f"unsupported installation capability: {sorted(unsupported)[0]}")
    with connection:
        connection.execute(
            """INSERT INTO community_installations(
                   community_id, platform, external_community_id, display_name, status, scopes_json,
                   metadata_json,capabilities_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, external_community_id) DO UPDATE SET
                   community_id=excluded.community_id, display_name=excluded.display_name,
                   status=excluded.status, scopes_json=excluded.scopes_json,
                   metadata_json=excluded.metadata_json,
                   capabilities_json=excluded.capabilities_json,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), platform.strip().casefold(), external_community_id.strip(),
                display_name.strip(), normalized_status,
                json.dumps(sorted({str(scope).strip() for scope in scopes if str(scope).strip()})),
                json.dumps(dict(metadata or {}), sort_keys=True),
                json.dumps(normalized_capabilities),
            ),
        )
        row = connection.execute(
            "SELECT id FROM community_installations WHERE platform=? AND external_community_id=?",
            (platform.strip().casefold(), external_community_id.strip()),
        ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("installation was not persisted")
    return int(row[0])


def installation_capabilities(
    connection: sqlite3.Connection, *, community_id: int, installation_id: int
) -> frozenset[str]:
    row = connection.execute(
        """SELECT capabilities_json FROM community_installations
           WHERE id=? AND community_id=?""",
        (int(installation_id), int(community_id)),
    ).fetchone()
    if row is None:
        raise LookupError("installation not found")
    try:
        values = json.loads(str(row[0] or "[]"))
    except json.JSONDecodeError:
        values = []
    return frozenset(str(value) for value in values if str(value) in INSTALLATION_CAPABILITIES)


def require_installation_surface(
    connection: sqlite3.Connection, *, tenant: TenantContext, surface: str
) -> None:
    if tenant.installation_id is None:
        raise ValueError("installation tenant context is required")
    require_non_http_surface(surface, guard="installation_capability")
    normalized_capability = INSTALLATION_CAPABILITY_BY_SURFACE.get(surface)
    if normalized_capability is None:
        raise ValueError(f"surface has no installation capability: {surface}")
    capabilities = installation_capabilities(
        connection, community_id=tenant.community_id, installation_id=tenant.installation_id
    )
    if normalized_capability not in capabilities:
        raise PermissionError(f"installation does not support {normalized_capability}")


def update_installation_health(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    installation_id: int,
    health_status: str,
    checked_at: str,
    error: str | None = None,
    reconnect_attempted: bool = False,
) -> None:
    normalized_health = health_status.strip().casefold()
    lifecycle_status = {
        "healthy": "active",
        "degraded": "degraded",
        "revoked": "revoked",
    }.get(normalized_health)
    if lifecycle_status is None:
        raise ValueError("health status must be healthy, degraded, or revoked")
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid installation health timestamp") from exc
    if checked.tzinfo is None:
        raise ValueError("installation health timestamp must include a timezone")
    normalized_error = str(error or "").strip() or None
    with connection:
        cursor = connection.execute(
            """UPDATE community_installations
               SET status=?,health_status=?,last_health_check_at=?,last_error=?,
                   last_verified_at=CASE WHEN ?='healthy' THEN ? ELSE last_verified_at END,
                   reconnect_attempts=reconnect_attempts+?
               WHERE id=? AND community_id=?""",
            (
                lifecycle_status, normalized_health, checked.isoformat(), normalized_error,
                normalized_health, checked.isoformat(), int(reconnect_attempted),
                int(installation_id), int(community_id),
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError("installation not found")
        connection.execute(
            """INSERT INTO installation_health_events(
                   installation_id,community_id,health_status,lifecycle_status,
                   reconnect_attempted,error_message,checked_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                int(installation_id), int(community_id), normalized_health,
                lifecycle_status, int(reconnect_attempted), normalized_error,
                checked.isoformat(),
            ),
        )


def revoke_installation(
    connection: sqlite3.Connection, *, community_id: int, installation_id: int,
    actor_operator_id: int,
) -> None:
    with connection:
        row = connection.execute(
            """SELECT platform,external_community_id,status FROM community_installations
               WHERE id=? AND community_id=?""",
            (int(installation_id), int(community_id)),
        ).fetchone()
        if row is None:
            raise LookupError("installation not found")
        if str(row[2]) == "revoked":
            raise ValueError("installation is already revoked")
        connection.execute(
            """UPDATE community_installations
               SET status='revoked',token_reference=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=?""",
            (int(installation_id), int(community_id)),
        )
        connection.execute(
            "DELETE FROM installation_credentials WHERE installation_id=?",
            (int(installation_id),),
        )
        _audit_access_change(
            connection, actor_operator_id=actor_operator_id,
            action_type="integration.revoked", community_id=community_id,
            payload={
                "installation_id": int(installation_id), "platform": str(row[0]),
                "external_community_id": str(row[1]),
            },
        )


def resolve_community_id(
    connection: sqlite3.Connection, *, platform: str, external_community_id: str
) -> int:
    return resolve_tenant_context(
        connection, platform=platform, external_community_id=external_community_id
    ).community_id


def resolve_tenant_context(
    connection: sqlite3.Connection, *, platform: str, external_community_id: str
) -> TenantContext:
    row = connection.execute(
        """SELECT id,community_id FROM community_installations
           WHERE platform=? AND status='active'
             AND (
                 external_community_id=?
                 OR json_extract(metadata_json,'$.broadcaster_login')=?
                 OR json_extract(metadata_json,'$.broadcaster_id')=?
             )""",
        (
            platform.strip().casefold(), external_community_id.strip(),
            external_community_id.strip().casefold(), external_community_id.strip(),
        ),
    ).fetchone()
    if row is None:
        raise LookupError("active installation not found")
    return TenantContext(community_id=int(row[1]), installation_id=int(row[0]))


def grant_operator_role(
    connection: sqlite3.Connection, *, operator_id: int, community_id: int, role: str,
    actor_operator_id: int | None = None,
) -> None:
    normalized = role.strip().casefold()
    if normalized not in ROLE_PERMISSIONS:
        raise ValueError(f"unsupported community role: {role}")
    with connection:
        existing = connection.execute(
            """SELECT role FROM operator_community_roles
               WHERE operator_id=? AND community_id=?""",
            (int(operator_id), int(community_id)),
        ).fetchone()
        if existing is not None and str(existing[0]).casefold() == "owner" and normalized != "owner":
            owner_count = int(connection.execute(
                """SELECT COUNT(*) FROM operator_community_roles
                   WHERE community_id=? AND role='owner'""",
                (int(community_id),),
            ).fetchone()[0])
            if owner_count <= 1:
                raise ValueError("the last community owner cannot be removed")
        connection.execute(
            """INSERT INTO operator_community_roles(operator_id, community_id, role)
               VALUES (?, ?, ?)
               ON CONFLICT(operator_id, community_id) DO UPDATE SET role=excluded.role""",
            (int(operator_id), int(community_id), normalized),
        )
        if actor_operator_id is not None:
            _invalidate_operator_sessions(connection, operator_id=int(operator_id))
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES (?,?, 'operator.role_granted','community',?,?)""",
            (
                "operator" if actor_operator_id is not None else "system",
                int(actor_operator_id) if actor_operator_id is not None else None,
                int(community_id),
                json.dumps({"operator_id": int(operator_id), "role": normalized}, sort_keys=True),
            ),
        )


def revoke_operator_role(
    connection: sqlite3.Connection, *, operator_id: int, community_id: int,
    actor_operator_id: int | None = None,
) -> bool:
    with connection:
        existing = connection.execute(
            """SELECT role FROM operator_community_roles
               WHERE operator_id=? AND community_id=?""",
            (int(operator_id), int(community_id)),
        ).fetchone()
        if existing is None:
            return False
        if str(existing[0]).casefold() == "owner":
            owner_count = int(connection.execute(
                """SELECT COUNT(*) FROM operator_community_roles
                   WHERE community_id=? AND role='owner'""",
                (int(community_id),),
            ).fetchone()[0])
            if owner_count <= 1:
                raise ValueError("the last community owner cannot be removed")
        connection.execute(
            "DELETE FROM operator_community_roles WHERE operator_id=? AND community_id=?",
            (int(operator_id), int(community_id)),
        )
        if actor_operator_id is not None:
            _invalidate_operator_sessions(connection, operator_id=int(operator_id))
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES (?,?, 'operator.role_revoked','community',?,?)""",
            (
                "operator" if actor_operator_id is not None else "system",
                int(actor_operator_id) if actor_operator_id is not None else None,
                int(community_id),
                json.dumps({"operator_id": int(operator_id), "role": str(existing[0])}, sort_keys=True),
            ),
        )
    return True


def set_operator_permission_override(
    connection: sqlite3.Connection,
    *,
    operator_id: int,
    community_id: int,
    permission: str,
    decision: str | None,
    actor_operator_id: int,
) -> None:
    normalized_permission = permission.strip()
    normalized_decision = str(decision or "").strip().casefold()
    if not normalized_permission or normalized_permission == "*":
        raise ValueError("a specific permission is required")
    if normalized_permission not in DASHBOARD_CAPABILITIES:
        raise ValueError("unsupported dashboard permission")
    if normalized_decision not in {"grant", "deny", "clear"}:
        raise ValueError("permission decision must be grant, deny, or clear")
    with connection:
        if normalized_decision == "clear":
            connection.execute(
                """DELETE FROM operator_permission_overrides
                   WHERE operator_id=? AND community_id=? AND permission=?""",
                (int(operator_id), int(community_id), normalized_permission),
            )
        else:
            connection.execute(
                """INSERT INTO operator_permission_overrides(
                       operator_id,community_id,permission,decision,updated_by_operator_id
                   ) VALUES (?,?,?,?,?)
                   ON CONFLICT(operator_id,community_id,permission) DO UPDATE SET
                       decision=excluded.decision,
                       updated_by_operator_id=excluded.updated_by_operator_id,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    int(operator_id), int(community_id), normalized_permission,
                    normalized_decision, int(actor_operator_id),
                ),
            )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'operator.permission_override','community',?,?)""",
            (
                int(actor_operator_id), int(community_id),
                json.dumps({
                    "decision": normalized_decision,
                    "operator_id": int(operator_id),
                    "permission": normalized_permission,
                }, sort_keys=True),
            ),
        )
        _invalidate_operator_sessions(connection, operator_id=int(operator_id))


def invite_operator(
    connection: sqlite3.Connection, *, tenant: TenantContext, actor: ActorAttribution,
    target_discord_user_id: str, role: str, expires_at: str,
) -> int:
    community_id = tenant.community_id
    actor_operator_id = _require_operator_actor(actor, "operator invitation")
    normalized_role = role.strip().casefold()
    target_id = target_discord_user_id.strip()
    if normalized_role not in ROLE_PERMISSIONS or normalized_role == "owner":
        raise ValueError("invited role must be viewer, analyst, moderator, or admin")
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValueError("invalid operator invitation expiry") from exc
    if not target_id or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("operator invitation target and future expiry are required")
    with connection:
        cursor = connection.execute(
            """INSERT INTO operator_invitations(
                   community_id,target_discord_user_id,invited_role,expires_at,
                   invited_by_operator_id
               ) VALUES (?,?,?,?,?)""",
            (int(community_id), target_id, normalized_role, expiry.isoformat(), int(actor_operator_id)),
        )
        _audit_access_change(
            connection, actor_operator_id=actor_operator_id,
            action_type="operator.invitation_created", community_id=community_id,
            payload={"invitation_id": int(cursor.lastrowid), "target_discord_user_id": target_id,
                     "role": normalized_role, "expires_at": expiry.isoformat()},
        )
    return int(cursor.lastrowid)


def revoke_operator_invitation(
    connection: sqlite3.Connection, *, invitation_id: int, tenant: TenantContext,
    actor: ActorAttribution,
) -> None:
    community_id = tenant.community_id
    actor_operator_id = _require_operator_actor(actor, "operator invitation revocation")
    with connection:
        cursor = connection.execute(
            """UPDATE operator_invitations SET status='revoked',revoked_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=? AND status='pending'""",
            (int(invitation_id), int(community_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError("pending operator invitation not found")
        _audit_access_change(
            connection, actor_operator_id=actor_operator_id,
            action_type="operator.invitation_revoked", community_id=community_id,
            payload={"invitation_id": int(invitation_id)},
        )


def accept_operator_invitations(
    connection: sqlite3.Connection, *, operator_id: int, discord_user_id: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        connection.execute(
            """UPDATE operator_invitations SET status='expired'
               WHERE target_discord_user_id=? AND status='pending'
                 AND julianday(expires_at)<=julianday('now')""",
            (discord_user_id.strip(),),
        )
        rows = connection.execute(
            """SELECT id,community_id,invited_role,invited_by_operator_id
               FROM operator_invitations
               WHERE target_discord_user_id=? AND status='pending'
                 AND julianday(expires_at)>julianday('now') ORDER BY id""",
            (discord_user_id.strip(),),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE operator_accounts SET status='active',session_version=session_version+1 WHERE id=?",
                (int(operator_id),),
            )
            connection.execute(
                """INSERT INTO operator_community_roles(operator_id,community_id,role)
                   VALUES (?,?,?) ON CONFLICT(operator_id,community_id)
                   DO UPDATE SET role=excluded.role""",
                (int(operator_id), int(row[1]), str(row[2])),
            )
            connection.execute(
                """UPDATE operator_invitations
                   SET status='accepted',accepted_by_operator_id=?,accepted_at=? WHERE id=?""",
                (int(operator_id), now, int(row[0])),
            )
            _audit_access_change(
                connection, actor_operator_id=operator_id,
                action_type="operator.invitation_accepted", community_id=int(row[1]),
                payload={"invitation_id": int(row[0]), "role": str(row[2])},
            )
    return len(rows)


def transfer_community_ownership(
    connection: sqlite3.Connection, *, tenant: TenantContext, actor: ActorAttribution,
    new_owner_id: int,
) -> None:
    community_id = tenant.community_id
    current_owner_id = _require_operator_actor(actor, "ownership transfer")
    if int(current_owner_id) == int(new_owner_id):
        raise ValueError("new owner must be a different operator")
    with connection:
        roles = connection.execute(
            """SELECT operator_id,role FROM operator_community_roles
               WHERE community_id=? AND operator_id IN (?,?)""",
            (int(community_id), int(current_owner_id), int(new_owner_id)),
        ).fetchall()
        role_map = {int(row[0]): str(row[1]) for row in roles}
        if role_map.get(int(current_owner_id)) != "owner" or int(new_owner_id) not in role_map:
            raise PermissionError("ownership transfer requires the current owner and an existing operator")
        connection.execute(
            "UPDATE operator_community_roles SET role='admin' WHERE operator_id=? AND community_id=?",
            (int(current_owner_id), int(community_id)),
        )
        connection.execute(
            "UPDATE operator_community_roles SET role='owner' WHERE operator_id=? AND community_id=?",
            (int(new_owner_id), int(community_id)),
        )
        _invalidate_operator_sessions(connection, operator_id=int(current_owner_id))
        _invalidate_operator_sessions(connection, operator_id=int(new_owner_id))
        _audit_access_change(
            connection, actor_operator_id=current_owner_id,
            action_type="operator.ownership_transferred", community_id=community_id,
            payload={"previous_owner_id": int(current_owner_id), "new_owner_id": int(new_owner_id)},
        )


def emergency_remove_operator_access(
    connection: sqlite3.Connection, *, tenant: TenantContext, actor: ActorAttribution,
    operator_id: int, reason: str,
) -> None:
    community_id = tenant.community_id
    actor_operator_id = _require_operator_actor(actor, "emergency access removal")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("emergency removal reason is required")
    with connection:
        role = connection.execute(
            "SELECT role FROM operator_community_roles WHERE operator_id=? AND community_id=?",
            (int(operator_id), int(community_id)),
        ).fetchone()
        if role is None:
            raise LookupError("operator community access not found")
        if str(role[0]) == "owner":
            raise ValueError("transfer ownership before emergency removal")
        connection.execute(
            "DELETE FROM operator_permission_overrides WHERE operator_id=? AND community_id=?",
            (int(operator_id), int(community_id)),
        )
        connection.execute(
            "DELETE FROM operator_community_roles WHERE operator_id=? AND community_id=?",
            (int(operator_id), int(community_id)),
        )
        connection.execute(
            "UPDATE operator_accounts SET status='disabled' WHERE id=?",
            (int(operator_id),),
        )
        _invalidate_operator_sessions(connection, operator_id=int(operator_id))
        _audit_access_change(
            connection, actor_operator_id=actor_operator_id,
            action_type="operator.access_emergency_removed", community_id=community_id,
            payload={"operator_id": int(operator_id), "reason": normalized_reason},
        )


def _invalidate_operator_sessions(connection: sqlite3.Connection, *, operator_id: int) -> None:
    connection.execute(
        "UPDATE operator_accounts SET session_version=session_version+1 WHERE id=?",
        (int(operator_id),),
    )


def _require_operator_actor(actor: ActorAttribution, action: str) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise PermissionError(f"{action} requires an operator actor")
    return actor.actor_id


def _audit_access_change(
    connection: sqlite3.Connection, *, actor_operator_id: int, action_type: str,
    community_id: int, payload: Mapping[str, object],
) -> None:
    connection.execute(
        """INSERT INTO audit_log(
               actor_type,actor_id,action_type,entity_type,entity_id,payload_json
           ) VALUES ('operator',?,?,'community',?,?)""",
        (int(actor_operator_id), action_type, int(community_id), json.dumps(dict(payload), sort_keys=True)),
    )


def operator_has_permission(
    connection: sqlite3.Connection, *, operator_id: int, community_id: int, permission: str
) -> bool:
    override = connection.execute(
        """SELECT decision FROM operator_permission_overrides
           WHERE operator_id=? AND community_id=? AND permission=?""",
        (int(operator_id), int(community_id), permission.strip()),
    ).fetchone()
    if override is not None:
        return str(override[0]) == "grant"
    row = connection.execute(
        "SELECT role FROM operator_community_roles WHERE operator_id=? AND community_id=?",
        (int(operator_id), int(community_id)),
    ).fetchone()
    if row is None:
        return False
    permissions = ROLE_PERMISSIONS.get(str(row[0]).casefold(), frozenset())
    return "*" in permissions or permission in permissions


def record_operator_discord_guild_permissions(
    connection: sqlite3.Connection,
    *,
    operator_id: int,
    permissions: Mapping[str, str],
    actor_operator_id: int | None = None,
) -> None:
    rows: list[tuple[int, str, int]] = []
    for guild_id, raw_permissions in permissions.items():
        normalized_guild_id = str(guild_id).strip()
        try:
            permission_bits = int(raw_permissions)
        except (TypeError, ValueError):
            continue
        if normalized_guild_id:
            rows.append((int(operator_id), normalized_guild_id, permission_bits))
    with connection:
        connection.execute(
            "DELETE FROM operator_discord_guild_permissions WHERE operator_id=?",
            (int(operator_id),),
        )
        connection.executemany(
            """INSERT INTO operator_discord_guild_permissions(
                   operator_id, guild_id, permissions
               ) VALUES (?, ?, ?)""",
            rows,
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES (?,?, 'operator.discord_guild_permissions_refreshed',
                         'operator_account',?,?)""",
            (
                "operator" if actor_operator_id is not None else "system",
                int(actor_operator_id) if actor_operator_id is not None else None,
                int(operator_id),
                json.dumps({
                    "guild_permissions": {
                        guild_id: permission_bits for _, guild_id, permission_bits in rows
                    }
                }, sort_keys=True),
            ),
        )


def create_discord_install_intent(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    guild_id: str,
    expires_at: str,
    pilot_invite_code: str,
) -> None:
    normalized_nonce = nonce.strip()
    normalized_guild_id = guild_id.strip()
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValueError("invalid Discord installation intent expiry") from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("Discord installation intent expiry must be in the future")
    guild_row = connection.execute(
        """SELECT permissions FROM operator_discord_guild_permissions
           WHERE operator_id=? AND guild_id=?""",
        (int(operator_id), normalized_guild_id),
    ).fetchone()
    guild_permissions = int(guild_row[0]) if guild_row is not None else 0
    can_manage_guild = bool(guild_permissions & ((1 << 3) | (1 << 5)))
    if (
        not operator_has_permission(
            connection, operator_id=operator_id, community_id=community_id,
            permission="integrations.manage",
        )
        or not can_manage_guild
    ):
        raise PermissionError("Discord installation is not authorized")
    existing_installation = connection.execute(
        """SELECT community_id FROM community_installations
           WHERE platform='discord' AND external_community_id=?""",
        (normalized_guild_id,),
    ).fetchone()
    if existing_installation is not None and int(existing_installation[0]) != int(community_id):
        raise PermissionError("Discord guild is already linked to another community")
    if not normalized_nonce or not normalized_guild_id:
        raise ValueError("Discord installation intent fields are required")
    invite_hash = hashlib.sha256(pilot_invite_code.strip().encode("utf-8")).hexdigest()
    with connection:
        invite_cursor = connection.execute(
            """UPDATE pilot_invitations
               SET consumed_at=CURRENT_TIMESTAMP,consumed_by_operator_id=?
               WHERE community_id=? AND code_hash=? AND consumed_at IS NULL
                 AND julianday(expires_at)>julianday('now')""",
            (int(operator_id), int(community_id), invite_hash),
        )
        if invite_cursor.rowcount != 1:
            raise PermissionError("pilot invitation is invalid, expired, or already used")
        connection.execute(
            """INSERT INTO discord_install_intents(
                   nonce, operator_id, community_id, guild_id, expires_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                normalized_nonce, int(operator_id), int(community_id),
                normalized_guild_id, expiry.isoformat(),
            ),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'integration.discord_link_intent_created',
                         'community',?,?)""",
            (
                int(operator_id), int(community_id),
                json.dumps({"guild_id": normalized_guild_id, "nonce": normalized_nonce}, sort_keys=True),
            ),
        )


def issue_pilot_invitation(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    expires_at: str,
    created_by_operator_id: int | None = None,
) -> str:
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValueError("invalid pilot invitation expiry") from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("pilot invitation expiry must be in the future")
    code = secrets.token_urlsafe(24)
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    with connection:
        cursor = connection.execute(
            """INSERT INTO pilot_invitations(
                   community_id,code_hash,expires_at,created_by_operator_id
               ) VALUES (?,?,?,?)""",
            (int(community_id), code_hash, expiry.isoformat(), created_by_operator_id),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES (?,?, 'pilot.invitation_issued','pilot_invitation',?,?)""",
            (
                "operator" if created_by_operator_id is not None else "system",
                created_by_operator_id, int(cursor.lastrowid),
                json.dumps({"community_id": int(community_id), "expires_at": expiry.isoformat()}, sort_keys=True),
            ),
        )
    return code


def consume_discord_install_intent(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    guild_id: str,
) -> bool:
    with connection:
        cursor = connection.execute(
            """UPDATE discord_install_intents
               SET consumed_at=CURRENT_TIMESTAMP
               WHERE nonce=? AND operator_id=? AND community_id=? AND guild_id=?
                 AND consumed_at IS NULL
                 AND julianday(expires_at) > julianday('now')""",
            (
                nonce.strip(), int(operator_id), int(community_id), guild_id.strip(),
            ),
        )
        if cursor.rowcount == 1:
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('operator',?,'integration.discord_link_intent_consumed',
                             'community',?,?)""",
                (
                    int(operator_id), int(community_id),
                    json.dumps({"guild_id": guild_id.strip(), "nonce": nonce.strip()}, sort_keys=True),
                ),
            )
    return cursor.rowcount == 1


def discord_install_intent_is_pending(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    guild_id: str,
) -> bool:
    row = connection.execute(
        """SELECT 1 FROM discord_install_intents
           WHERE nonce=? AND operator_id=? AND community_id=? AND guild_id=?
             AND consumed_at IS NULL
             AND julianday(expires_at) > julianday('now')""",
        (nonce.strip(), int(operator_id), int(community_id), guild_id.strip()),
    ).fetchone()
    return row is not None


def complete_discord_install_intent(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    guild_id: str,
) -> bool:
    normalized_guild_id = guild_id.strip()
    with connection:
        existing = connection.execute(
            """SELECT community_id, status FROM community_installations
               WHERE platform='discord' AND external_community_id=?""",
            (normalized_guild_id,),
        ).fetchone()
        if existing is not None and int(existing[0]) != int(community_id):
            raise PermissionError("Discord guild is already linked to another community")
        cursor = connection.execute(
            """UPDATE discord_install_intents
               SET consumed_at=CURRENT_TIMESTAMP
               WHERE nonce=? AND operator_id=? AND community_id=? AND guild_id=?
                 AND consumed_at IS NULL
                 AND julianday(expires_at) > julianday('now')""",
            (nonce.strip(), int(operator_id), int(community_id), normalized_guild_id),
        )
        if cursor.rowcount != 1:
            return False
        status = "active" if existing is not None and str(existing[1]) == "active" else "pending"
        connection.execute(
            """INSERT INTO community_installations(
                   community_id, platform, external_community_id, display_name,
                   status, scopes_json, capabilities_json
               ) VALUES (?, 'discord', ?, ?, ?, ?, ?)
               ON CONFLICT(platform, external_community_id) DO UPDATE SET
                   display_name=excluded.display_name,
                   status=excluded.status,
                   scopes_json=excluded.scopes_json,
                   capabilities_json=excluded.capabilities_json,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), normalized_guild_id, normalized_guild_id, status,
                json.dumps(["applications.commands", "bot"]),
                json.dumps(sorted(platform_capabilities("discord"))),
            ),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type, actor_id, action_type, entity_type, entity_id, payload_json
               ) VALUES ('operator', ?, 'integration.discord_link_pending',
                         'community_installation', ?, ?)""",
            (
                int(operator_id), normalized_guild_id,
                json.dumps(
                    {"community_id": int(community_id), "guild_id": normalized_guild_id},
                    sort_keys=True,
                ),
            ),
        )
    return True


def create_twitch_install_intent(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    broadcaster_login: str,
    scopes: tuple[str, ...],
    expires_at: str,
) -> None:
    normalized_nonce = nonce.strip()
    normalized_login = broadcaster_login.strip().casefold()
    normalized_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValueError("invalid Twitch installation intent expiry") from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("Twitch installation intent expiry must be in the future")
    if not normalized_nonce or not normalized_login or not normalized_scopes:
        raise ValueError("Twitch installation intent fields are required")
    if set(normalized_scopes) - TWITCH_INSTALL_OAUTH_SCOPES:
        raise ValueError("Twitch installation requested unsupported scopes")
    if not operator_has_permission(
        connection, operator_id=operator_id, community_id=community_id,
        permission="integrations.manage",
    ):
        raise PermissionError("Twitch installation is not authorized")
    existing = connection.execute(
        """SELECT community_id FROM community_installations
           WHERE platform='twitch' AND (
               external_community_id=?
               OR json_extract(metadata_json,'$.broadcaster_login')=?
           )""",
        (normalized_login, normalized_login),
    ).fetchone()
    if existing is not None and int(existing[0]) != int(community_id):
        raise PermissionError("Twitch broadcaster is already linked to another community")
    with connection:
        connection.execute(
            """INSERT INTO twitch_install_intents(
                   nonce,operator_id,community_id,broadcaster_login,scopes_json,expires_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                normalized_nonce, int(operator_id), int(community_id), normalized_login,
                json.dumps(normalized_scopes), expiry.isoformat(),
            ),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'integration.twitch_link_intent_created',
                         'community',?,?)""",
            (
                int(operator_id), int(community_id),
                json.dumps({"broadcaster_login": normalized_login, "nonce": normalized_nonce}, sort_keys=True),
            ),
        )


def twitch_install_intent_is_pending(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    broadcaster_login: str,
) -> bool:
    return connection.execute(
        """SELECT 1 FROM twitch_install_intents
           WHERE nonce=? AND operator_id=? AND community_id=? AND broadcaster_login=?
             AND consumed_at IS NULL AND julianday(expires_at)>julianday('now')""",
        (
            nonce.strip(), int(operator_id), int(community_id),
            broadcaster_login.strip().casefold(),
        ),
    ).fetchone() is not None


def complete_twitch_install_intent(
    connection: sqlite3.Connection,
    *,
    nonce: str,
    operator_id: int,
    community_id: int,
    broadcaster_login: str,
    broadcaster_id: str,
    access_token: str,
    refresh_token: str | None,
    scopes: tuple[str, ...],
    encryption_key: str,
) -> int:
    from ..credential_store import store_installation_credentials

    normalized_login = broadcaster_login.strip().casefold()
    normalized_broadcaster_id = broadcaster_id.strip()
    if not normalized_broadcaster_id:
        raise ValueError("Twitch broadcaster ID is required")
    normalized_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
    with connection:
        intent = connection.execute(
            """SELECT scopes_json FROM twitch_install_intents
               WHERE nonce=? AND operator_id=? AND community_id=? AND broadcaster_login=?
                 AND consumed_at IS NULL AND julianday(expires_at)>julianday('now')""",
            (nonce.strip(), int(operator_id), int(community_id), normalized_login),
        ).fetchone()
        if intent is None:
            raise PermissionError("Twitch installation intent is invalid, expired, or already consumed")
        requested_scopes = tuple(json.loads(str(intent[0])))
        if not set(requested_scopes).issubset(normalized_scopes):
            raise PermissionError("Twitch did not grant all reviewed scopes")
        existing = connection.execute(
            """SELECT community_id FROM community_installations
               WHERE platform='twitch' AND (
                   external_community_id=?
                   OR json_extract(metadata_json,'$.broadcaster_login')=?
                   OR json_extract(metadata_json,'$.broadcaster_id')=?
               )""",
            (normalized_login, normalized_login, normalized_broadcaster_id),
        ).fetchone()
        if existing is not None and int(existing[0]) != int(community_id):
            raise PermissionError("Twitch broadcaster is already linked to another community")
        connection.execute(
            """INSERT INTO community_installations(
                   community_id,platform,external_community_id,display_name,status,
                   scopes_json,metadata_json,capabilities_json,health_status
               ) VALUES (?,'twitch',?,?,'pending',?,?,?,'unknown')
               ON CONFLICT(platform,external_community_id) DO UPDATE SET
                   display_name=excluded.display_name,status='pending',
                   scopes_json=excluded.scopes_json,metadata_json=excluded.metadata_json,
                   capabilities_json=excluded.capabilities_json,health_status='unknown',
                   last_error=NULL,updated_at=CURRENT_TIMESTAMP""",
            (
                int(community_id), normalized_broadcaster_id, normalized_login,
                json.dumps(normalized_scopes), json.dumps({
                    "broadcaster_id": normalized_broadcaster_id,
                    "broadcaster_login": normalized_login,
                    "moderation_mode": "shadow",
                }, sort_keys=True),
                json.dumps(sorted(platform_capabilities("twitch"))),
            ),
        )
        installation_id = int(connection.execute(
            """SELECT id FROM community_installations
                    WHERE community_id=? AND platform='twitch' AND external_community_id=?""",
                (int(community_id), normalized_broadcaster_id),
        ).fetchone()[0])
        store_installation_credentials(
            connection, community_id=community_id, installation_id=installation_id,
            access_token=access_token, refresh_token=refresh_token,
            scopes=normalized_scopes, encryption_key=encryption_key,
            actor_operator_id=operator_id,
        )
        connection.execute(
            "UPDATE twitch_install_intents SET consumed_at=CURRENT_TIMESTAMP WHERE nonce=?",
            (nonce.strip(),),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'integration.twitch_link_pending',
                         'community_installation',?,?)""",
            (
                int(operator_id), installation_id,
                json.dumps({"community_id": int(community_id), "broadcaster_login": normalized_login}, sort_keys=True),
            ),
        )
    return installation_id


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
