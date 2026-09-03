from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..contexts import ActorAttribution, TenantContext


DEFAULT_TENANT_QUOTAS = {
    "ingestion": (10_000, 60),
    "api": (5_000, 60),
    "jobs": (10_000, 60),
    "exports": (100, 3600),
    "announcements": (1_000, 60),
    "moderation": (1_000, 60),
}


class TenantQuotaExceededError(RuntimeError):
    def __init__(self, quota_type: str, retry_after_seconds: int) -> None:
        self.quota_type = quota_type
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(f"tenant {quota_type} quota exceeded")


def configure_tenant_quota(
    connection: sqlite3.Connection, *, tenant: TenantContext,
    actor: ActorAttribution, quota_type: str, limit_count: int,
    window_seconds: int,
) -> None:
    if actor.actor_type != "operator" or actor.actor_id is None:
        raise ValueError("quota changes require an operator actor")
    normalized_type = _normalize_quota_type(quota_type)
    limit = int(limit_count)
    window = int(window_seconds)
    if limit < 1 or limit > 1_000_000:
        raise ValueError("quota limit must be between 1 and 1000000")
    if window < 1 or window > 86_400:
        raise ValueError("quota window must be between 1 and 86400 seconds")
    with connection:
        connection.execute(
            """INSERT INTO tenant_quota_policies(
                   community_id,quota_type,limit_count,window_seconds
               ) VALUES (?,?,?,?)
               ON CONFLICT(community_id,quota_type) DO UPDATE SET
                   limit_count=excluded.limit_count,
                   window_seconds=excluded.window_seconds,
                   updated_at=CURRENT_TIMESTAMP""",
            (tenant.community_id, normalized_type, limit, window),
        )
        connection.execute(
            """INSERT INTO audit_log(
                   actor_type,actor_id,action_type,entity_type,entity_id,payload_json
               ) VALUES ('operator',?,'tenant.quota_updated','community',?,?)""",
            (
                actor.actor_id,
                tenant.community_id,
                json.dumps({
                    "limit_count": limit,
                    "quota_type": normalized_type,
                    "window_seconds": window,
                }, sort_keys=True),
            ),
        )


def consume_tenant_quota(
    connection: sqlite3.Connection, *, tenant: TenantContext,
    quota_type: str, amount: int = 1, now: datetime | None = None,
) -> int:
    normalized_type = _normalize_quota_type(quota_type)
    increment = int(amount)
    if increment < 1:
        raise ValueError("quota amount must be positive")
    default_limit, default_window = DEFAULT_TENANT_QUOTAS[normalized_type]
    policy = connection.execute(
        """SELECT limit_count,window_seconds FROM tenant_quota_policies
           WHERE community_id=? AND quota_type=?""",
        (tenant.community_id, normalized_type),
    ).fetchone()
    limit = int(policy[0]) if policy is not None else default_limit
    window_seconds = int(policy[1]) if policy is not None else default_window
    current = now or datetime.now(timezone.utc)
    epoch = int(current.timestamp())
    window_epoch = epoch - (epoch % window_seconds)
    window_start = datetime.fromtimestamp(window_epoch, timezone.utc).isoformat()
    connection.execute(
        """INSERT INTO tenant_quota_usage(community_id,quota_type,window_start,usage_count)
           VALUES (?,?,?,?) ON CONFLICT(community_id,quota_type,window_start)
              DO UPDATE SET usage_count=tenant_quota_usage.usage_count+excluded.usage_count""",
        (tenant.community_id, normalized_type, window_start, increment),
    )
    usage = int(connection.execute(
        """SELECT usage_count FROM tenant_quota_usage
           WHERE community_id=? AND quota_type=? AND window_start=?""",
        (tenant.community_id, normalized_type, window_start),
    ).fetchone()[0])
    if usage > limit:
        connection.execute(
            """UPDATE tenant_quota_usage SET usage_count=usage_count-?
               WHERE community_id=? AND quota_type=? AND window_start=?""",
            (increment, tenant.community_id, normalized_type, window_start),
        )
        raise TenantQuotaExceededError(normalized_type, window_epoch + window_seconds - epoch)
    return limit - usage


def _normalize_quota_type(quota_type: str) -> str:
    normalized = quota_type.strip().casefold()
    if normalized not in DEFAULT_TENANT_QUOTAS:
        raise ValueError("unsupported tenant quota type")
    return normalized
