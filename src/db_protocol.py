from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, Self, runtime_checkable
from urllib.parse import unquote, urlparse


DatabaseParameters = Sequence[object] | Mapping[str, object]


class DatabaseRow(Protocol):
    def __getitem__(self, key: int | str) -> Any: ...

    def __iter__(self) -> Iterator[Any]: ...

    def keys(self) -> list[str]: ...


class DatabaseCursor(Protocol):
    lastrowid: int | None
    rowcount: int

    def fetchone(self) -> DatabaseRow | None: ...

    def fetchall(self) -> list[DatabaseRow]: ...

    def __iter__(self) -> Iterator[DatabaseRow]: ...


@runtime_checkable
class DatabaseConnection(Protocol):
    def execute(
        self, sql: str, parameters: DatabaseParameters = (),
    ) -> DatabaseCursor: ...

    def executemany(
        self, sql: str, parameters: Iterable[DatabaseParameters],
    ) -> DatabaseCursor: ...

    def executescript(self, sql_script: str) -> DatabaseCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None: ...


class PostgreSQLRow:
    """Row shape compatible with the numeric and named access used by SQLite callers."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._positions = {name: index for index, name in enumerate(self._columns)}

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            return self._values[self._positions[key]]
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)


def _replace_qmark_placeholders(sql: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            output.append("%%" if character == "%" else character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        elif character == "%":
            if index + 1 < len(sql) and sql[index + 1] in {"s", "b", "t", "%"}:
                output.extend((character, sql[index + 1]))
                index += 1
            else:
                output.append("%%")
        else:
            output.append(character)
        index += 1
    return "".join(output)


def translate_postgresql_query(sql: str) -> str:
    translated = sql.strip()
    if translated.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    add_conflict_clause = bool(re.match(r"INSERT\s+OR\s+IGNORE\b", translated, re.IGNORECASE))
    translated = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"([a-zA-Z_][a-zA-Z0-9_.]*)\s*=\s*(\?)\s+COLLATE\s+NOCASE",
        r"LOWER(\1) = LOWER(\2)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"(COALESCE\([^)]*\)|[a-zA-Z_][a-zA-Z0-9_.]*)\s+COLLATE\s+NOCASE",
        r"LOWER(\1)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"json_extract\(\s*([a-zA-Z0-9_.]+)\s*,\s*'\$\.([a-zA-Z0-9_]+)'\s*\)",
        r"(\1::jsonb ->> '\2')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*'now'\s*,\s*'(-?[^']+)'\s*\)",
        lambda match: (
            f"(CURRENT_TIMESTAMP {'-' if match.group(1).startswith('-') else '+'} "
            f"INTERVAL '{match.group(1).lstrip('+-')}')"
        ),
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*'now'\s*,\s*\?\s*\)",
        "(CURRENT_TIMESTAMP + %s::interval)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*\?\s*,\s*'([^']+)'\s*\)",
        lambda match: f"(%s::timestamptz + INTERVAL '{match.group(1)}')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*\?\s*,\s*\?\s*\)",
        "(%s::timestamptz + %s::interval)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*([a-zA-Z0-9_.]+)\s*,\s*'([^']+)'\s*\)",
        lambda match: (
            f"({match.group(1)}::timestamptz + INTERVAL '{match.group(2).lstrip('+')}')"
        ),
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*\?\s*\)", "%s::timestamptz", translated, flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", translated, flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(\s*([a-zA-Z0-9_.]+)\s*\)",
        r"(\1)::timestamptz",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\b([a-zA-Z_][a-zA-Z0-9_.]*)\s*(<=|>=|<|>)\s*CURRENT_TIMESTAMP\b",
        r"(\1)::timestamptz \2 CURRENT_TIMESTAMP",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"(?<!::)\b([a-zA-Z_][a-zA-Z0-9_.]*)\s*(<=|>=|<|>)\s*"
        r"(\([^)]*(?:CURRENT_TIMESTAMP|::timestamptz)[^)]*\)|%s::timestamptz)",
        r"(\1)::timestamptz \2 \3",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"julianday\(\s*'now'\s*\)",
        "(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) / 86400.0)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"julianday\(\s*\?\s*\)",
        "(EXTRACT(EPOCH FROM %s::timestamptz) / 86400.0)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"julianday\(\s*([a-zA-Z0-9_.]+)\s*\)",
        r"(EXTRACT(EPOCH FROM \1::timestamptz) / 86400.0)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"julianday\(\s*(MAX|MIN)\(\s*([a-zA-Z0-9_.]+)\s*\)\s*\)",
        r"(EXTRACT(EPOCH FROM \1(\2)::timestamptz) / 86400.0)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(r"\bMIN\((?=[^()]*,)", "LEAST(", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bMAX\((?=[^()]*,)", "GREATEST(", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"strftime\(\s*'%Y-%m-%dT%H:%M:00\+00:00'\s*,\s*([a-zA-Z0-9_.]+)\s*\)",
        r"to_char(date_trunc('minute', \1::timestamptz AT TIME ZONE 'UTC'), "
        "'YYYY-MM-DD\"T\"HH24:MI:00+00:00')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"observation_fts\s+JOIN\s+observations\s+AS\s+o\s+"
        r"ON\s+o\.id\s*=\s*observation_fts\.rowid",
        "observations AS o",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"observation_fts\s+MATCH\s+\?",
        "o.search_vector @@ websearch_to_tsquery('simple', ?)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"bm25\(\s*observation_fts\s*\)",
        "-ts_rank_cd(o.search_vector, websearch_to_tsquery('simple', ?))",
        translated,
        flags=re.IGNORECASE,
    )
    if re.search(
        r"SELECT\s+id\s*,\s*community_id\s+FROM\s+processing_jobs\b",
        translated,
        re.IGNORECASE,
    ) and re.search(r"ORDER\s+BY\s+priority\b.*?LIMIT\s+1\s*$", translated, re.IGNORECASE | re.DOTALL):
        translated += " FOR UPDATE SKIP LOCKED"
    translated = _replace_qmark_placeholders(translated)
    if add_conflict_clause and " ON CONFLICT" not in translated.upper():
        translated = translated.rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated


class PostgreSQLCursor:
    def __init__(self, cursor: Any, *, insert_statement: bool = False) -> None:
        self._cursor = cursor
        self._lastrowid: int | None = None
        if insert_statement and int(cursor.rowcount) == 1:
            connection = cursor.connection
            try:
                connection.execute("SAVEPOINT qbot4k_lastrowid")
                row = connection.execute("SELECT lastval()").fetchone()
                self._lastrowid = int(row[0]) if row is not None else None
            except Exception:
                connection.execute("ROLLBACK TO SAVEPOINT qbot4k_lastrowid")
                self._lastrowid = None
            finally:
                connection.execute("RELEASE SAVEPOINT qbot4k_lastrowid")

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> DatabaseRow | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[DatabaseRow]:
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[DatabaseRow]:
        return iter(self._cursor)


class PostgreSQLConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(
        self, sql: str, parameters: DatabaseParameters = (),
    ) -> DatabaseCursor:
        translated = translate_postgresql_query(sql)
        translated_parameters = parameters
        fts_match = re.search(r"observation_fts\s+MATCH\s+\?", sql, re.IGNORECASE)
        if fts_match and re.search(r"bm25\(\s*observation_fts\s*\)", sql, re.IGNORECASE):
            if isinstance(parameters, Mapping):
                raise TypeError("FTS ranking requires positional parameters")
            match_index = sql[:fts_match.start()].count("?")
            translated_parameters = (parameters[match_index], *parameters)
        cursor = self._connection.execute(translated, translated_parameters)
        return PostgreSQLCursor(
            cursor, insert_statement=translated.lstrip().upper().startswith("INSERT "),
        )

    def executemany(
        self, sql: str, parameters: Iterable[DatabaseParameters],
    ) -> DatabaseCursor:
        cursor = self._connection.cursor()
        translated = translate_postgresql_query(sql)
        cursor.executemany(translated, parameters)
        return PostgreSQLCursor(
            cursor, insert_statement=translated.lstrip().upper().startswith("INSERT "),
        )

    def executescript(self, sql_script: str) -> DatabaseCursor:
        raise NotImplementedError(
            "PostgreSQL migration scripts must use the versioned migration runner"
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        return None


class DatabaseTarget:
    def __init__(self, backend: str, value: Path | str) -> None:
        self.backend = backend
        self.value = value

    @classmethod
    def parse(cls, target: Path | str) -> DatabaseTarget:
        if isinstance(target, Path):
            return cls("sqlite", target)
        parsed = urlparse(target)
        if parsed.scheme in {"postgres", "postgresql"}:
            return cls("postgresql", target)
        if parsed.scheme == "sqlite":
            sqlite_path = unquote(parsed.path)
            if parsed.netloc:
                sqlite_path = f"//{parsed.netloc}{sqlite_path}"
            return cls("sqlite", Path(sqlite_path))
        if parsed.scheme:
            raise ValueError(f"unsupported database backend: {parsed.scheme}")
        return cls("sqlite", Path(target))