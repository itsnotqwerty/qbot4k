from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Mapping


def create_legal_hold(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    reason: str,
    case_id: int | None = None,
    operator_id: int | None = None,
) -> int:
    if not reason.strip():
        raise ValueError("legal hold reason is required")
    with connection:
        cursor = connection.execute(
            """INSERT INTO legal_holds(community_id,case_id,reason,created_by_operator_id)
               VALUES (?,?,?,?)""",
            (int(community_id), case_id, reason.strip(), operator_id),
        )
        connection.execute(
            """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json)
               VALUES ('operator',?,'legal_hold.created','legal_hold',?,?)""",
            (operator_id, int(cursor.lastrowid), json.dumps({"community_id": int(community_id), "case_id": case_id})),
        )
    return int(cursor.lastrowid)


def release_legal_hold(
    connection: sqlite3.Connection, *, hold_id: int, operator_id: int | None = None
) -> None:
    with connection:
        cursor = connection.execute(
            """UPDATE legal_holds SET status='released',released_at=CURRENT_TIMESTAMP
               WHERE id=? AND status='active'""",
            (int(hold_id),),
        )
        if cursor.rowcount != 1:
            raise ValueError("active legal hold was not found")
        connection.execute(
            """INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id)
               VALUES ('operator',?,'legal_hold.released','legal_hold',?)""",
            (operator_id, int(hold_id)),
        )


def create_data_subject_request(
    connection: sqlite3.Connection,
    *,
    organization_id: int,
    request_type: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
) -> int:
    normalized_type = request_type.strip().casefold()
    if normalized_type not in {"access", "export", "delete", "correct", "restrict"}:
        raise ValueError("unsupported data subject request type")
    with connection:
        cursor = connection.execute(
            """INSERT INTO data_subject_requests(
                   organization_id,request_type,platform,platform_user_id
               ) VALUES (?,?,?,?)""",
            (int(organization_id), normalized_type,
             platform.strip().casefold() if platform else None,
             platform_user_id.strip() if platform_user_id else None),
        )
    return int(cursor.lastrowid)


def complete_data_subject_request(
    connection: sqlite3.Connection, *, request_id: int, result: Mapping[str, object]
) -> None:
    with connection:
        cursor = connection.execute(
            """UPDATE data_subject_requests SET status='completed',completed_at=CURRENT_TIMESTAMP,
                   result_json=? WHERE id=? AND status='open'""",
            (json.dumps(dict(result), sort_keys=True, default=str), int(request_id)),
        )
    if cursor.rowcount != 1:
        raise ValueError("open data subject request was not found")


def create_api_client(
    connection: sqlite3.Connection,
    *,
    organization_id: int,
    name: str,
    scopes: Iterable[str],
    rate_limit_per_minute: int = 120,
) -> tuple[int, str]:
    plaintext_key = "qbot_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    normalized_scopes = sorted({scope.strip() for scope in scopes if scope.strip()})
    with connection:
        cursor = connection.execute(
            """INSERT INTO api_clients(
                   organization_id,name,key_hash,scopes_json,rate_limit_per_minute
               ) VALUES (?,?,?,?,?)""",
            (int(organization_id), name.strip(), key_hash, json.dumps(normalized_scopes),
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
