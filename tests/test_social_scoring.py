from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import connect_database, initialize_database
from src.discord import DiscordConnector
from src.intelligence.scoring import (
    SOCIAL_SCORE_MODEL_VERSION,
    calculate_social_score,
    get_current_social_score,
)
from src.intelligence.signals import refresh_user_derived_signals
from tests.pipeline_support import ingest_and_analyze


class SocialScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "social-scoring.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _ingest(self, message_id: str, content: str) -> None:
        result = ingest_and_analyze(
            DiscordConnector(self.database_path),
            {
                "id": message_id,
                "timestamp": f"2026-08-09T12:0{message_id[-1]}:00Z",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "content": content,
                "author": {"id": "scored-user", "username": "scored_user", "bot": False},
            },
        )
        self.assertEqual(result.status, "persisted")

    def test_pipeline_persists_versioned_score_runs_and_components(self) -> None:
        self._ingest("score-message-1", "hello everyone")
        self._ingest("score-message-2", "you are an asshole")

        connection = connect_database(self.database_path)
        try:
            user_id = int(connection.execute("SELECT id FROM users").fetchone()[0])
            result = get_current_social_score(connection, user_id)
            run_count = int(connection.execute(
                "SELECT COUNT(*) FROM social_score_runs WHERE user_id = ?", (user_id,)
            ).fetchone()[0])
            component_keys = {component.key for component in result.components} if result else set()
            user_row = connection.execute(
                "SELECT current_reputation_score, score_confidence, score_model_version FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(run_count, 2)
        self.assertEqual(result.model_version, SOCIAL_SCORE_MODEL_VERSION)
        self.assertIn("activity.depth", component_keys)
        self.assertIn("behavior.harmful_content", component_keys)
        self.assertEqual(user_row[0], result.score)
        self.assertEqual(user_row[1], result.confidence)
        self.assertEqual(user_row[2], SOCIAL_SCORE_MODEL_VERSION)

    def test_risk_signal_does_not_read_materialized_social_score(self) -> None:
        self._ingest("score-message-1", "you are an asshole")
        connection = connect_database(self.database_path)
        try:
            user_id = int(connection.execute("SELECT id FROM users").fetchone()[0])
            refresh_user_derived_signals(connection, user_id)
            first_risk = float(connection.execute(
                "SELECT value_real FROM derived_signals WHERE user_id=? AND signal_key='risk.composite' ORDER BY analyzer_version DESC LIMIT 1",
                (user_id,),
            ).fetchone()[0])
            connection.execute("UPDATE users SET current_reputation_score=350 WHERE id=?", (user_id,))
            refresh_user_derived_signals(connection, user_id)
            second_risk = float(connection.execute(
                "SELECT value_real FROM derived_signals WHERE user_id=? AND signal_key='risk.composite' ORDER BY analyzer_version DESC LIMIT 1",
                (user_id,),
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertEqual(first_risk, second_risk)

    def test_only_adjudicated_alerts_change_the_score(self) -> None:
        self._ingest("score-message-1", "hello everyone")
        connection = connect_database(self.database_path)
        try:
            user_id = int(connection.execute("SELECT id FROM users").fetchone()[0])
            baseline = calculate_social_score(connection, user_id).score
            connection.execute(
                """
                INSERT INTO intelligence_alerts (
                    community_id, user_id, alert_type, severity, title, summary, confidence,
                    status, disposition, dedupe_key
                ) VALUES (1, ?, 'operator_test', 'high', 'Test', 'Test', 1.0,
                          'open', NULL, 'operator-test-open')
                """,
                (user_id,),
            )
            open_alert_score = calculate_social_score(connection, user_id).score
            connection.execute(
                """
                UPDATE intelligence_alerts
                SET status='resolved', disposition='confirmed'
                WHERE dedupe_key='operator-test-open'
                """
            )
            confirmed_score = calculate_social_score(connection, user_id).score
        finally:
            connection.close()

        self.assertEqual(open_alert_score, baseline)
        self.assertLess(confirmed_score, baseline)

    def test_schema_exposes_explainable_score_storage(self) -> None:
        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            tables = {str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            user_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(users)")}
        finally:
            connection.close()

        self.assertIn("social_score_runs", tables)
        self.assertIn("social_score_components", tables)
        self.assertIn("score_confidence", user_columns)
        self.assertIn("score_model_version", user_columns)


if __name__ == "__main__":
    unittest.main()
