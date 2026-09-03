from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from .config import AppSettings
from .schema_scope import SCHEMA_SCOPE_INVENTORY


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
        "community_announcements", "community_announcement_deliveries",
        "installation_health_events",
        "community_onboarding_settings",
        "community_onboarding_resources",
        "moderation_shift_schedules",
        "community_onboarding_members",
    }
    existing = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    missing = sorted(required_tables - existing)
    checks.append(AuditCheck("schema", "pass" if not missing else "fail",
                             "professional schema present" if not missing else f"missing: {', '.join(missing)}"))
    checks.append(_scope_inventory_check(connection))
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


def _scope_inventory_check(connection: sqlite3.Connection) -> AuditCheck:
    existing = {
        str(row[0]) for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        ).fetchall()
    }
    unknown = sorted(existing - SCHEMA_SCOPE_INVENTORY.keys())
    missing = sorted(SCHEMA_SCOPE_INVENTORY.keys() - existing)
    structural_errors: list[str] = []
    for table in sorted(existing & SCHEMA_SCOPE_INVENTORY.keys()):
        rule = SCHEMA_SCOPE_INVENTORY[table]
        if rule.owner_column is None:
            continue
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if rule.owner_column not in columns:
            structural_errors.append(f"{table}.{rule.owner_column} missing")
            continue
        if rule.owner_table is None:
            continue
        foreign_keys = {
            (str(row[3]), str(row[2]))
            for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        if (rule.owner_column, rule.owner_table) not in foreign_keys:
            structural_errors.append(
                f"{table}.{rule.owner_column}->{rule.owner_table} missing"
            )
    errors = [
        *(f"unknown table: {table}" for table in unknown),
        *(f"missing table: {table}" for table in missing),
        *structural_errors,
    ]
    if errors:
        return AuditCheck("scope_inventory", "fail", "; ".join(errors))
    counts: dict[str, int] = {}
    for rule in SCHEMA_SCOPE_INVENTORY.values():
        counts[rule.scope] = counts.get(rule.scope, 0) + 1
    detail = ", ".join(f"{scope}={counts[scope]}" for scope in sorted(counts))
    return AuditCheck("scope_inventory", "pass", f"{len(existing)} tables classified ({detail})")
