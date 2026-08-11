from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import collect_observation, connect_database, initialize_database
from src.intelligence.analytics import record_evaluation_label, run_model_evaluation
from src.intelligence.content import analyze_observation_content, emit_content_alert
from src.intelligence.workflows import (
    add_case_entity,
    add_case_evidence,
    add_case_note,
    create_case_from_alert,
    update_alert_workflow,
    update_case,
)
from src.jobs import create_database_backup
from src.models import Observation


class P0P3DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "platform.sqlite3"
        self.connection = connect_database(self.database_path)
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_critical_content_alert_keeps_source_observation(self) -> None:
        result = collect_observation(self.connection, Observation(
            platform="external:tip", event_type="external.item", external_event_id="threat-1",
            actor_platform_user_id="actor-1", actor_username="Actor",
            context_id="public", text="I will attack the site now",
            occurred_at="2026-08-11T12:00:00+00:00",
        ))
        with self.connection:
            analysis = analyze_observation_content(self.connection, int(result.observation_id))
            alert_id = emit_content_alert(self.connection, int(result.observation_id), analysis)
        row = self.connection.execute(
            "SELECT observation_id,severity,dedupe_key FROM intelligence_alerts WHERE id=?", (alert_id,)
        ).fetchone()
        self.assertEqual((row[0], row[1]), (result.observation_id, "critical"))
        self.assertIn(str(result.observation_id), row[2])

    def test_case_and_alert_lifecycle_is_editable_and_audited(self) -> None:
        user_id = int(self.connection.execute(
            "INSERT INTO users(primary_display_name) VALUES ('Subject')"
        ).lastrowid)
        alert_id = int(self.connection.execute(
            """INSERT INTO intelligence_alerts(user_id,alert_type,severity,title,summary,confidence,dedupe_key)
               VALUES (?,'test','high','Finding','Evidence summary',0.9,'case-test')""", (user_id,)
        ).lastrowid)
        with self.connection:
            case_id = create_case_from_alert(self.connection, alert_id, operator_id=7)
            update_case(self.connection, case_id, status="active", priority="critical", operator_id=7)
            add_case_entity(self.connection, case_id, user_id, role="subject", operator_id=7)
            add_case_evidence(self.connection, case_id, alert_id=alert_id, note="Corroborated", operator_id=7)
            add_case_note(self.connection, case_id, "Analyst note", operator_id=7)
            update_alert_workflow(self.connection, alert_id, status="acknowledged", assigned_operator_id=7, operator_id=7)
        case = self.connection.execute(
            "SELECT status,priority FROM investigation_cases WHERE id=?", (case_id,)
        ).fetchone()
        alert = self.connection.execute(
            "SELECT status,assigned_operator_id,acknowledged_at FROM intelligence_alerts WHERE id=?", (alert_id,)
        ).fetchone()
        self.assertEqual(tuple(case), ("active", "critical"))
        self.assertEqual(tuple(alert[:2]), ("acknowledged", 7))
        self.assertIsNotNone(alert[2])
        self.assertGreaterEqual(self.connection.execute(
            "SELECT COUNT(*) FROM case_activity WHERE case_id=?", (case_id,)
        ).fetchone()[0], 4)

    def test_evaluation_uses_score_captured_at_adjudication(self) -> None:
        user_id = int(self.connection.execute(
            "INSERT INTO users(primary_display_name) VALUES ('Evaluated')"
        ).lastrowid)
        self.connection.execute(
            """INSERT INTO derived_signal_windows(user_id,signal_key,window_name,analyzer_version,
               value_real,confidence,evidence_count,calculated_at)
               VALUES (?,'risk.composite','24h',2,82,0.9,12,'2026-08-11')""", (user_id,)
        )
        with self.connection:
            label_id = record_evaluation_label(
                self.connection, label_key="risk", label_value="positive", user_id=user_id
            )
            self.connection.execute(
                "UPDATE derived_signal_windows SET value_real=5 WHERE user_id=?", (user_id,)
            )
            run_id = run_model_evaluation(self.connection)
        captured = self.connection.execute(
            "SELECT score_key,score_value,model_version FROM evaluation_labels WHERE id=?", (label_id,)
        ).fetchone()
        distribution = json.loads(self.connection.execute(
            "SELECT score_distribution_json FROM model_evaluation_runs WHERE id=?", (run_id,)
        ).fetchone()[0])
        self.assertEqual(tuple(captured), ("risk.composite", 82.0, 2))
        self.assertEqual(distribution["75-100"], 1)

    def test_online_backup_contains_committed_wal_data_and_passes_integrity(self) -> None:
        self.connection.execute("INSERT INTO users(primary_display_name) VALUES ('Backed up')")
        self.connection.commit()
        backup, metadata, digest = create_database_backup(
            self.database_path, self.root / "backups", datetime(2026, 8, 11, tzinfo=UTC),
            retention_count=2,
        )
        restored = connect_database(backup)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            restored.close()
        self.assertTrue(metadata.exists())
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
