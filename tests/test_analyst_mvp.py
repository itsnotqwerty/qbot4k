from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.dashboard.moderation import resolve_review, save_moderation_rule
from src.db import (
    connect_database,
    database_health,
    initialize_database,
    operational_readiness_snapshot,
    record_operational_metric,
)


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
                """INSERT INTO messages(platform,platform_message_id,platform_account_id,channel_id,
                                          content_raw,content_normalized,sent_at)
                   VALUES ('discord','review-1',?,'channel-1','suspicious text','suspicious text',
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
                operator_id=42,
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
            )

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
