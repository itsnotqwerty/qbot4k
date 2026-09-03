from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .contexts import ActorAttribution, TenantContext


@dataclass(frozen=True)
class InstallationCredentials:
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    key_version: int
    rotation_count: int


def store_installation_credentials(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    installation_id: int,
    access_token: str,
    refresh_token: str | None,
    scopes: tuple[str, ...],
    encryption_key: str,
    key_version: int = 1,
    actor_operator_id: int,
) -> str:
    tenant = TenantContext.require(community_id, installation_id=installation_id)
    actor = ActorAttribution("operator", actor_operator_id)
    token = access_token.strip()
    if not token:
        raise ValueError("access_token is required")
    cipher = _cipher(encryption_key)
    normalized_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
    installation = connection.execute(
        """SELECT status FROM community_installations
           WHERE id=? AND community_id=?""",
        (int(tenant.installation_id), tenant.community_id),
    ).fetchone()
    if installation is None or str(installation[0]) == "revoked":
        raise LookupError("installation not found")
    existing = connection.execute(
        "SELECT id,rotation_count FROM installation_credentials WHERE installation_id=?",
        (int(installation_id),),
    ).fetchone()
    encrypted_access = cipher.encrypt(token.encode("utf-8"))
    encrypted_refresh = (
        cipher.encrypt(refresh_token.strip().encode("utf-8"))
        if refresh_token and refresh_token.strip()
        else None
    )
    with connection:
        connection.execute(
            """INSERT INTO installation_credentials(
                   installation_id,access_token_ciphertext,refresh_token_ciphertext,
                   scopes_json,key_version,rotation_count
               ) VALUES (?,?,?,?,?,1)
               ON CONFLICT(installation_id) DO UPDATE SET
                   access_token_ciphertext=excluded.access_token_ciphertext,
                   refresh_token_ciphertext=excluded.refresh_token_ciphertext,
                   scopes_json=excluded.scopes_json,
                   key_version=excluded.key_version,
                   rotation_count=installation_credentials.rotation_count+1,
                   rotated_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                int(tenant.installation_id), encrypted_access, encrypted_refresh,
                json.dumps(normalized_scopes), int(key_version),
            ),
        )
        credential_id = int(connection.execute(
            "SELECT id FROM installation_credentials WHERE installation_id=?",
            (int(tenant.installation_id),),
        ).fetchone()[0])
        reference = f"installation-credential:{credential_id}"
        connection.execute(
            """UPDATE community_installations SET token_reference=?,scopes_json=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND community_id=?""",
            (reference, json.dumps(normalized_scopes), int(tenant.installation_id), tenant.community_id),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'integration.credentials_rotated',
                         'community_installation',?,?)""",
            (
                int(actor.actor_id), int(tenant.installation_id),
                json.dumps({
                    "community_id": tenant.community_id,
                    "key_version": int(key_version),
                    "rotation_count": int(existing[1]) + 1 if existing is not None else 1,
                    "scopes": normalized_scopes,
                }, sort_keys=True),
            ),
        )
    return reference


def load_installation_credentials(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    installation_id: int,
    encryption_key: str,
) -> InstallationCredentials:
    tenant = TenantContext.require(community_id, installation_id=installation_id)
    row = connection.execute(
        """SELECT c.access_token_ciphertext,c.refresh_token_ciphertext,c.scopes_json,
                  c.key_version,c.rotation_count
           FROM installation_credentials c
           JOIN community_installations i ON i.id=c.installation_id
           WHERE c.installation_id=? AND i.community_id=?
             AND i.status IN ('pending','active','degraded')""",
        (int(tenant.installation_id), tenant.community_id),
    ).fetchone()
    if row is None:
        raise LookupError("installation credentials not found")
    cipher = _cipher(encryption_key)
    try:
        access_token = cipher.decrypt(bytes(row[0])).decode("utf-8")
        refresh_token = (
            cipher.decrypt(bytes(row[1])).decode("utf-8") if row[1] is not None else None
        )
    except InvalidToken as exc:
        raise ValueError("installation credentials cannot be decrypted") from exc
    scopes_raw = json.loads(str(row[2]) or "[]")
    scopes = tuple(str(scope) for scope in scopes_raw) if isinstance(scopes_raw, list) else ()
    return InstallationCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        key_version=int(row[3]),
        rotation_count=int(row[4]),
    )


def _cipher(encryption_key: str) -> Fernet:
    try:
        return Fernet(encryption_key.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("credential encryption key must be a valid Fernet key") from exc