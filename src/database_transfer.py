from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .db import connect_database, initialize_database
from .db_protocol import DatabaseConnection
from .schema_scope import SCHEMA_SCOPE_INVENTORY


FORMAT_VERSION = 1
EXCLUDED_TABLE_PREFIXES = ("observation_fts", "sqlite_")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _encode(value: object) -> object:
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    return value


def _decode(value: object) -> object:
    if isinstance(value, Mapping) and set(value) == {"$base64"}:
        return base64.b64decode(str(value["$base64"]), validate=True)
    return value


def _canonical_row(values: Sequence[object]) -> str:
    return json.dumps(
        [_encode(value) for value in values],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        )
        if not str(row[0]).startswith(EXCLUDED_TABLE_PREFIXES)
    ]


def _table_metadata(
    connection: sqlite3.Connection, table: str,
) -> tuple[list[str], list[str], list[str]]:
    columns = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    column_names = [str(row[1]) for row in columns]
    primary_key = [
        str(row[1])
        for row in sorted((row for row in columns if int(row[5]) > 0), key=lambda row: int(row[5]))
    ]
    dependencies = sorted({
        str(row[2])
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})")
        if str(row[2]) != table
    })
    return column_names, primary_key, dependencies


def _ordered_rows(
    connection: Any, table: str, columns: Sequence[str], primary_key: Sequence[str],
) -> Iterable[Sequence[object]]:
    selected = ",".join(_quote(column) for column in columns)
    order_columns = primary_key or columns
    order = ",".join(_quote(column) for column in order_columns)
    return connection.execute(
        f"SELECT {selected} FROM {_quote(table)} ORDER BY {order}"
    )


def _checksum_rows(rows: Iterable[Sequence[object]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _ownership_check(connection: Any, table: str) -> dict[str, object]:
    rule = SCHEMA_SCOPE_INVENTORY.get(table)
    if rule is None:
        return {"scope": "unclassified", "checked_rows": 0, "unowned_rows": 0}
    if rule.scope == "global" or rule.owner_column is None or rule.owner_table is None:
        return {"scope": rule.scope, "checked_rows": 0, "unowned_rows": 0}
    owner_column = _quote(rule.owner_column)
    owner_table = _quote(rule.owner_table)
    child_table = _quote(table)
    row = connection.execute(
        f"""SELECT COUNT(*),
                   SUM(CASE WHEN child.{owner_column} IS NOT NULL AND owner.id IS NULL THEN 1 ELSE 0 END)
            FROM {child_table} AS child
            LEFT JOIN {owner_table} AS owner ON owner.id=child.{owner_column}"""
    ).fetchone()
    return {
        "scope": rule.scope,
        "checked_rows": int(row[0]),
        "unowned_rows": int(row[1] or 0),
    }


def export_sqlite_database(
    source_path: Path,
    output_directory: Path,
    *,
    mark_source_read_only: bool = False,
) -> Path:
    source_path = source_path.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(source_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA foreign_keys=ON")
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()
        foreign_key_rows = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        tables: dict[str, dict[str, object]] = {}
        total_rows = 0
        for table in _table_names(connection):
            columns, primary_key, dependencies = _table_metadata(connection, table)
            data_path = output_directory / f"{table}.jsonl"
            digest = hashlib.sha256()
            row_count = 0
            with data_path.open("w", encoding="utf-8", newline="\n") as output:
                for row in _ordered_rows(connection, table, columns, primary_key):
                    line = _canonical_row(row)
                    output.write(line + "\n")
                    digest.update(line.encode("utf-8"))
                    digest.update(b"\n")
                    row_count += 1
            total_rows += row_count
            tables[table] = {
                "columns": columns,
                "primary_key": primary_key,
                "dependencies": dependencies,
                "row_count": row_count,
                "sha256": digest.hexdigest(),
                "ownership": _ownership_check(connection, table),
            }
        manifest = {
            "format_version": FORMAT_VERSION,
            "schema_version": int(schema_row[0]),
            "source": str(source_path),
            "tables": tables,
            "totals": {"tables": len(tables), "rows": total_rows},
            "orphan_check": {
                "count": len(foreign_key_rows),
                "rows": foreign_key_rows,
            },
        }
        manifest_path = output_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    finally:
        connection.close()
    if mark_source_read_only:
        source_path.chmod(source_path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return manifest_path


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported database transfer manifest version")
    if not isinstance(manifest.get("tables"), dict):
        raise ValueError("database transfer manifest has no tables")
    if int(manifest.get("orphan_check", {}).get("count", -1)) != 0:
        raise ValueError("source database contains foreign-key orphans")
    unowned = [
        table
        for table, metadata in manifest["tables"].items()
        if int(metadata.get("ownership", {}).get("unowned_rows", 0)) != 0
    ]
    if unowned:
        raise ValueError(f"source database contains unowned rows: {', '.join(sorted(unowned))}")
    unclassified = [
        table
        for table, metadata in manifest["tables"].items()
        if metadata.get("ownership", {}).get("scope") == "unclassified"
    ]
    if unclassified:
        raise ValueError(
            f"source database contains unclassified tables: {', '.join(sorted(unclassified))}"
        )
    return manifest


def _foreign_key_order(tables: Mapping[str, Mapping[str, object]]) -> list[str]:
    remaining = set(tables)
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            table
            for table in remaining
            if not (set(tables[table].get("dependencies", ())) & remaining)
        )
        if not ready:
            raise ValueError(f"cyclic transfer dependencies: {', '.join(sorted(remaining))}")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _verify_target(
    connection: DatabaseConnection, manifest: Mapping[str, Any],
) -> dict[str, object]:
    mismatches: list[str] = []
    total_rows = 0
    for table, metadata in manifest["tables"].items():
        columns = list(metadata["columns"])
        primary_key = list(metadata["primary_key"])
        checksum, row_count = _checksum_rows(
            _ordered_rows(connection, table, columns, primary_key)
        )
        total_rows += row_count
        if row_count != int(metadata["row_count"]):
            mismatches.append(f"{table}: row count")
        if checksum != metadata["sha256"]:
            mismatches.append(f"{table}: checksum")
        ownership = _ownership_check(connection, table)
        if int(ownership["unowned_rows"]) != 0:
            mismatches.append(f"{table}: tenant ownership")
    invalid_constraints = int(connection.execute(
        "SELECT COUNT(*) FROM pg_constraint WHERE NOT convalidated"
    ).fetchone()[0])
    if invalid_constraints:
        mismatches.append("unvalidated PostgreSQL constraints")
    if total_rows != int(manifest["totals"]["rows"]):
        mismatches.append("source/target row totals")
    if mismatches:
        raise ValueError("database transfer verification failed: " + ", ".join(mismatches))
    return {
        "schema_version": int(manifest["schema_version"]),
        "tables": len(manifest["tables"]),
        "rows": total_rows,
        "constraints_validated": invalid_constraints == 0,
    }


def import_postgresql_database(
    manifest_path: Path,
    database_url: str,
    *,
    replace_target: bool = False,
) -> dict[str, object]:
    if not replace_target:
        raise ValueError("PostgreSQL import requires explicit replace_target=True")
    manifest = _load_manifest(manifest_path)
    tables: dict[str, dict[str, object]] = manifest["tables"]
    order = _foreign_key_order(tables)
    connection = connect_database(database_url)
    try:
        initialize_database(connection)
        quoted_tables = ",".join(_quote(table) for table in order)
        connection.execute(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE")
        for table in order:
            metadata = tables[table]
            columns = list(metadata["columns"])
            placeholders = ",".join("?" for _ in columns)
            statement = (
                f"INSERT INTO {_quote(table)} "
                f"({','.join(_quote(column) for column in columns)}) VALUES ({placeholders})"
            )
            rows = []
            data_path = manifest_path.parent / f"{table}.jsonl"
            with data_path.open(encoding="utf-8") as source:
                for line in source:
                    rows.append(tuple(_decode(value) for value in json.loads(line)))
            if rows:
                connection.executemany(statement, rows)
        identity_rows = connection.execute(
            """SELECT table_name,column_name FROM information_schema.columns
               WHERE table_schema='public' AND is_identity='YES' ORDER BY table_name"""
        ).fetchall()
        for table, column in identity_rows:
            connection.execute(
                f"""SELECT setval(
                        pg_get_serial_sequence('{table}','{column}'),
                        COALESCE((SELECT MAX({_quote(str(column))}) FROM {_quote(str(table))}),1),
                        EXISTS(SELECT 1 FROM {_quote(str(table))})
                    )"""
            )
        result = _verify_target(connection, manifest)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transfer QBot4K SQLite data to PostgreSQL")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("source", type=Path)
    export_parser.add_argument("output", type=Path)
    export_parser.add_argument("--mark-source-read-only", action="store_true")
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("manifest", type=Path)
    import_parser.add_argument("database_url")
    import_parser.add_argument("--replace-target", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "export":
        path = export_sqlite_database(
            arguments.source,
            arguments.output,
            mark_source_read_only=arguments.mark_source_read_only,
        )
        print(path)
    else:
        result = import_postgresql_database(
            arguments.manifest,
            arguments.database_url,
            replace_target=arguments.replace_target,
        )
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())