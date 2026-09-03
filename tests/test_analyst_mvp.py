from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.contexts import ActorAttribution, TenantContext
from src.dashboard.moderation import (
    add_moderation_rule_exemption,
    create_moderation_rule_draft,
    preview_moderation_rule_version,
    publish_moderation_rule_version,
    resolve_review,
    rollback_moderation_rule,
    save_moderation_rule,
)
from src.db import (
    connect_database,
    database_health,
    initialize_database,
    operational_readiness_snapshot,
    record_operational_metric,
    load_enabled_moderation_rules,
)
from src.intelligence.community import create_community


class AnalystMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "analyst.sqlite3"
        self.connection = connect_database(self.database_path)
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def _seed_review(self) -> int:
        with self.connection:
            account_id = int(self.connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username)
                   VALUES ('discord','subject-1','subject')"""
            ).lastrowid)
            message_id = int(self.connection.execute(
                     """INSERT INTO messages(community_id,platform,platform_message_id,platform_account_id,channel_id,
                                          content_raw,content_normalized,sent_at)
                         VALUES (1,'discord','review-1',?,'channel-1','suspicious text','suspicious text',
                           '2026-08-11T12:00:00+00:00')""",
                (account_id,),
            ).lastrowid)
            return int(self.connection.execute(
                """INSERT INTO review_queue(message_id,severity,queue_reason_code)
                   VALUES (?,'high','analyst_test')""",
                (message_id,),
            ).lastrowid)

    def test_confirmed_review_creates_bounded_action_job_and_audit(self) -> None:
        review_id = self._seed_review()
        with self.connection:
            action_id = resolve_review(
                self.connection,
                review_id,
                resolution="confirmed",
                tenant=TenantContext(1),
                actor=ActorAttribution("operator", 42),
                note="corroborated evidence",
                action_type="timeout",
                duration_seconds=999999999,
            )

        review = self.connection.execute(
            "SELECT status,resolution,resolution_note,resolved_by_operator_id,resolved_at FROM review_queue WHERE id=?",
            (review_id,),
        ).fetchone()
        action = self.connection.execute(
            "SELECT action_type,status,duration_seconds,actor_type,actor_id FROM moderation_actions WHERE id=?",
            (action_id,),
        ).fetchone()
        job = self.connection.execute(
            "SELECT stage,status,idempotency_key FROM processing_jobs WHERE idempotency_key=?",
            (f"review:{review_id}:moderation:timeout",),
        ).fetchone()
        self.assertEqual(tuple(review[:4]), ("resolved", "confirmed", "corroborated evidence", 42))
        self.assertIsNotNone(review[4])
        self.assertEqual(tuple(action), ("timeout", "pending", 2_419_200, "operator", 42))
        self.assertEqual(tuple(job), ("action", "pending", f"review:{review_id}:moderation:timeout"))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='moderation.review_resolved' AND actor_id=42"
        ).fetchone()[0], 1)

    def test_rule_management_validates_policy_and_records_audit(self) -> None:
        with self.connection:
            rule_id = save_moderation_rule(
                self.connection,
                name="High-risk phrase",
                rule_type="banned_phrase",
                pattern="dangerous phrase",
                severity="high",
                auto_enforce_action="timeout",
                enabled=True,
                enforcement_mode="review",
                action_duration_seconds=900,
                operator_id=7,
                community_id=1,
            )
        rule = self.connection.execute(
            "SELECT name,rule_type,severity,enforcement_mode,action_duration_seconds FROM moderation_rules WHERE id=?",
            (rule_id,),
        ).fetchone()
        self.assertEqual(tuple(rule), ("High-risk phrase", "banned_phrase", "high", "review", 900))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='moderation.rule_saved' AND actor_id=7"
        ).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "unsupported moderation rule type"):
            save_moderation_rule(
                self.connection,
                name="Invalid",
                rule_type="arbitrary_sql",
                pattern="x",
                severity="high",
                auto_enforce_action=None,
                enabled=True,
                enforcement_mode="review",
                action_duration_seconds=60,
                operator_id=7,
                community_id=1,
            )

    def test_rule_lifecycle_is_versioned_scoped_approved_and_reversible(self) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO operator_accounts(id,discord_user_id,discord_username,role)
                   VALUES (7,'rule-author','author','admin'),(8,'rule-approver','approver','admin')"""
            )
        other_community_id = create_community(
            self.connection, workspace_id=1, name="Other rules", slug="other-rules",
        )
        tenant = TenantContext(1)
        author = ActorAttribution("operator", 7)
        approver = ActorAttribution("operator", 8)
        config = {
            "name": "Critical Discord phrase", "rule_type": "exact_term",
            "pattern": "blocked", "severity": "critical",
            "auto_enforce_action": "ban", "action_duration_seconds": 600,
            "platform_scope": ["discord"],
        }
        version_one = create_moderation_rule_draft(
            self.connection, tenant=tenant, actor=author, config=config,
        )
        impact = preview_moderation_rule_version(
            self.connection, tenant=tenant, version_id=version_one,
            samples=["allowed", "this is blocked"],
        )
        self.assertEqual(impact, {
            "sample_count": 2, "match_count": 1, "matched_indexes": [1],
        })
        with self.assertRaisesRegex(PermissionError, "different operator"):
            publish_moderation_rule_version(
                self.connection, tenant=tenant, actor=author,
                version_id=version_one, lifecycle_state="enforce",
            )
        rule_id = publish_moderation_rule_version(
            self.connection, tenant=tenant, actor=approver,
            version_id=version_one, lifecycle_state="enforce",
        )
        self.assertEqual(
            [rule.id for rule in load_enabled_moderation_rules(
                self.connection, community_id=1, platform="discord",
            ) if rule.id == rule_id],
            [rule_id],
        )
        self.assertNotIn(rule_id, [rule.id for rule in load_enabled_moderation_rules(
            self.connection, community_id=1, platform="twitch",
        )])
        add_moderation_rule_exemption(
            self.connection, tenant=tenant, actor=author, rule_id=rule_id,
            exemption_type="channel", exemption_value="trusted-channel",
            reason="Trusted staff channel",
        )
        self.assertNotIn(rule_id, [rule.id for rule in load_enabled_moderation_rules(
            self.connection, community_id=1, platform="discord",
            channel_id="trusted-channel",
        )])
        with self.assertRaisesRegex(LookupError, "not found"):
            preview_moderation_rule_version(
                self.connection, tenant=TenantContext(other_community_id),
                version_id=version_one, samples=["blocked"],
            )

        changed_config = {**config, "pattern": "replacement", "severity": "high",
                          "auto_enforce_action": "timeout"}
        version_two = create_moderation_rule_draft(
            self.connection, tenant=tenant, actor=author, config=changed_config,
        )
        publish_moderation_rule_version(
            self.connection, tenant=tenant, actor=author,
            version_id=version_two, lifecycle_state="shadow",
        )
        rollback_id = rollback_moderation_rule(
            self.connection, tenant=tenant, actor=author, version_id=version_one,
        )
        versions = self.connection.execute(
            """SELECT version_number,lifecycle_state,impact_json
               FROM moderation_rule_versions WHERE moderation_rule_id=? ORDER BY version_number""",
            (rule_id,),
        ).fetchall()
        self.assertEqual([row[0] for row in versions], [1, 2, 3])
        self.assertEqual(versions[2][1], "enforce")
        self.assertEqual(json.loads(versions[2][2]), {"rollback_of": version_one})
        self.assertEqual(self.connection.execute(
            "SELECT pattern FROM moderation_rules WHERE id=?", (rule_id,),
        ).fetchone()[0], "blocked")
        self.assertEqual(rollback_id, self.connection.execute(
            "SELECT id FROM moderation_rule_versions WHERE moderation_rule_id=? AND version_number=3",
            (rule_id,),
        ).fetchone()[0])
        self.assertEqual(self.connection.execute(
            """SELECT COUNT(*) FROM audit_log WHERE action_type IN (
                   'moderation.rule_drafted','moderation.rule_published',
                   'moderation.rule_exemption_added','moderation.rule_rolled_back')"""
        ).fetchone()[0], 6)

    def test_operational_snapshot_and_database_integrity_are_observable(self) -> None:
        with self.connection:
            record_operational_metric(
                self.connection,
                "worker.latency_ms",
                12.5,
                dimension_key="analysis:success",
                observed_at="2026-08-11T12:00:00+00:00",
            )
        snapshot = operational_readiness_snapshot(self.connection)
        self.assertIn("open_reviews", snapshot["counters"])
        self.assertEqual(snapshot["latest_metrics"][0]["metric_name"], "worker.latency_ms")
        health = database_health(self.database_path)
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["integrity"], "ok")

    def test_existing_review_queue_receives_additive_resolution_columns(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        try:
            legacy.execute(
                """CREATE TABLE review_queue(
                    id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    severity TEXT NOT NULL,
                    queue_reason_code TEXT NOT NULL,
                    assigned_operator_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                )"""
            )
            legacy.commit()
        finally:
            legacy.close()
        upgraded = connect_database(legacy_path)
        try:
            initialize_database(upgraded)
            columns = {str(row[1]) for row in upgraded.execute("PRAGMA table_info(review_queue)")}
        finally:
            upgraded.close()
        self.assertTrue({"resolution", "resolution_note", "resolved_by_operator_id"} <= columns)


if __name__ == "__main__":
    unittest.main()
