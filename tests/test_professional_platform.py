from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet

from src.config import AppSettings
from src.contexts import ActorAttribution, TenantContext
from src.credential_store import load_installation_credentials, store_installation_credentials
from src.db import (
    collect_observation,
    connect_database,
    initialize_database,
    persist_normalized_message,
	upsert_operator_account,
)
from src.intelligence.campaigns import analyze_coordination_campaign
from src.intelligence.community import (
    accept_operator_invitations,
    DASHBOARD_CAPABILITIES,
    ROLE_PERMISSIONS,
    consume_discord_install_intent,
    complete_twitch_install_intent,
    create_discord_install_intent,
    create_twitch_install_intent,
    create_community,
    create_organization,
    create_workspace,
	emergency_remove_operator_access,
    grant_operator_role,
    issue_pilot_invitation,
    installation_capabilities,
	invite_operator,
    operator_has_permission,
    platform_capabilities,
    revoke_operator_role,
	revoke_operator_invitation,
    set_operator_permission_override,
    update_installation_health,
	transfer_community_ownership,
    record_operator_discord_guild_permissions,
    register_installation,
    resolve_community_id,
	revoke_installation,
    twitch_install_intent_is_pending,
)
from src.intelligence.profiles import refresh_community_profile
from src.intelligence.governance import (
    authorize_api_client,
    complete_data_subject_request,
    configure_retention_policy,
    create_api_client,
    create_data_subject_request,
    create_legal_hold,
    fulfill_data_subject_request,
    offboard_community,
    release_legal_hold,
)
from src.intelligence.recovery import flush_raw_event_archive, record_dead_letter, replay_dead_letter
from src.models import NormalizedMessage, Observation
from src.pipeline.handlers import claim_processing_job, permanently_fail_processing_job
from src.platform_audit import run_platform_audit
from src.schema_scope import SCHEMA_SCOPE_INVENTORY
from src.twitch_eventsub import observation_from_eventsub, verify_eventsub_signature


class ProfessionalPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "platform.sqlite3"
        self.connection = connect_database(self.database_path)
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_tenant_installations_and_roles_are_community_scoped(self) -> None:
        organization = create_organization(self.connection, name="Agency", slug="agency")
        workspace = create_workspace(
            self.connection, organization_id=organization, name="Roster", slug="roster"
        )
        community = create_community(
            self.connection, workspace_id=workspace, name="Streamer A", slug="streamer-a"
        )
        register_installation(
            self.connection, community_id=community, platform="twitch",
            external_community_id="12345", display_name="Streamer A",
            scopes=("moderator:manage:banned_users",),
            metadata={"channel_login": "streamer_a"},
        )
        operator = self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('9','analyst','analyst')"
        ).lastrowid
        grant_operator_role(
            self.connection, operator_id=int(operator), community_id=community, role="analyst"
        )
        self.assertEqual(resolve_community_id(
            self.connection, platform="twitch", external_community_id="12345"
        ), community)
        metadata = self.connection.execute(
            """SELECT metadata_json FROM community_installations
               WHERE platform='twitch' AND external_community_id='12345'"""
        ).fetchone()[0]
        self.assertEqual(json.loads(str(metadata)), {"channel_login": "streamer_a"})
        installation_id = int(self.connection.execute(
            "SELECT id FROM community_installations WHERE platform='twitch' AND external_community_id='12345'"
        ).fetchone()[0])
        self.assertEqual(
            installation_capabilities(
                self.connection, community_id=community, installation_id=installation_id
            ),
            platform_capabilities("twitch"),
        )
        self.assertTrue(operator_has_permission(
            self.connection, operator_id=int(operator), community_id=community,
            permission="intelligence.read",
        ))
        self.assertFalse(operator_has_permission(
            self.connection, operator_id=int(operator), community_id=1,
            permission="intelligence.read",
        ))

    def test_operator_invitation_ownership_and_emergency_access_lifecycle(self) -> None:
        with self.connection:
            owner_id = int(self.connection.execute(
                "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('lifecycle-owner','owner','admin')"
            ).lastrowid)
            target_id = int(self.connection.execute(
                "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('lifecycle-target','target','moderator')"
            ).lastrowid)
        grant_operator_role(self.connection, operator_id=owner_id, community_id=1, role="owner")
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        invitation_id = invite_operator(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", owner_id),
            target_discord_user_id="lifecycle-target", role="admin", expires_at=expires_at,
        )
        self.assertEqual(accept_operator_invitations(
            self.connection, operator_id=target_id, discord_user_id="lifecycle-target"
        ), 1)
        self.assertEqual(self.connection.execute(
            "SELECT role FROM operator_community_roles WHERE operator_id=? AND community_id=1",
            (target_id,),
        ).fetchone()[0], "admin")
        revoked_id = invite_operator(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", owner_id),
            target_discord_user_id="revoked-target", role="viewer", expires_at=expires_at,
        )
        revoke_operator_invitation(
            self.connection, invitation_id=revoked_id, tenant=TenantContext(1),
            actor=ActorAttribution("operator", owner_id),
        )
        self.assertEqual(accept_operator_invitations(
            self.connection, operator_id=target_id, discord_user_id="revoked-target"
        ), 0)
        expired_id = invite_operator(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", owner_id),
            target_discord_user_id="expired-target", role="viewer", expires_at=expires_at,
        )
        with self.connection:
            self.connection.execute(
                "UPDATE operator_invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (expired_id,),
            )
        self.assertEqual(accept_operator_invitations(
            self.connection, operator_id=target_id, discord_user_id="expired-target"
        ), 0)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM operator_invitations WHERE id=?", (expired_id,)
        ).fetchone()[0], "expired")
        transfer_community_ownership(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", owner_id),
            new_owner_id=target_id,
        )
        emergency_remove_operator_access(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", target_id),
            operator_id=owner_id, reason="Credential compromise",
        )
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM operator_community_roles WHERE operator_id=? AND community_id=1",
            (owner_id,),
        ).fetchone())
        self.assertEqual(upsert_operator_account(
            self.connection, discord_user_id="lifecycle-owner",
            discord_username="owner-returned", role="admin",
        ), owner_id)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM operator_community_roles WHERE operator_id=? AND community_id=1",
            (owner_id,),
        ).fetchone())
        self.assertEqual(self.connection.execute(
            "SELECT status FROM operator_accounts WHERE id=?", (owner_id,)
        ).fetchone()[0], "disabled")
        restoration_invite = invite_operator(
            self.connection, tenant=TenantContext(1), actor=ActorAttribution("operator", target_id),
            target_discord_user_id="lifecycle-owner", role="admin", expires_at=expires_at,
        )
        self.assertGreater(restoration_invite, 0)
        self.assertEqual(accept_operator_invitations(
            self.connection, operator_id=owner_id, discord_user_id="lifecycle-owner"
        ), 1)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM operator_accounts WHERE id=?", (owner_id,)
        ).fetchone()[0], "active")
        self.assertGreater(int(self.connection.execute(
            "SELECT session_version FROM operator_accounts WHERE id=?", (owner_id,)
        ).fetchone()[0]), 1)
        actions = {row[0] for row in self.connection.execute(
            """SELECT action_type FROM audit_log WHERE action_type IN (
                   'operator.invitation_created','operator.invitation_accepted',
                   'operator.invitation_revoked','operator.ownership_transferred',
                   'operator.access_emergency_removed')"""
        ).fetchall()}
        self.assertEqual(actions, {
            "operator.invitation_created", "operator.invitation_accepted",
            "operator.invitation_revoked", "operator.ownership_transferred",
            "operator.access_emergency_removed",
        })

    def test_platform_adapters_advertise_supported_capabilities(self) -> None:
        from src.discord import CAPABILITIES as discord_capabilities
        from src.twitch import CAPABILITIES as twitch_capabilities

        self.assertEqual(discord_capabilities, platform_capabilities("discord"))
        self.assertEqual(twitch_capabilities, platform_capabilities("twitch"))
        self.assertNotIn("live_controls", discord_capabilities)
        self.assertIn("live_controls", twitch_capabilities)

    def test_installation_revocation_clears_credentials_and_is_tenant_scoped(self) -> None:
        with self.connection:
            operator_id = int(self.connection.execute(
                "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('revoke-actor','actor','admin')"
            ).lastrowid)
        grant_operator_role(self.connection, operator_id=operator_id, community_id=1, role="owner")
        installation_id = register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="revocation-guild", display_name="Revocation Guild",
        )
        with self.connection:
            self.connection.execute(
                "UPDATE community_installations SET token_reference='secret-ref' WHERE id=?",
                (installation_id,),
            )
        revoke_installation(
            self.connection, community_id=1, installation_id=installation_id,
            actor_operator_id=operator_id,
        )
        row = self.connection.execute(
            "SELECT status,token_reference FROM community_installations WHERE id=?",
            (installation_id,),
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("revoked", None))
        with self.assertRaises(LookupError):
            revoke_installation(
                self.connection, community_id=999, installation_id=installation_id,
                actor_operator_id=operator_id,
            )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='integration.revoked' AND actor_id=?",
            (operator_id,),
        ).fetchone()[0], 1)

    def test_dashboard_capability_catalog_covers_role_permissions(self) -> None:
        for role, permissions in ROLE_PERMISSIONS.items():
            with self.subTest(role=role):
                self.assertTrue("*" in permissions or permissions <= DASHBOARD_CAPABILITIES)
                self.assertIn("dashboard.access", permissions if "*" not in permissions else DASHBOARD_CAPABILITIES)
        self.assertTrue({
            "members.read", "moderation.queues.read", "moderation.manage", "moderation.bulk",
            "rules.manage", "appeals.manage", "evidence.sensitive.read", "cases.manage",
            "analytics.export", "exports.create", "announcements.manage", "integrations.manage",
            "settings.manage", "operators.manage", "audit.read",
        } <= DASHBOARD_CAPABILITIES)

    def test_every_role_and_permission_override_is_table_driven(self) -> None:
        with self.connection:
            actor_id = int(self.connection.execute(
                "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('matrix-actor','actor','admin')"
            ).lastrowid)
            operator_id = int(self.connection.execute(
                "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('matrix-target','target','viewer')"
            ).lastrowid)
        grant_operator_role(
            self.connection, operator_id=actor_id, community_id=1, role="owner"
        )
        for role, role_permissions in ROLE_PERMISSIONS.items():
            grant_operator_role(
                self.connection, operator_id=operator_id, community_id=1, role=role
            )
            for permission in DASHBOARD_CAPABILITIES:
                with self.subTest(role=role, permission=permission):
                    self.assertEqual(
                        operator_has_permission(
                            self.connection, operator_id=operator_id, community_id=1,
                            permission=permission,
                        ),
                        "*" in role_permissions or permission in role_permissions,
                    )

        grant_operator_role(
            self.connection, operator_id=operator_id, community_id=1, role="viewer"
        )
        for decision, expected in (("grant", True), ("deny", False), ("clear", False)):
            set_operator_permission_override(
                self.connection, operator_id=operator_id, community_id=1,
                permission="operators.manage", decision=decision,
                actor_operator_id=actor_id,
            )
            with self.subTest(decision=decision):
                self.assertEqual(
                    operator_has_permission(
                        self.connection, operator_id=operator_id, community_id=1,
                        permission="operators.manage",
                    ),
                    expected,
                )
        with self.assertRaisesRegex(ValueError, "unsupported dashboard permission"):
            set_operator_permission_override(
                self.connection, operator_id=operator_id, community_id=1,
                permission="unknown.permission", decision="grant",
                actor_operator_id=actor_id,
            )

    def test_installation_capabilities_health_and_reconnect_history(self) -> None:
        installation_id = register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="health-guild", display_name="Health Guild",
            status="pending", capabilities=("events", "member_lifecycle", "announcements"),
        )
        self.assertEqual(
            installation_capabilities(
                self.connection, community_id=1, installation_id=installation_id
            ),
            frozenset({"events", "member_lifecycle", "announcements"}),
        )
        update_installation_health(
            self.connection, community_id=1, installation_id=installation_id,
            health_status="degraded", checked_at="2026-08-26T12:00:00+00:00",
            error="gateway unavailable", reconnect_attempted=True,
        )
        with self.assertRaises(LookupError):
            resolve_community_id(
                self.connection, platform="discord", external_community_id="health-guild"
            )
        update_installation_health(
            self.connection, community_id=1, installation_id=installation_id,
            health_status="healthy", checked_at="2026-08-26T12:05:00+00:00",
        )
        row = self.connection.execute(
            """SELECT status,health_status,reconnect_attempts,last_error,last_verified_at
               FROM community_installations WHERE id=?""",
            (installation_id,),
        ).fetchone()
        events = self.connection.execute(
            """SELECT health_status,lifecycle_status,reconnect_attempted,error_message
               FROM installation_health_events WHERE installation_id=? ORDER BY id""",
            (installation_id,),
        ).fetchall()
        self.assertEqual(resolve_community_id(
            self.connection, platform="discord", external_community_id="health-guild"
        ), 1)
        self.assertEqual(tuple(row[:4]), ("active", "healthy", 1, None))
        self.assertEqual(row[4], "2026-08-26T12:05:00+00:00")
        self.assertEqual(
            [tuple(event) for event in events],
            [
                ("degraded", "degraded", 1, "gateway unavailable"),
                ("healthy", "active", 0, None),
            ],
        )
        with self.assertRaises(LookupError):
            update_installation_health(
                self.connection, community_id=2, installation_id=installation_id,
                health_status="healthy", checked_at="2026-08-26T12:06:00+00:00",
            )

    def test_permission_overrides_deny_wins_and_last_owner_is_preserved(self) -> None:
        with self.connection:
            owner_id = int(self.connection.execute(
                """INSERT INTO operator_accounts(discord_user_id,discord_username,role)
                   VALUES ('override-owner','owner','admin')"""
            ).lastrowid)
            analyst_id = int(self.connection.execute(
                """INSERT INTO operator_accounts(discord_user_id,discord_username,role)
                   VALUES ('override-analyst','analyst','analyst')"""
            ).lastrowid)
        grant_operator_role(self.connection, operator_id=owner_id, community_id=1, role="owner")
        grant_operator_role(self.connection, operator_id=analyst_id, community_id=1, role="analyst")
        set_operator_permission_override(
            self.connection, operator_id=analyst_id, community_id=1,
            permission="moderation.manage", decision="grant", actor_operator_id=owner_id,
        )
        set_operator_permission_override(
            self.connection, operator_id=owner_id, community_id=1,
            permission="integrations.manage", decision="deny", actor_operator_id=owner_id,
        )
        self.assertTrue(operator_has_permission(
            self.connection, operator_id=analyst_id, community_id=1,
            permission="moderation.manage",
        ))
        self.assertFalse(operator_has_permission(
            self.connection, operator_id=owner_id, community_id=1,
            permission="integrations.manage",
        ))
        with self.assertRaisesRegex(ValueError, "last community owner"):
            revoke_operator_role(self.connection, operator_id=owner_id, community_id=1)
        with self.assertRaisesRegex(ValueError, "last community owner"):
            grant_operator_role(
                self.connection, operator_id=owner_id, community_id=1, role="admin"
            )
        set_operator_permission_override(
            self.connection, operator_id=owner_id, community_id=1,
            permission="integrations.manage", decision="clear", actor_operator_id=owner_id,
        )
        self.assertTrue(operator_has_permission(
            self.connection, operator_id=owner_id, community_id=1,
            permission="integrations.manage",
        ))
        self.assertTrue(revoke_operator_role(
            self.connection, operator_id=analyst_id, community_id=1,
            actor_operator_id=owner_id,
        ))
        audit_count = int(self.connection.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE action_type='operator.permission_override' AND entity_id=1"""
        ).fetchone()[0])
        self.assertEqual(audit_count, 3)
        role_audits = self.connection.execute(
            """SELECT actor_type,actor_id,action_type,payload_json FROM audit_log
               WHERE action_type IN ('operator.role_granted','operator.role_revoked')
                 AND entity_id=1 ORDER BY id"""
        ).fetchall()
        self.assertEqual([row[2] for row in role_audits], [
            "operator.role_granted", "operator.role_granted", "operator.role_revoked",
        ])
        self.assertEqual((role_audits[-1][0], int(role_audits[-1][1])), ("operator", owner_id))
        self.assertEqual(json.loads(role_audits[-1][3]), {
            "operator_id": analyst_id, "role": "analyst",
        })

    def test_legacy_announcement_schema_migrates_before_dedupe_index(self) -> None:
        legacy_path = self.root / "legacy-announcements.sqlite3"
        connection = connect_database(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE community_announcements (
                       id INTEGER PRIMARY KEY,
                       community_id INTEGER NOT NULL,
                       platform TEXT NOT NULL,
                       target_external_id TEXT NOT NULL,
                       body TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'draft',
                       scheduled_at TEXT,
                       timezone TEXT NOT NULL DEFAULT 'UTC',
                       created_by_operator_id INTEGER,
                       approved_by_operator_id INTEGER,
                       approved_at TEXT,
                       delivered_at TEXT,
                       last_error TEXT,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            initialize_database(connection, force=True)
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(community_announcements)"
                ).fetchall()
            }
            indexes = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA index_list(community_announcements)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue({"target_installation_id", "dedupe_key", "source_json"} <= columns)
        self.assertIn("idx_community_announcement_dedupe", indexes)

    def test_legacy_installation_schema_adds_lifecycle_metadata(self) -> None:
        legacy_path = self.root / "legacy-installations.sqlite3"
        connection = connect_database(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE community_installations (
                       id INTEGER PRIMARY KEY,
                       community_id INTEGER NOT NULL,
                       platform TEXT NOT NULL,
                       external_community_id TEXT NOT NULL,
                       display_name TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'pending',
                       scopes_json TEXT NOT NULL DEFAULT '[]',
                       metadata_json TEXT NOT NULL DEFAULT '{}',
                       token_reference TEXT,
                       last_verified_at TEXT,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       UNIQUE(platform, external_community_id)
                   )"""
            )
            initialize_database(connection, force=True)
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(community_installations)"
                ).fetchall()
            }
            health_table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='installation_health_events'"""
            ).fetchone()
        finally:
            connection.close()
        self.assertTrue({
            "capabilities_json", "health_status", "last_health_check_at",
            "reconnect_attempts", "last_error",
        } <= columns)
        self.assertIsNotNone(health_table)

    def test_checkpoint_onboarding_schema_adds_verification_resources(self) -> None:
        legacy_path = self.root / "legacy-onboarding.sqlite3"
        connection = connect_database(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE community_onboarding_settings (
                       community_id INTEGER PRIMARY KEY,
                       discord_installation_id INTEGER,
                       welcome_channel_id TEXT,
                       welcome_template TEXT NOT NULL DEFAULT 'Welcome {mention}!',
                       welcome_enabled INTEGER NOT NULL DEFAULT 0,
                       newcomer_role_id TEXT,
                       newcomer_role_enabled INTEGER NOT NULL DEFAULT 0,
                       checkpoint_due_hours INTEGER NOT NULL DEFAULT 24,
                       checkpoint_reminder_enabled INTEGER NOT NULL DEFAULT 0,
                       checkpoint_reminder_template TEXT NOT NULL DEFAULT 'Reminder {mention}',
                       updated_by_operator_id INTEGER,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            connection.execute(
                """CREATE TABLE community_onboarding_members (
                       community_id INTEGER NOT NULL,
                       discord_installation_id INTEGER NOT NULL,
                       platform_user_id TEXT NOT NULL,
                       username TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'newcomer',
                       newcomer_role_id TEXT,
                       role_assignment_status TEXT NOT NULL DEFAULT 'disabled',
                       role_assignment_attempts INTEGER NOT NULL DEFAULT 0,
                       role_assignment_error TEXT,
                       joined_at TEXT NOT NULL,
                       checkpoint_due_at TEXT,
                       reminder_sent_at TEXT,
                       verified_at TEXT,
                       verified_by_operator_id INTEGER,
                       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       PRIMARY KEY(community_id, platform_user_id)
                   )"""
            )
            initialize_database(connection, force=True)
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(community_onboarding_settings)"
                ).fetchall()
            }
            member_columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(community_onboarding_members)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue({
            "verification_resource_enabled",
            "verification_resource_url",
            "verification_resource_template",
            "verification_evidence_required",
        } <= columns)
        self.assertIn("verification_evidence", member_columns)

    def test_installation_resolution_rejects_unknown_and_inactive_installations(self) -> None:
        with self.assertRaisesRegex(LookupError, "active installation not found"):
            resolve_community_id(
                self.connection, platform="twitch", external_community_id="unknown"
            )

        register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="guild-1", display_name="Inactive Guild",
            status="revoked",
        )
        with self.assertRaisesRegex(LookupError, "active installation not found"):
            resolve_community_id(
                self.connection, platform="discord", external_community_id="guild-1"
            )

    def test_discord_install_intents_require_authority_and_are_single_use(self) -> None:
        operator_id = int(self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) "
            "VALUES ('19','owner','admin')"
        ).lastrowid)
        grant_operator_role(
            self.connection, operator_id=operator_id, community_id=1, role="owner"
        )
        record_operator_discord_guild_permissions(
            self.connection, operator_id=operator_id,
            permissions={"guild-1": "32", "guild-2": "32", "guild-3": "32"},
        )
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        invite_code = issue_pilot_invitation(
            self.connection, community_id=1, expires_at=expires_at,
            created_by_operator_id=operator_id,
        )

        create_discord_install_intent(
            self.connection, nonce="intent-1", operator_id=operator_id,
            community_id=1, guild_id="guild-1", expires_at=expires_at,
            pilot_invite_code=invite_code,
        )

        self.assertTrue(consume_discord_install_intent(
            self.connection, nonce="intent-1", operator_id=operator_id,
            community_id=1, guild_id="guild-1",
        ))
        self.assertFalse(consume_discord_install_intent(
            self.connection, nonce="intent-1", operator_id=operator_id,
            community_id=1, guild_id="guild-1",
        ))
        with self.assertRaises(PermissionError):
            create_discord_install_intent(
                self.connection, nonce="intent-2", operator_id=operator_id,
                community_id=1, guild_id="unmanaged-guild", expires_at=expires_at,
                pilot_invite_code=invite_code,
            )

        organization_id = create_organization(
            self.connection, name="Other Agency", slug="other-agency"
        )
        workspace_id = create_workspace(
            self.connection, organization_id=organization_id,
            name="Other Roster", slug="other-roster",
        )
        other_community_id = create_community(
            self.connection, workspace_id=workspace_id,
            name="Other Community", slug="other-community",
        )
        other_invite_code = issue_pilot_invitation(
            self.connection, community_id=other_community_id, expires_at=expires_at,
            created_by_operator_id=operator_id,
        )
        with self.assertRaisesRegex(PermissionError, "pilot invitation"):
            create_discord_install_intent(
                self.connection, nonce="wrong-community", operator_id=operator_id,
                community_id=1, guild_id="guild-3", expires_at=expires_at,
                pilot_invite_code=other_invite_code,
            )
        expired_invite_code = issue_pilot_invitation(
            self.connection, community_id=1, expires_at=expires_at,
            created_by_operator_id=operator_id,
        )
        with self.connection:
            self.connection.execute(
                "UPDATE pilot_invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE code_hash=?",
                (hashlib.sha256(expired_invite_code.encode("utf-8")).hexdigest(),),
            )
        with self.assertRaisesRegex(PermissionError, "pilot invitation"):
            create_discord_install_intent(
                self.connection, nonce="expired-invite", operator_id=operator_id,
                community_id=1, guild_id="guild-3", expires_at=expires_at,
                pilot_invite_code=expired_invite_code,
            )
        register_installation(
            self.connection, community_id=other_community_id, platform="discord",
            external_community_id="guild-2", display_name="Other Guild",
        )
        with self.assertRaisesRegex(PermissionError, "already linked"):
            create_discord_install_intent(
                self.connection, nonce="intent-3", operator_id=operator_id,
                community_id=1, guild_id="guild-2", expires_at=expires_at,
                pilot_invite_code=invite_code,
            )
        with self.assertRaisesRegex(PermissionError, "pilot invitation"):
            create_discord_install_intent(
                self.connection, nonce="intent-4", operator_id=operator_id,
                community_id=1, guild_id="guild-3", expires_at=expires_at,
                pilot_invite_code=invite_code,
            )

    def test_twitch_install_intent_encrypts_credentials_and_is_single_use(self) -> None:
        operator_id = int(self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) "
            "VALUES ('twitch-owner','owner','admin')"
        ).lastrowid)
        grant_operator_role(
            self.connection, operator_id=operator_id, community_id=1, role="owner"
        )
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        scopes = ("moderator:read:followers", "channel:read:subscriptions")
        create_twitch_install_intent(
            self.connection, nonce="twitch-intent", operator_id=operator_id,
            community_id=1, broadcaster_login="Streamer_A", scopes=scopes,
            expires_at=expires_at,
        )
        self.assertTrue(twitch_install_intent_is_pending(
            self.connection, nonce="twitch-intent", operator_id=operator_id,
            community_id=1, broadcaster_login="streamer_a",
        ))

        encryption_key = Fernet.generate_key().decode("ascii")
        installation_id = complete_twitch_install_intent(
            self.connection, nonce="twitch-intent", operator_id=operator_id,
            community_id=1, broadcaster_login="streamer_a",
            broadcaster_id="12345",
            access_token="access-secret", refresh_token="refresh-secret",
            scopes=scopes, encryption_key=encryption_key,
        )
        installation = self.connection.execute(
            """SELECT status,health_status,metadata_json,token_reference
               FROM community_installations WHERE id=?""",
            (installation_id,),
        ).fetchone()
        self.assertEqual((installation[0], installation[1]), ("pending", "unknown"))
        self.assertEqual(json.loads(str(installation[2]))["moderation_mode"], "shadow")
        self.assertTrue(str(installation[3]).startswith("installation-credential:"))
        credentials = load_installation_credentials(
            self.connection, community_id=1, installation_id=installation_id,
            encryption_key=encryption_key,
        )
        self.assertEqual(
            (credentials.access_token, credentials.refresh_token),
            ("access-secret", "refresh-secret"),
        )
        ciphertext = bytes(self.connection.execute(
            "SELECT access_token_ciphertext FROM installation_credentials WHERE installation_id=?",
            (installation_id,),
        ).fetchone()[0])
        self.assertNotIn(b"access-secret", ciphertext)
        store_installation_credentials(
            self.connection, community_id=1, installation_id=installation_id,
            access_token="rotated-access", refresh_token="rotated-refresh",
            scopes=scopes, encryption_key=encryption_key, actor_operator_id=operator_id,
        )
        rotated = load_installation_credentials(
            self.connection, community_id=1, installation_id=installation_id,
            encryption_key=encryption_key,
        )
        self.assertEqual((rotated.access_token, rotated.rotation_count), ("rotated-access", 2))
        with self.assertRaisesRegex(PermissionError, "already consumed"):
            complete_twitch_install_intent(
                self.connection, nonce="twitch-intent", operator_id=operator_id,
                community_id=1, broadcaster_login="streamer_a",
                broadcaster_id="12345",
                access_token="again", refresh_token=None, scopes=scopes,
                encryption_key=encryption_key,
            )

        revoke_installation(
            self.connection, community_id=1, installation_id=installation_id,
            actor_operator_id=operator_id,
        )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM installation_credentials WHERE installation_id=?",
            (installation_id,),
        ).fetchone()[0], 0)

    def test_twitch_install_intent_rejects_unreviewed_scope(self) -> None:
        operator_id = int(self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) "
            "VALUES ('scope-owner','owner','admin')"
        ).lastrowid)
        grant_operator_role(
            self.connection, operator_id=operator_id, community_id=1, role="owner"
        )
        with self.assertRaisesRegex(ValueError, "unsupported scopes"):
            create_twitch_install_intent(
                self.connection, nonce="bad-scope", operator_id=operator_id,
                community_id=1, broadcaster_login="streamer",
                scopes=("user:edit",),
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            )

    def test_eventsub_signature_and_normalization(self) -> None:
        secret = "a-professional-secret"
        message_id = "event-1"
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "subscription": {"id": "sub-1", "type": "channel.chat.message"},
            "event": {
                "broadcaster_user_id": "12345", "chatter_user_id": "42",
                "chatter_user_name": "viewer", "message_id": "message-1",
                "message": {"text": "hello community"},
            },
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(
            secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(verify_eventsub_signature(
            secret, message_id=message_id, timestamp=timestamp, body=body, signature=signature
        ))
        observation = observation_from_eventsub(payload, message_id=message_id, community_id=7)
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.event_type, "message.created")
        self.assertEqual(observation.community_id, 7)
        self.assertEqual(observation.actor_platform_user_id, "42")

    def test_raw_archive_dead_letter_and_replay(self) -> None:
        result = collect_observation(self.connection, Observation(
            platform="twitch", event_type="stream.started", external_event_id="stream-1",
            community_id=1,
            actor_platform_user_id="broadcaster", actor_username="broadcaster",
            container_id="channel", context_id="channel", text="Live",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            raw_payload={"stream_id": "stream-1"},
        ))
        self.assertEqual(flush_raw_event_archive(
            self.connection, self.root / "raw-events"
        ), 1)
        archive_path = self.connection.execute(
            "SELECT archive_path FROM raw_event_archive WHERE observation_id=?",
            (result.observation_id,),
        ).fetchone()[0]
        self.assertTrue(Path(str(archive_path)).is_file())
        job = claim_processing_job(self.connection, stage="analysis", worker_id="test")
        assert job is not None
        permanently_fail_processing_job(self.connection, int(job["id"]), "fixture failure")
        dead_letter = record_dead_letter(
            self.connection, job_id=int(job["id"]), error=RuntimeError("fixture failure")
        )
        replay_job = replay_dead_letter(self.connection, dead_letter)
        self.assertGreater(replay_job, int(job["id"]))

    def test_profiles_and_campaigns_are_multi_axis_and_community_scoped(self) -> None:
        observation_ids: list[int] = []
        user_ids: list[int] = []
        for index, actor in enumerate(("actor-a", "actor-b", "actor-a"), start=1):
            message = NormalizedMessage(
                platform="twitch", platform_user_id=actor, username=actor,
                channel_id="12345", content_raw="Join the giveaway at https://bad.example/deal now",
                sent_at=datetime.now(timezone.utc).isoformat(), platform_message_id=f"msg-{index}",
                guild_or_channel_context="12345", metadata={"community_id": 1},
            )
            collected = collect_observation(self.connection, Observation(
                platform="twitch", event_type="message.created", external_event_id=f"msg-{index}",
                actor_platform_user_id=actor, actor_username=actor, container_id="12345",
                context_id="12345", text=message.content_raw, occurred_at=message.sent_at,
                attributes={"community_id": 1}, community_id=1,
            ))
            persisted = persist_normalized_message(
                self.connection, message, observation_id=collected.observation_id,
                moderation_shadow_mode=True,
            )
            observation_ids.append(int(collected.observation_id))
            user_id = self.connection.execute(
                "SELECT user_id FROM messages WHERE id=?", (persisted.message_id,)
            ).fetchone()[0]
            user_ids.append(int(user_id))
        campaign_id = analyze_coordination_campaign(self.connection, observation_ids[-1])
        self.assertIsNotNone(campaign_id)
        profile = refresh_community_profile(
            self.connection, community_id=1, user_id=user_ids[0]
        )
        self.assertGreaterEqual(profile.engagement, 0)
        self.assertGreaterEqual(profile.risk, 0)
        self.assertNotEqual(profile.trust, profile.engagement)

    def test_platform_audit_reports_no_schema_failure(self) -> None:
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_RAW_ARCHIVE_DIR": str(self.root / "raw"),
            "QBOT_ENABLED_SERVICES": "jobs,analysis",
        })
        result = run_platform_audit(self.connection, settings)

        scope_check = next(item for item in result["checks"] if item["key"] == "scope_inventory")
        self.assertEqual(scope_check["status"], "pass")
        existing = {
            str(row[0]) for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        self.assertEqual(existing, SCHEMA_SCOPE_INVENTORY.keys())
        self.assertNotEqual(result["status"], "fail")

    def test_platform_audit_rejects_unclassified_schema_surface(self) -> None:
        with self.connection:
            self.connection.execute("CREATE TABLE unclassified_tenant_data(id INTEGER PRIMARY KEY)")
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_RAW_ARCHIVE_DIR": str(self.root / "raw"),
            "QBOT_ENABLED_SERVICES": "jobs,analysis",
        })

        result = run_platform_audit(self.connection, settings)

        scope_check = next(item for item in result["checks"] if item["key"] == "scope_inventory")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(scope_check["status"], "fail")
        self.assertIn("unknown table: unclassified_tenant_data", scope_check["detail"])

    def test_governance_keys_are_hashed_and_legal_holds_are_audited(self) -> None:
        client_id, plaintext = create_api_client(
            self.connection, organization_id=1, community_id=1, name="automation",
            scopes=("events.write",), rate_limit_per_minute=2,
        )
        stored = str(self.connection.execute(
            "SELECT key_hash FROM api_clients WHERE id=?", (client_id,)
        ).fetchone()[0])
        self.assertNotIn(plaintext, stored)
        self.assertEqual(authorize_api_client(
            self.connection, plaintext_key=plaintext, required_scope="events.write"
        ), client_id)
        self.assertIsNone(authorize_api_client(
            self.connection, plaintext_key=plaintext, required_scope="admin"
        ))
        tenant = TenantContext(1)
        actor = ActorAttribution("operator", 1)
        hold_id = create_legal_hold(
            self.connection, tenant=tenant, actor=actor, reason="active investigation"
        )
        with self.assertRaises(ValueError):
            release_legal_hold(
                self.connection, tenant=TenantContext(2), actor=actor, hold_id=hold_id,
            )
        release_legal_hold(
            self.connection, tenant=tenant, actor=actor, hold_id=hold_id,
        )
        actions = [str(row[0]) for row in self.connection.execute(
            "SELECT action_type FROM audit_log WHERE entity_type='legal_hold' ORDER BY id"
        ).fetchall()]
        self.assertEqual(actions, ["legal_hold.created", "legal_hold.released"])

    def test_retention_policy_administration_is_tenant_scoped_and_audited(self) -> None:
        tenant = TenantContext(1)
        actor = ActorAttribution("operator", 1)

        configure_retention_policy(
            self.connection, tenant=tenant, actor=actor,
            message_retention_days=30, analytics_retention_days=180,
        )

        policy = self.connection.execute(
            """SELECT message_retention_days,analytics_retention_days,
                      updated_by_operator_id
               FROM community_policy_settings WHERE community_id=1"""
        ).fetchone()
        audit = self.connection.execute(
            """SELECT actor_id,payload_json FROM audit_log
               WHERE action_type='retention.policy_updated' AND entity_id=1"""
        ).fetchone()
        self.assertEqual(tuple(policy), (30, 180, 1))
        self.assertEqual(int(audit[0]), 1)
        self.assertEqual(json.loads(str(audit[1]))["message_retention_days"], 30)
        with self.assertRaises(ValueError):
            configure_retention_policy(
                self.connection, tenant=tenant, actor=actor,
                message_retention_days=0, analytics_retention_days=180,
            )

    def test_data_subject_request_lifecycle_is_tenant_scoped_and_audited(self) -> None:
        operator_id = upsert_operator_account(
            self.connection, discord_user_id="privacy-operator",
            discord_username="Privacy Operator", role="admin",
        )
        actor = ActorAttribution("operator", operator_id)
        other_community_id = create_community(
            self.connection, workspace_id=1,
            name="Other Privacy Tenant", slug="other-privacy-tenant",
        )
        request_id = create_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor,
            request_type="export", platform="discord", platform_user_id="subject-1",
        )

        with self.assertRaises(ValueError):
            complete_data_subject_request(
                self.connection, tenant=TenantContext(other_community_id), actor=actor,
                request_id=request_id, result={"export_reference": "wrong-tenant"},
            )
        complete_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor,
            request_id=request_id, result={"export_reference": "export-1"},
        )

        request = self.connection.execute(
            """SELECT community_id,status,requested_by_operator_id,
                      completed_by_operator_id,result_json
               FROM data_subject_requests WHERE id=?""",
            (request_id,),
        ).fetchone()
        actions = [str(row[0]) for row in self.connection.execute(
            """SELECT action_type FROM audit_log
               WHERE entity_type='data_subject_request' AND entity_id=? ORDER BY id""",
            (request_id,),
        ).fetchall()]
        self.assertEqual(tuple(request[:4]), (1, "completed", operator_id, operator_id))
        self.assertEqual(json.loads(str(request[4]))["export_reference"], "export-1")
        self.assertEqual(actions, [
            "data_subject_request.created", "data_subject_request.completed",
        ])

    def test_subject_export_and_deletion_respect_tenant_and_legal_hold(self) -> None:
        operator_id = upsert_operator_account(
            self.connection, discord_user_id="subject-operator",
            discord_username="Subject Operator", role="admin",
        )
        actor = ActorAttribution("operator", operator_id)
        other_community_id = create_community(
            self.connection, workspace_id=1,
            name="Other Subject Tenant", slug="other-subject-tenant",
        )
        with self.connection:
            account_id = int(self.connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username)
                   VALUES ('discord','subject-shared','Shared Subject') RETURNING id"""
            ).fetchone()[0])
            self.connection.executemany(
                """INSERT INTO community_memberships(community_id,platform_account_id)
                   VALUES (?,?)""",
                ((1, account_id), (other_community_id, account_id)),
            )
            self.connection.executemany(
                """INSERT INTO messages(
                       community_id,platform,platform_message_id,platform_account_id,
                       channel_id,content_raw,content_normalized,sent_at
                   ) VALUES (?,'discord',?,?, 'channel',?,?, '2026-09-02T00:00:00+00:00')""",
                (
                    (1, "subject-message-1", account_id, "tenant one private", "tenant one private"),
                    (other_community_id, "subject-message-2", account_id,
                     "tenant two private", "tenant two private"),
                ),
            )
        export_id = create_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor,
            request_type="export", platform="discord", platform_user_id="subject-shared",
        )
        exported = fulfill_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor, request_id=export_id,
        )
        self.assertEqual([row["content_raw"] for row in exported["messages"]], [
            "tenant one private",
        ])
        hold_id = create_legal_hold(
            self.connection, tenant=TenantContext(1), actor=actor,
            reason="preserve subject evidence",
        )
        deletion_id = create_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor,
            request_type="delete", platform="discord", platform_user_id="subject-shared",
        )
        with self.assertRaises(ValueError):
            fulfill_data_subject_request(
                self.connection, tenant=TenantContext(1), actor=actor,
                request_id=deletion_id,
            )
        release_legal_hold(
            self.connection, tenant=TenantContext(1), actor=actor, hold_id=hold_id,
        )
        result = fulfill_data_subject_request(
            self.connection, tenant=TenantContext(1), actor=actor,
            request_id=deletion_id,
        )
        contents = [str(row[0]) for row in self.connection.execute(
            "SELECT content_raw FROM messages WHERE platform_account_id=? ORDER BY community_id",
            (account_id,),
        ).fetchall()]
        memberships = [int(row[0]) for row in self.connection.execute(
            """SELECT community_id FROM community_memberships
               WHERE platform_account_id=? ORDER BY community_id""",
            (account_id,),
        ).fetchall()]
        self.assertEqual(result["redacted_messages"], 1)
        self.assertEqual(contents, ["[deleted by privacy request]", "tenant two private"])
        self.assertEqual(memberships, [other_community_id])

    def test_community_offboarding_revokes_credentials_without_touching_peer(self) -> None:
        operator_id = upsert_operator_account(
            self.connection, discord_user_id="offboarding-operator",
            discord_username="Offboarding Operator", role="admin",
        )
        peer_community_id = create_community(
            self.connection, workspace_id=1, name="Offboarding Peer", slug="offboarding-peer",
        )
        installation_id = register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="offboarding-guild", display_name="Offboarding Guild",
        )
        with self.connection:
            self.connection.execute(
                """INSERT INTO installation_credentials(
                       installation_id,access_token_ciphertext,scopes_json
                   ) VALUES (?,X'0102','[]')""",
                (installation_id,),
            )

        revoked = offboard_community(
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", operator_id),
            export_reference="backup:verified:20260902",
        )

        statuses = [tuple(row) for row in self.connection.execute(
            "SELECT id,status FROM communities WHERE id IN (1,?) ORDER BY id",
            (peer_community_id,),
        ).fetchall()]
        installation = self.connection.execute(
            """SELECT status,token_reference FROM community_installations
               WHERE id=?""", (installation_id,),
        ).fetchone()
        credential_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM installation_credentials WHERE installation_id=?",
            (installation_id,),
        ).fetchone()[0])
        self.assertEqual(revoked, 1)
        self.assertEqual(statuses, [(1, "offboarded"), (peer_community_id, "active")])
        self.assertEqual(tuple(installation), ("revoked", None))
        self.assertEqual(credential_count, 0)


if __name__ == "__main__":
    unittest.main()
