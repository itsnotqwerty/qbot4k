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
        raise ValueError("stage must be either 'analysis' or 'action'")
    if not normalized_worker_id:
        raise ValueError("worker_id must not be empty")

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
                        WITH eligible_tenants AS (
                                SELECT DISTINCT community_id
                                FROM processing_jobs
                                WHERE stage = ? AND community_id IS NOT NULL
                                    AND (
                                            (status IN ('pending', 'retry') AND available_at <= CURRENT_TIMESTAMP)
                                            OR (status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP)
                                    )
                                    AND attempts < max_attempts
                        ), selected_tenant AS (
                                SELECT eligible_tenants.community_id
                                FROM eligible_tenants
                                LEFT JOIN tenant_job_schedule USING (community_id)
                                ORDER BY COALESCE(last_claim_sequence, 0), eligible_tenants.community_id
                                LIMIT 1
                        )
                        SELECT id,community_id
                        FROM processing_jobs
                        WHERE stage = ?
                            AND community_id=(SELECT community_id FROM selected_tenant)
              AND (
                  (status IN ('pending', 'retry') AND available_at <= CURRENT_TIMESTAMP)
                  OR (status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP)
              )
              AND attempts < max_attempts
            ORDER BY priority ASC, available_at ASC, id ASC
            LIMIT 1
            """,
            (normalized_stage, normalized_stage),
        ).fetchone()
        if row is None:
            connection.commit()
            return None

        job_id = int(row["id"])
        community_id = int(row["community_id"]) if row["community_id"] is not None else None
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'running',
                attempts = attempts + 1,
                claimed_at = CURRENT_TIMESTAMP,
                claimed_by = ?,
                lease_expires_at = datetime(CURRENT_TIMESTAMP, '+2 minutes'),
                completed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND (
                  status IN ('pending', 'retry')
                  OR (status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP)
              )
            """,
            (normalized_worker_id, job_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        if community_id is not None:
            connection.execute(
                """INSERT INTO tenant_job_schedule(community_id,last_claim_sequence)
                   VALUES (?,(SELECT COALESCE(MAX(last_claim_sequence),0)+1 FROM tenant_job_schedule))
                   ON CONFLICT(community_id) DO UPDATE SET
                       last_claim_sequence=(SELECT COALESCE(MAX(last_claim_sequence),0)+1
                                            FROM tenant_job_schedule)""",
                (community_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return connection.execute("SELECT * FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()


def permanently_fail_processing_job(connection: sqlite3.Connection, job_id: int, error: str) -> None:
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'failed', claimed_at = NULL, claimed_by = NULL,
                lease_expires_at = NULL, completed_at = CURRENT_TIMESTAMP,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('running', 'pending', 'retry')
            """,
            (error.strip()[:2000], job_id),
        )
    if cursor.rowcount != 1:
        raise ValueError(f"Active processing job {job_id} was not found")


def retry_processing_job(
    connection: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    retry_delay_seconds: int | None = None,
) -> bool:
    row = connection.execute(
        "SELECT attempts, max_attempts FROM processing_jobs WHERE id = ? AND status = 'running'",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Running processing job {job_id} was not found")
    attempts, max_attempts = int(row["attempts"]), int(row["max_attempts"])
    if attempts >= max_attempts:
        permanently_fail_processing_job(connection, job_id, error)
        return False
    delay = min(300, 2**attempts) if retry_delay_seconds is None else max(0, int(retry_delay_seconds))
    available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status='retry', available_at=?, claimed_at=NULL, claimed_by=NULL,
                lease_expires_at=NULL, completed_at=NULL, last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='running'
            """,
            (available_at, error.strip()[:2000], job_id),
        )
    if cursor.rowcount != 1:
        raise ValueError(f"Could not retry processing job {job_id}")
    return True


def complete_processing_job(connection: sqlite3.Connection, job_id: int) -> None:
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status='completed', completed_at=CURRENT_TIMESTAMP,
                claimed_at=NULL, claimed_by=NULL, lease_expires_at=NULL,
                last_error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='running'
            """,
            (job_id,),
        )
    if cursor.rowcount != 1:
        raise ValueError(f"Running processing job {job_id} was not found")


def recover_expired_processing_jobs(connection: sqlite3.Connection) -> int:
    with connection:
        cursor = connection.execute(
            """
            UPDATE processing_jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retry' END,
                available_at = CURRENT_TIMESTAMP,
                claimed_at = NULL,
                claimed_by = NULL,
                lease_expires_at = NULL,
                last_error = 'Recovered after worker lease expired',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP
            """
        )
    return int(cursor.rowcount)
