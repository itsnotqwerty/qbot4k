from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from .config import AppSettings


@dataclass(frozen=True)
class AuditCheck:
    key: str
    status: str
    detail: str


def run_platform_audit(connection: sqlite3.Connection, settings: AppSettings) -> dict[str, Any]:
    checks: list[AuditCheck] = []
    required_tables = {
        "organizations", "communities", "community_installations",
        "twitch_eventsub_subscriptions", "raw_event_archive", "dead_letter_events",
        "community_intelligence_profiles", "coordination_campaigns", "legal_holds",
    }
    existing = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    missing = sorted(required_tables - existing)
    checks.append(AuditCheck("schema", "pass" if not missing else "fail",
                             "professional schema present" if not missing else f"missing: {', '.join(missing)}"))
    tenant_count = int(connection.execute("SELECT COUNT(*) FROM communities WHERE status='active'").fetchone()[0])
    checks.append(AuditCheck("tenancy", "pass", f"{tenant_count} active communities"))
    role_count = int(connection.execute("SELECT COUNT(*) FROM operator_community_roles").fetchone()[0])
    checks.append(AuditCheck("scoped_roles", "pass" if role_count else "warn",
                             f"{role_count} community role grants"))
    checks.append(AuditCheck(
        "eventsub", "pass" if settings.twitch_eventsub_secret and settings.twitch_eventsub_callback_url else "warn",
        "webhook signing configured" if settings.twitch_eventsub_secret and settings.twitch_eventsub_callback_url
        else "set QBOT_TWITCH_EVENTSUB_SECRET and QBOT_TWITCH_EVENTSUB_CALLBACK_URL",
    ))
    model_rows = connection.execute(
        "SELECT model_key,status FROM model_registry ORDER BY model_key"
    ).fetchall()
    unapproved = [str(row[0]) for row in model_rows if str(row[1]) != "active"]
    checks.append(AuditCheck("model_governance", "warn" if unapproved else "pass",
                             f"shadow/paused: {', '.join(unapproved)}" if unapproved else "all models active"))
    pending_archive = int(connection.execute(
        "SELECT COUNT(*) FROM raw_event_archive WHERE archive_path IS NULL"
    ).fetchone()[0])
    checks.append(AuditCheck("raw_archive", "pass" if pending_archive == 0 else "warn",
                             f"{pending_archive} events awaiting archive flush"))
    open_dead_letters = int(connection.execute(
        "SELECT COUNT(*) FROM dead_letter_events WHERE status='open'"
    ).fetchone()[0])
    checks.append(AuditCheck("dead_letters", "pass" if open_dead_letters == 0 else "warn",
                             f"{open_dead_letters} open dead letters"))
    overall = "fail" if any(item.status == "fail" for item in checks) else (
        "warn" if any(item.status == "warn" for item in checks) else "pass"
    )
    return {"status": overall, "checks": [asdict(item) for item in checks]}
