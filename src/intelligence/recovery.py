from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


def flush_raw_event_archive(
    connection: sqlite3.Connection, archive_root: Path, *, limit: int = 1000
) -> int:
    """Durably materialize the append-only event ledger outside the query store."""
    root = Path(archive_root)
    rows = connection.execute(
        """SELECT id, community_id, platform, event_type, payload_sha256,
                  payload_json, received_at
           FROM raw_event_archive WHERE archive_path IS NULL
           ORDER BY id LIMIT ?""",
        (max(1, int(limit)),),
    ).fetchall()
    flushed = 0
    for row in rows:
        received = datetime.fromisoformat(str(row[6]).replace("Z", "+00:00"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        directory = root / f"community-{int(row[1])}" / received.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{int(row[0])}-{str(row[4])[:16]}.json"
        envelope = {
            "archive_id": int(row[0]), "community_id": int(row[1]),
            "platform": str(row[2]), "event_type": str(row[3]),
            "payload_sha256": str(row[4]), "received_at": str(row[6]),
            "payload": json.loads(str(row[5]) or "{}"),
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=".raw-event-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        with connection:
            connection.execute(
                "UPDATE raw_event_archive SET archive_path=? WHERE id=? AND archive_path IS NULL",
                (str(destination), int(row[0])),
            )
        flushed += 1
    return flushed


def record_dead_letter(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    error: BaseException | str,
    payload: Mapping[str, object] | None = None,
) -> int:
    job = connection.execute(
        """SELECT stage, observation_id, payload_json,
                  COALESCE(o.community_id, 1)
           FROM processing_jobs j LEFT JOIN observations o ON o.id=j.observation_id
           WHERE j.id=?""",
        (int(job_id),),
    ).fetchone()
    if job is None:
        raise ValueError(f"processing job {job_id} was not found")
    error_class = error.__class__.__name__ if isinstance(error, BaseException) else "ProcessingError"
    body = dict(payload or {})
    if not body:
        try:
            body = json.loads(str(job[2]) or "{}")
        except json.JSONDecodeError:
            body = {"unparsed_payload": str(job[2])}
    with connection:
        cursor = connection.execute(
            """INSERT INTO dead_letter_events(
                   community_id, observation_id, processing_job_id, stage,
                   error_class, error_message, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(job[3]), job[1], int(job_id), str(job[0]), error_class,
             str(error)[:2000], json.dumps(body, sort_keys=True, default=str)),
        )
    return int(cursor.lastrowid)


def replay_dead_letter(connection: sqlite3.Connection, dead_letter_id: int) -> int:
    row = connection.execute(
        """SELECT d.observation_id, d.stage, j.job_type, d.status
           FROM dead_letter_events d LEFT JOIN processing_jobs j ON j.id=d.processing_job_id
           WHERE d.id=?""",
        (int(dead_letter_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"dead letter {dead_letter_id} was not found")
    if row[0] is None or row[2] is None:
        raise ValueError("dead letter has no replayable observation")
    key = f"dead-letter:{int(dead_letter_id)}:{datetime.now(timezone.utc).isoformat()}"
    with connection:
        cursor = connection.execute(
            """INSERT INTO processing_jobs(stage, job_type, observation_id, idempotency_key)
               VALUES (?, ?, ?, ?)""",
            (str(row[1]), str(row[2]), int(row[0]), key),
        )
        connection.execute(
            "UPDATE dead_letter_events SET status='replayed', replayed_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(dead_letter_id),),
        )
    return int(cursor.lastrowid)
