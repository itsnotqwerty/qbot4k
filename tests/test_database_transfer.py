from __future__ import annotations

import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.database_transfer import _load_manifest, export_sqlite_database
from src.db import connect_database, initialize_database
from src.intelligence.community import create_community


class DatabaseTransferTests(unittest.TestCase):
    def test_export_manifest_is_deterministic_and_complete(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.sqlite3"
            connection = connect_database(source_path)
            try:
                initialize_database(connection)
                create_community(
                    connection,
                    workspace_id=1,
                    name="Peer Community",
                    slug="peer-community",
                )
            finally:
                connection.close()

            first_path = export_sqlite_database(source_path, root / "first")
            second_path = export_sqlite_database(source_path, root / "second")
            first = json.loads(first_path.read_text(encoding="utf-8"))
            second = json.loads(second_path.read_text(encoding="utf-8"))

            self.assertEqual(first["schema_version"], 27)
            self.assertEqual(first["orphan_check"]["count"], 0)
            self.assertEqual(first["totals"], second["totals"])
            self.assertEqual(first["tables"], second["tables"])
            self.assertEqual(first["tables"]["communities"]["row_count"], 2)
            self.assertIn("ownership", first["tables"]["communities"])
            self.assertNotIn("observation_fts", first["tables"])
            self.assertNotIn(
                "unclassified",
                {metadata["ownership"]["scope"] for metadata in first["tables"].values()},
            )

    def test_invalid_orphan_manifest_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps({
                    "format_version": 1,
                    "tables": {},
                    "orphan_check": {"count": 1, "rows": [["child", 1, "parent", 0]]},
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "foreign-key orphans"):
                _load_manifest(manifest_path)

    def test_final_export_can_make_source_read_only(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.sqlite3"
            connection = connect_database(source_path)
            initialize_database(connection)
            connection.close()

            export_sqlite_database(
                source_path, root / "export", mark_source_read_only=True,
            )

            self.assertEqual(source_path.stat().st_mode & stat.S_IWUSR, 0)


if __name__ == "__main__":
    unittest.main()