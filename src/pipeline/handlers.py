from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

def claim_processing_job(
    connection: sqlite3.Connection,
    *,
    stage: str,
    worker_id: str,
) -> sqlite3.Row | None:
    normalized_stage = stage.strip().casefold()
    normalized_worker_id = worker_id.strip()

    if normalized_stage not in {"analysis", "action"}:
        raise ValueError(
            "stage must be either 'analysis' or 'action'"
        )

    if not normalized_worker_id:
        raise ValueError("worker_id must not be empty")

    connection.execute("BEGIN IMMEDIATE")

    try:
        row = connection.execute(
            """
            SELECT id
            FROM processing_jobs
            WHERE stage = ?
              AND status IN ('pending', 'retry')
              AND available_at <= CURRENT_TIMESTAMP
              AND attempts < max_attempts
            ORDER BY priority ASC, available_at ASC, id ASC
            LIMIT 1
            """,
            (normalized_stage,),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        job_id = int(row["id"])

        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'running',
                attempts = attempts + 1,
                claimed_at = CURRENT_TIMESTAMP,
                claimed_by = ?,
                completed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN ('pending', 'retry')
            """,
            (normalized_worker_id, job_id),
        )

        if cursor.rowcount != 1:
            connection.rollback()
            return None

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return connection.execute(
        """
        SELECT *
        FROM processing_jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()

def permanently_fail_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
    error: str,
) -> None:
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'failed',
                claimed_at = NULL,
                claimed_by = NULL,
                completed_at = CURRENT_TIMESTAMP,
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN ('running', 'pending', 'retry')
            """,
            (
                error.strip()[:2000],
                job_id,
            ),
        )

    if cursor.rowcount != 1:
        raise ValueError(
            f"Active processing job {job_id} was not found"
        )

def retry_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    retry_delay_seconds: int | None = None,
) -> bool:
    row = connection.execute(
        """
        SELECT attempts, max_attempts
        FROM processing_jobs
        WHERE id = ?
          AND status = 'running'
        """,
        (job_id,),
    ).fetchone()

    if row is None:
        raise ValueError(
            f"Running processing job {job_id} was not found"
        )

    attempts = int(row["attempts"])
    max_attempts = int(row["max_attempts"])

    if attempts >= max_attempts:
        permanently_fail_processing_job(
            connection,
            job_id,
            error,
        )
        return False

    if retry_delay_seconds is None:
        # attempts starts at 1. Delays are 2, 4, 8, 16...
        retry_delay_seconds = min(300, 2**attempts)

    retry_delay_seconds = max(0, int(retry_delay_seconds))
    available_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=retry_delay_seconds)
    ).isoformat()

    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'retry',
                available_at = ?,
                claimed_at = NULL,
                claimed_by = NULL,
                completed_at = NULL,
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'running'
            """,
            (
                available_at,
                error.strip()[:2000],
                job_id,
            ),
        )

    if cursor.rowcount != 1:
        raise ValueError(
            f"Could not retry processing job {job_id}"
        )

    return True

def complete_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
) -> None:
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP,
                claimed_at = NULL,
                claimed_by = NULL,
                last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'running'
            """,
            (job_id,),
        )

    if cursor.rowcount != 1:
        raise ValueError(
            f"Running processing job {job_id} was not found"
        )