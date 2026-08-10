from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import connect_database, initialize_database
from src.discord import DiscordConnector
from src.intelligence.signals import (
    SIGNAL_ANALYZER_VERSION,
    derived_signal_count,
    list_signal_overview,
    list_user_derived_signals,
    refresh_all_derived_signals,
    refresh_user_derived_signals,
)
from tests.pipeline_support import ingest_and_analyze


class DerivedSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "signals.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _ingest(self, message_id: str, content: str, channel_id: str = "channel-1") -> None:
        result = ingest_and_analyze(
            DiscordConnector(self.database_path),
            {
                "id": message_id,
                "timestamp": "2026-08-09T12:00:00Z",
                "channel_id": channel_id,
                "guild_id": "guild-1",
                "content": content,
                "author": {"id": "signal-user-1", "username": "analyst_target", "bot": False},
            },
        )
        self.assertEqual(result.status, "persisted")

    def test_message_analysis_persists_versioned_signal_set(self) -> None:
        self._ingest("signal-message-1", "thanks, great stream")
        self._ingest("signal-message-2", "you are an asshole")
        self._ingest("signal-message-3", "ordinary message", "channel-2")

        connection = connect_database(self.database_path)
        try:
            user_id = int(connection.execute("SELECT id FROM users WHERE primary_display_name = 'analyst_target'").fetchone()[0])
            signals = list_user_derived_signals(connection, user_id)
        finally:
            connection.close()

        by_key = {signal.signal_key: signal for signal in signals}
        self.assertEqual(len(by_key), 15)
        self.assertEqual(by_key["activity.message_count"].value, 3.0)
        self.assertEqual(by_key["activity.active_channel_count"].value, 2.0)
        self.assertAlmostEqual(by_key["behavior.positive_message_ratio"].value, 1 / 3)
        self.assertAlmostEqual(by_key["behavior.negative_message_ratio"].value, 1 / 3)
        self.assertGreater(by_key["risk.composite"].value, 0)
        self.assertEqual(by_key["risk.composite"].evidence_count, 3)
        self.assertEqual(by_key["risk.composite"].confidence, 0.15)
        self.assertEqual(by_key["risk.composite"].analyzer_version, SIGNAL_ANALYZER_VERSION)
        self.assertIn("formula", by_key["risk.composite"].details)
        self.assertTrue(by_key["risk.composite"].details["independent_of_social_score"])

    def test_refresh_is_idempotent_and_updates_existing_rows(self) -> None:
        self._ingest("signal-message-idempotent", "hello")
        connection = connect_database(self.database_path)
        try:
            user_id = int(connection.execute("SELECT id FROM users").fetchone()[0])
            first_ids = [int(row[0]) for row in connection.execute("SELECT id FROM derived_signals ORDER BY id").fetchall()]
            refresh_user_derived_signals(connection, user_id)
            refresh_user_derived_signals(connection, user_id)
            second_ids = [int(row[0]) for row in connection.execute("SELECT id FROM derived_signals ORDER BY id").fetchall()]
            count = derived_signal_count(connection)
        finally:
            connection.close()

        self.assertEqual(count, 15)
        self.assertEqual(first_ids, second_ids)

    def test_signal_overview_prioritizes_composite_risk(self) -> None:
        self._ingest("signal-message-overview", "you are an asshole")
        connection = connect_database(self.database_path)
        try:
            overview = list_signal_overview(connection)
        finally:
            connection.close()

        self.assertEqual(len(overview), 15)
        self.assertEqual(overview[0][0], "analyst_target")
        self.assertEqual(overview[0][1].signal_key, "risk.composite")

    def test_signal_overview_filters_multiple_keys_and_sorts_columns(self) -> None:
        self._ingest("signal-message-sort-1", "hello")
        self._ingest("signal-message-sort-2", "you are an asshole")
        selected = ("activity.message_count", "risk.composite")
        connection = connect_database(self.database_path)
        try:
            by_value = list_signal_overview(
                connection,
                signal_keys=selected,
                sort_by="value",
                sort_dir="asc",
            )
            by_confidence = list_signal_overview(connection, signal_keys=selected, sort_by="confidence", sort_dir="desc")
            by_evidence = list_signal_overview(connection, signal_keys=selected, sort_by="evidence", sort_dir="asc")
            by_timestamp = list_signal_overview(connection, signal_keys=selected, sort_by="timestamp", sort_dir="desc")
            by_signal = list_signal_overview(connection, signal_keys=selected, sort_by="signal", sort_dir="asc")
        finally:
            connection.close()

        for items in (by_value, by_confidence, by_evidence, by_timestamp, by_signal):
            self.assertEqual({signal.signal_key for _, signal in items}, set(selected))
            self.assertEqual(len(items), 2)
        self.assertEqual([signal.value for _, signal in by_value], sorted(signal.value for _, signal in by_value))
        self.assertEqual([signal.signal_key for _, signal in by_signal], sorted(selected))

    def test_refresh_all_backfills_existing_users(self) -> None:
        self._ingest("signal-message-backfill", "hello")
        connection = connect_database(self.database_path)
        try:
            connection.execute("DELETE FROM derived_signals")
            self.assertEqual(derived_signal_count(connection), 0)
            refreshed_users = refresh_all_derived_signals(connection)
            signal_count = derived_signal_count(connection)
        finally:
            connection.close()

        self.assertEqual(refreshed_users, 1)
        self.assertEqual(signal_count, 15)

    def test_schema_contains_persistent_signal_indexes(self) -> None:
        connection = connect_database(self.database_path)
        try:
            initialize_database(connection)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(derived_signals)").fetchall()}
            indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(derived_signals)").fetchall()}
        finally:
            connection.close()

        self.assertIn("analyzer_version", columns)
        self.assertIn("confidence", columns)
        self.assertIn("evidence_count", columns)
        self.assertIn("value_json", columns)
        self.assertIn("idx_derived_signals_user", indexes)


if __name__ == "__main__":
    unittest.main()
