from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Mapping

from ..contexts import ActorAttribution, TenantContext


def configure_retention_policy(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    message_retention_days: int,
    analytics_retention_days: int,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("retention policy changes require an operator actor")
    message_days = int(message_retention_days)
    analytics_days = int(analytics_retention_days)
    for name, value in (
        ("message_retention_days", message_days),
        ("analytics_retention_days", analytics_days),
    ):
        if value < 1 or value > 3650:
            raise ValueError(f"{name} must be between 1 and 3650")
    with connection:
        cursor = connection.execute(
            """UPDATE community_policy_settings
               SET message_retention_days=?,analytics_retention_days=?,
                   updated_by_operator_id=?,updated_at=CURRENT_TIMESTAMP
               WHERE community_id=?""",
            (message_days, analytics_days, actor.actor_id, tenant.community_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("community policy settings not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'retention.policy_updated','community',?,?)""",
            (actor.actor_id, tenant.community_id, json.dumps({
                "message_retention_days": message_days,
                "analytics_retention_days": analytics_days,
            }, sort_keys=True)),
        )


def create_legal_hold(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    reason: str,
    case_id: int | None = None,
) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("legal hold changes require an operator actor")
    if not reason.strip():
        raise ValueError("legal hold reason is required")
    with connection:
        cursor = connection.execute(
            """INSERT INTO legal_holds(community_id,case_id,reason,created_by_operator_id)
               VALUES (?,?,?,?)""",
                (tenant.community_id, case_id, reason.strip(), actor.actor_id),
        )
        connection.execute(
            """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
               VALUES ('operator',?,'legal_hold.created','legal_hold',?,?)""",
            (actor.actor_id, int(cursor.lastrowid), json.dumps({
                "community_id": tenant.community_id, "case_id": case_id,
            })),
        )
    return int(cursor.lastrowid)


def release_legal_hold(
    connection: sqlite3.Connection, *, tenant: TenantContext,
    actor: ActorAttribution, hold_id: int,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("legal hold changes require an operator actor")
    with connection:
        cursor = connection.execute(
            """UPDATE legal_holds SET status='released',released_at=CURRENT_TIMESTAMP
               WHERE id=? AND community_id=? AND status='active'""",
            (int(hold_id), tenant.community_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("active legal hold was not found")
        connection.execute(
            """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id)
               VALUES ('operator',?,'legal_hold.released','legal_hold',?)""",
            (actor.actor_id, int(hold_id)),
        )


def create_data_subject_request(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    request_type: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("data subject requests require an operator actor")
    normalized_type = request_type.strip().casefold()
    if normalized_type not in {"access", "export", "delete", "correct", "restrict"}:
        raise ValueError("unsupported data subject request type")
    with connection:
        organization = connection.execute(
            """SELECT w.organization_id FROM communities c
               JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?""",
            (tenant.community_id,),
        ).fetchone()
        if organization is None:
            raise LookupError("community not found")
        cursor = connection.execute(
            """INSERT INTO data_subject_requests(
                   organization_id,community_id,request_type,platform,platform_user_id,
                   requested_by_operator_id
               ) VALUES (?,?,?,?,?,?)""",
            (int(organization[0]), tenant.community_id, normalized_type,
             platform.strip().casefold() if platform else None,
             platform_user_id.strip() if platform_user_id else None, actor.actor_id),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'data_subject_request.created',
                         'data_subject_request',?,?)""",
            (actor.actor_id, int(cursor.lastrowid), json.dumps({
                "community_id": tenant.community_id, "request_type": normalized_type,
            }, sort_keys=True)),
        )
    return int(cursor.lastrowid)


def complete_data_subject_request(
    connection: sqlite3.Connection, *, tenant: TenantContext,
    actor: ActorAttribution, request_id: int, result: Mapping[str, object]
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("data subject request completion requires an operator actor")
    with connection:
        cursor = connection.execute(
            """UPDATE data_subject_requests SET status='completed',completed_at=CURRENT_TIMESTAMP,
                   completed_by_operator_id=?,result_json=?
               WHERE id=? AND community_id=? AND status='open'""",
            (actor.actor_id, json.dumps(dict(result), sort_keys=True, default=str),
             int(request_id), tenant.community_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("open data subject request was not found")
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'data_subject_request.completed',
                         'data_subject_request',?,?)""",
            (actor.actor_id, int(request_id), json.dumps({
                "community_id": tenant.community_id,
            }, sort_keys=True)),
        )


def fulfill_data_subject_request(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    request_id: int,
) -> dict[str, object]:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("data subject request fulfillment requires an operator actor")
    request = connection.execute(
        """SELECT request_type,platform,platform_user_id FROM data_subject_requests
           WHERE id=? AND community_id=? AND status='open'""",
        (int(request_id), tenant.community_id),
    ).fetchone()
    if request is None:
        raise ValueError("open data subject request was not found")
    request_type = str(request[0])
    if request_type not in {"export", "delete"}:
        raise ValueError("request type does not support automated fulfillment")
    if request[1] is None or request[2] is None:
        raise ValueError("platform subject identity is required")
    account = connection.execute(
        """SELECT id,user_id,platform,platform_user_id,username
           FROM platform_accounts WHERE platform=? AND platform_user_id=?""",
        (str(request[1]), str(request[2])),
    ).fetchone()
    if account is None:
        raise LookupError("platform subject was not found")
    account_id = int(account[0])
    user_id = int(account[1]) if account[1] is not None else None
    belongs_to_tenant = connection.execute(
        """SELECT 1 WHERE
               EXISTS (SELECT 1 FROM community_memberships
                       WHERE community_id=? AND platform_account_id=?)
               OR EXISTS (SELECT 1 FROM messages
                          WHERE community_id=? AND platform_account_id=?)
               OR EXISTS (SELECT 1 FROM moderation_actions
                          WHERE community_id=? AND target_platform_account_id=?)""",
        (tenant.community_id, account_id, tenant.community_id, account_id,
         tenant.community_id, account_id),
    ).fetchone()
    if belongs_to_tenant is None:
        raise LookupError("platform subject was not found in community")
    if request_type == "export":
        result: dict[str, object] = {
            "community_id": tenant.community_id,
            "subject": dict(account),
            "messages": [dict(row) for row in connection.execute(
                """SELECT id,platform_message_id,channel_id,content_raw,sent_at,edited_at,deleted_at
                   FROM messages WHERE community_id=? AND platform_account_id=? ORDER BY id""",
                (tenant.community_id, account_id),
            ).fetchall()],
            "moderation_actions": [dict(row) for row in connection.execute(
                """SELECT id,action_type,reason,status,created_at,completed_at
                   FROM moderation_actions
                   WHERE community_id=? AND target_platform_account_id=? ORDER BY id""",
                (tenant.community_id, account_id),
            ).fetchall()],
        }
    else:
        active_hold = connection.execute(
            "SELECT 1 FROM legal_holds WHERE community_id=? AND status='active' LIMIT 1",
            (tenant.community_id,),
        ).fetchone()
        if active_hold is not None:
            raise ValueError("active legal hold prevents deletion fulfillment")
        message_ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM messages WHERE community_id=? AND platform_account_id=?",
            (tenant.community_id, account_id),
        ).fetchall()]
        with connection:
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                connection.execute(
                    f"DELETE FROM message_attachments WHERE message_id IN ({placeholders})",
                    tuple(message_ids),
                )
            redacted_messages = connection.execute(
                """UPDATE messages SET content_raw='[deleted by privacy request]',
                       content_normalized='[deleted by privacy request]',
                       platform_message_id=NULL,deleted_at=CURRENT_TIMESTAMP
                   WHERE community_id=? AND platform_account_id=?""",
                (tenant.community_id, account_id),
            ).rowcount
            removed_memberships = connection.execute(
                """DELETE FROM community_memberships
                   WHERE community_id=? AND platform_account_id=?""",
                (tenant.community_id, account_id),
            ).rowcount
            removed_notes = 0 if user_id is None else connection.execute(
                "DELETE FROM user_notes WHERE community_id=? AND user_id=?",
                (tenant.community_id, user_id),
            ).rowcount
            connection.execute(
                """UPDATE member_reports SET summary='[deleted by privacy request]',
                       evidence_json='{}'
                   WHERE community_id=? AND
                         (reporter_platform_account_id=? OR subject_platform_account_id=?)""",
                (tenant.community_id, account_id, account_id),
            )
        result = {
            "community_id": tenant.community_id,
            "redacted_messages": int(redacted_messages),
            "removed_memberships": int(removed_memberships),
            "removed_notes": int(removed_notes),
        }
    complete_data_subject_request(
        connection, tenant=tenant, actor=actor, request_id=request_id, result=result,
    )
    return result


def offboard_community(
    connection: sqlite3.Connection,
    *,
    tenant: TenantContext,
    actor: ActorAttribution,
    export_reference: str,
) -> int:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("community offboarding requires an operator actor")
    reference = export_reference.strip()
    if not reference:
        raise ValueError("completed tenant export reference is required")
    with connection:
        community = connection.execute(
            "SELECT status FROM communities WHERE id=?",
            (tenant.community_id,),
        ).fetchone()
        if community is None:
            raise LookupError("community not found")
        if str(community[0]) != "active":
            raise ValueError("community is not active")
        installation_ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM community_installations WHERE community_id=?",
            (tenant.community_id,),
        ).fetchall()]
        if installation_ids:
            placeholders = ",".join("?" for _ in installation_ids)
            connection.execute(
                f"DELETE FROM installation_credentials WHERE installation_id IN ({placeholders})",
                tuple(installation_ids),
            )
        connection.execute(
            """UPDATE community_installations
               SET status='revoked',token_reference=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE community_id=?""",
            (tenant.community_id,),
        )
        connection.execute(
            """UPDATE communities SET status='offboarded',updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (tenant.community_id,),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'community.offboarded','community',?,?)""",
            (actor.actor_id, tenant.community_id, json.dumps({
                "export_reference": reference,
                "revoked_installations": len(installation_ids),
            }, sort_keys=True)),
        )
    return len(installation_ids)


def create_api_client(
    connection: sqlite3.Connection,
    *,
    organization_id: int,
    community_id: int,
    name: str,
    scopes: Iterable[str],
    rate_limit_per_minute: int = 120,
) -> tuple[int, str]:
    plaintext_key = "qbot_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    normalized_scopes = sorted({scope.strip() for scope in scopes if scope.strip()})
    community = connection.execute(
        """SELECT 1 FROM communities c
           JOIN workspaces w ON w.id=c.workspace_id
           WHERE c.id=? AND w.organization_id=?""",
        (int(community_id), int(organization_id)),
    ).fetchone()
    if community is None:
        raise ValueError("community does not belong to organization")
    with connection:
        cursor = connection.execute(
            """INSERT INTO api_clients(
                   organization_id,community_id,name,key_hash,scopes_json,rate_limit_per_minute
               ) VALUES (?,?,?,?,?,?)""",
            (int(organization_id), int(community_id), name.strip(), key_hash, json.dumps(normalized_scopes),
             max(1, min(int(rate_limit_per_minute), 10_000))),
        )
    return int(cursor.lastrowid), plaintext_key


def authorize_api_client(
    connection: sqlite3.Connection, *, plaintext_key: str, required_scope: str
) -> int | None:
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    row = connection.execute(
        "SELECT id,scopes_json,rate_limit_per_minute FROM api_clients WHERE key_hash=? AND status='active'",
        (key_hash,),
    ).fetchone()
    if row is None:
        return None
    scopes = json.loads(str(row[1]) or "[]")
    if required_scope not in scopes and "*" not in scopes:
        return None
    minute = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    with connection:
        connection.execute(
            """INSERT INTO api_request_usage(api_client_id,minute_bucket,request_count)
               VALUES (?,?,1) ON CONFLICT(api_client_id,minute_bucket)
               DO UPDATE SET request_count=request_count+1""",
            (int(row[0]), minute),
        )
        count = int(connection.execute(
            "SELECT request_count FROM api_request_usage WHERE api_client_id=? AND minute_bucket=?",
            (int(row[0]), minute),
        ).fetchone()[0])
    return int(row[0]) if count <= int(row[2]) else None


def authorize_api_client_for_community(
    connection: sqlite3.Connection,
    *,
    plaintext_key: str,
    required_scope: str,
    community_id: int,
) -> int | None:
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    row = connection.execute(
        """SELECT id,scopes_json,rate_limit_per_minute FROM api_clients
           WHERE key_hash=? AND community_id=? AND status='active'""",
        (key_hash, int(community_id)),
    ).fetchone()
    if row is None:
        return None
    scopes = json.loads(str(row[1]) or "[]")
    if required_scope not in scopes and "*" not in scopes:
        return None
    minute = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    with connection:
        connection.execute(
            """INSERT INTO api_request_usage(api_client_id,minute_bucket,request_count)
               VALUES (?,?,1) ON CONFLICT(api_client_id,minute_bucket)
               DO UPDATE SET request_count=request_count+1""",
            (int(row[0]), minute),
        )
        count = int(connection.execute(
            "SELECT request_count FROM api_request_usage WHERE api_client_id=? AND minute_bucket=?",
            (int(row[0]), minute),
        ).fetchone()[0])
    return int(row[0]) if count <= int(row[2]) else None
