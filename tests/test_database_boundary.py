from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.config import AppSettings
from src.db import connect_database
from src.db_protocol import (
    DatabaseConnection,
    DatabaseTarget,
    PostgreSQLConnection,
    PostgreSQLRow,
)


class DatabaseBoundaryTests(unittest.TestCase):
    def test_sqlite_path_preserves_existing_connection_behavior(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "boundary.sqlite3"
            connection = connect_database(database_path)
            try:
                self.assertIsInstance(connection, sqlite3.Connection)
                self.assertIsInstance(connection, DatabaseConnection)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                connection.close()

    def test_postgresql_url_selects_postgresql_adapter(self) -> None:
        sentinel = mock.Mock(spec=DatabaseConnection)
        database_url = "postgresql://fixture.invalid/qbot4k"
        with mock.patch("src.db._connect_postgresql", return_value=sentinel) as connect:
            result = connect_database(database_url)
        self.assertIs(result, sentinel)
        connect.assert_called_once_with(database_url)

    def test_postgresql_url_is_selected_by_application_configuration(self) -> None:
        database_url = "postgresql://fixture.invalid/qbot4k"
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": "/ignored/when/url/is/set.sqlite3",
            "QBOT_DATABASE_URL": database_url,
            "QBOT_ENABLED_SERVICES": "jobs",
        })
        self.assertEqual(settings.database_path, database_url)
        self.assertEqual(settings.safe_summary()["database_backend"], "postgresql")
        self.assertNotIn("fixture.invalid", str(settings.raw_archive_dir))

    def test_database_target_rejects_unknown_url_scheme(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported database backend"):
            DatabaseTarget.parse("mysql://fixture.invalid/qbot4k")

    def test_postgresql_row_supports_index_name_and_mapping_conversion(self) -> None:
        row = PostgreSQLRow(["id", "name"], [7, "fixture"])
        self.assertEqual(row[0], 7)
        self.assertEqual(row["name"], "fixture")
        self.assertEqual(dict(row), {"id": 7, "name": "fixture"})

    def test_postgresql_adapter_satisfies_protocol_and_gates_sqlite_scripts(self) -> None:
        raw_connection = mock.Mock()
        connection = PostgreSQLConnection(raw_connection)
        self.assertIsInstance(connection, DatabaseConnection)
        with self.assertRaisesRegex(NotImplementedError, "versioned migration runner"):
            connection.executescript("SELECT 1;")


if __name__ == "__main__":
    unittest.main()