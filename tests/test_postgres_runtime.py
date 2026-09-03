from __future__ import annotations

import unittest
from unittest import mock
from datetime import UTC, datetime

from src.db_protocol import PostgreSQLConnection, translate_postgresql_query
from src.jobs import create_database_backup


class PostgreSQLRuntimeTranslationTests(unittest.TestCase):
    def test_placeholders_skip_quoted_question_marks(self) -> None:
        translated = translate_postgresql_query(
            "SELECT '?' AS literal, name FROM users WHERE id=? AND name='it''s?'"
        )
        self.assertEqual(
            translated,
            "SELECT '?' AS literal, name FROM users WHERE id=%s AND name='it''s?'",
        )

    def test_literal_percent_is_escaped_for_psycopg(self) -> None:
        translated = translate_postgresql_query(
            "SELECT * FROM sample WHERE name LIKE '%' || ? || '%' AND id=%s"
        )
        self.assertEqual(
            translated,
            "SELECT * FROM sample WHERE name LIKE '%%' || %s || '%%' AND id=%s",
        )

    def test_insert_or_ignore_uses_postgresql_conflict_clause(self) -> None:
        self.assertEqual(
            translate_postgresql_query("INSERT OR IGNORE INTO sample(value) VALUES (?)"),
            "INSERT INTO sample(value) VALUES (%s) ON CONFLICT DO NOTHING",
        )

    def test_nocase_comparison_and_ordering_use_lower(self) -> None:
        translated = translate_postgresql_query(
            "SELECT name FROM users WHERE username=? COLLATE NOCASE "
            "ORDER BY COALESCE(display_name, '') COLLATE NOCASE, name COLLATE NOCASE"
        )
        self.assertIn("LOWER(username) = LOWER(%s)", translated)
        self.assertIn("LOWER(COALESCE(display_name, ''))", translated)
        self.assertIn("LOWER(name)", translated)
        self.assertNotIn("COLLATE NOCASE", translated)

    def test_json_datetime_and_transaction_translation(self) -> None:
        query = translate_postgresql_query(
            "SELECT json_extract(metadata_json,'$.name') FROM sample "
            "WHERE datetime(created_at)>=datetime('now','-5 minutes')"
        )
        self.assertIn("metadata_json::jsonb ->> 'name'", query)
        self.assertIn("(created_at)::timestamptz", query)
        self.assertIn("CURRENT_TIMESTAMP - INTERVAL '5 minutes'", query)
        self.assertEqual(translate_postgresql_query("BEGIN IMMEDIATE"), "BEGIN")

    def test_timestamp_comparison_and_bucket_translation(self) -> None:
        translated = translate_postgresql_query(
            """SELECT MAX(0,(julianday(?)-julianday(created_at))*86400),
                      strftime('%Y-%m-%dT%H:%M:00+00:00',created_at)
               FROM sample
               WHERE datetime(created_at)>=datetime(?,'-24 hours')
                 AND datetime(created_at)<=datetime(?, ?)"""
        )
        self.assertIn("GREATEST(0,", translated)
        self.assertIn("EXTRACT(EPOCH FROM %s::timestamptz)", translated)
        self.assertIn("EXTRACT(EPOCH FROM created_at::timestamptz)", translated)
        self.assertIn("(%s::timestamptz + INTERVAL '-24 hours')", translated)
        self.assertIn("(%s::timestamptz + %s::interval)", translated)
        self.assertIn("to_char(date_trunc('minute'", translated)

    def test_translated_timestamp_rhs_and_scalar_min_max_cast_text_columns(self) -> None:
        translated = translate_postgresql_query(
            "SELECT MIN(first_at,?),MAX(score,?) FROM sample "
            "WHERE occurred_at>=datetime('now','-5 minutes') "
            "AND ingested_at>=datetime(?,?) AND datetime(sent_at)>=datetime(?)"
        )
        self.assertIn("LEAST(first_at,%s)", translated)
        self.assertIn("GREATEST(score,%s)", translated)
        self.assertIn("(occurred_at)::timestamptz >= (CURRENT_TIMESTAMP - INTERVAL '5 minutes')", translated)
        self.assertIn("(ingested_at)::timestamptz >= (%s::timestamptz + %s::interval)", translated)
        self.assertIn("(sent_at)::timestamptz>=%s::timestamptz", translated)
        self.assertNotIn("(timestamptz)::timestamptz", translated)
        self.assertIn(
            "EXTRACT(EPOCH FROM MAX(observed_at)::timestamptz)",
            translate_postgresql_query("SELECT julianday(MAX(observed_at)) FROM sample"),
        )

    def test_fts_query_uses_tsvector_rank_and_reuses_match_parameter(self) -> None:
        raw_connection = mock.MagicMock()
        connection = PostgreSQLConnection(raw_connection)
        connection.execute(
            """SELECT bm25(observation_fts) AS rank
               FROM observation_fts JOIN observations AS o ON o.id=observation_fts.rowid
               WHERE o.community_id=? AND observation_fts MATCH ?
               ORDER BY rank ASC""",
            (7, '"cobalt lantern"'),
        )

        translated, parameters = raw_connection.execute.call_args.args
        self.assertIn("FROM observations AS o", translated)
        self.assertIn("o.search_vector @@ websearch_to_tsquery('simple', %s)", translated)
        self.assertIn("-ts_rank_cd(o.search_vector", translated)
        self.assertEqual(parameters, ('"cobalt lantern"', 7, '"cobalt lantern"'))

    def test_processing_claim_uses_skip_locked_and_interval(self) -> None:
        claim = translate_postgresql_query(
            """SELECT id,community_id FROM processing_jobs
                             WHERE stage=? AND available_at<=CURRENT_TIMESTAMP
                                 AND lease_expires_at<=CURRENT_TIMESTAMP
                             ORDER BY priority ASC, id ASC LIMIT 1"""
        )
        lease = translate_postgresql_query(
            """UPDATE processing_jobs SET
               lease_expires_at=datetime(CURRENT_TIMESTAMP, '+2 minutes') WHERE id=?"""
        )

        self.assertTrue(claim.endswith("LIMIT 1 FOR UPDATE SKIP LOCKED"))
        self.assertIn("(available_at)::timestamptz <= CURRENT_TIMESTAMP", claim)
        self.assertIn("(lease_expires_at)::timestamptz <= CURRENT_TIMESTAMP", claim)
        self.assertIn("CURRENT_TIMESTAMP::timestamptz + INTERVAL '2 minutes'", lease)

    def test_cursor_lastrowid_uses_session_sequence_value(self) -> None:
        raw_connection = mock.MagicMock()
        insert_cursor = mock.MagicMock()
        insert_cursor.rowcount = 1
        insert_cursor.connection.execute.return_value.fetchone.return_value = (42,)
        raw_connection.execute.return_value = insert_cursor
        connection = PostgreSQLConnection(raw_connection)

        cursor = connection.execute("INSERT INTO sample(name) VALUES (?)", ("fixture",))

        self.assertEqual(cursor.lastrowid, 42)
        raw_connection.execute.assert_called_once_with(
            "INSERT INTO sample(name) VALUES (%s)", ("fixture",),
        )

    def test_connection_context_commits_without_closing_connection(self) -> None:
        raw_connection = mock.MagicMock()
        connection = PostgreSQLConnection(raw_connection)

        with connection as active:
            self.assertIs(active, connection)

        raw_connection.commit.assert_called_once_with()
        raw_connection.rollback.assert_not_called()
        raw_connection.close.assert_not_called()

    def test_connection_context_rolls_back_on_error(self) -> None:
        raw_connection = mock.MagicMock()
        connection = PostgreSQLConnection(raw_connection)

        with self.assertRaisesRegex(ValueError, "fixture"):
            with connection:
                raise ValueError("fixture")

        raw_connection.rollback.assert_called_once_with()
        raw_connection.commit.assert_not_called()

    def test_sqlite_backup_api_rejects_postgresql_target_explicitly(self) -> None:
        with self.assertRaisesRegex(NotImplementedError, "DF4-06"):
            create_database_backup(
                "postgresql://fixture.invalid/qbot4k",
                mock.Mock(),
                datetime.now(UTC),
            )


if __name__ == "__main__":
    unittest.main()