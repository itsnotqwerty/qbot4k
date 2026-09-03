from __future__ import annotations

import logging
import threading
import time

from ..db import connect_database, record_operational_metric
from ..db_protocol import DatabaseTarget
from .handlers import (
    claim_processing_job,
    complete_processing_job,
    permanently_fail_processing_job,
    retry_processing_job,
)
from .analysis import AnalysisRegistry
from .message_analysis import (
    AnalysisJob,
    PermanentAnalysisError,
)
from .actions import (
    ActionRegistry,
    PermanentActionError,
)
from ..intelligence.recovery import record_dead_letter

class AnalysisWorker:
    def __init__(
        self,
        database_path: DatabaseTarget,
        registry: AnalysisRegistry,
        *,
        worker_id: str = "analysis-1",
        poll_interval: float = 0.5,
    ) -> None:
        self.database_path = database_path
        self.registry = registry
        self.worker_id = worker_id
        self.poll_interval = poll_interval

        self._stop_event = threading.Event()
        self._logger = logging.getLogger(
            "qbot4k.pipeline.analysis"
        )
        self._last_status = "idle"

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        self._stop_event.clear()
        self._last_status = "ready"

        while not self._stop_event.is_set():
            try:
                processed = self.process_next_job()

                if not processed:
                    self._stop_event.wait(
                        self.poll_interval
                    )
            except Exception:
                self._last_status = "degraded"
                self._logger.exception(
                    "Analysis worker loop failed"
                )
                self._stop_event.wait(1)

        self._last_status = "stopped"

    def process_next_job(self) -> bool:
        connection = connect_database(
            self.database_path
        )

        try:
            row = claim_processing_job(
                connection,
                stage="analysis",
                worker_id=self.worker_id,
            )

            if row is None:
                self._last_status = "ready"
                return False

            job = AnalysisJob.from_row(row)
            started_at = time.perf_counter()
            outcome = "completed"

            try:
                self.registry.dispatch(
                    connection,
                    job,
                )
            except PermanentAnalysisError as exc:
                outcome = "permanent_failure"
                permanently_fail_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                record_dead_letter(connection, job_id=job.id, error=exc, payload=job.payload)
                self._last_status = "degraded"
                self._logger.error(
                    "Analysis job permanently failed "
                    "job_id=%s type=%s error=%s",
                    job.id,
                    job.job_type,
                    exc,
                )
            except Exception as exc:
                outcome = "retry"
                retry_scheduled = retry_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                if not retry_scheduled:
                    record_dead_letter(connection, job_id=job.id, error=exc, payload=job.payload)
                self._last_status = "degraded"
                self._logger.exception(
                    "Analysis job failed "
                    "job_id=%s type=%s retry=%s",
                    job.id,
                    job.job_type,
                    retry_scheduled,
                )
            else:
                complete_processing_job(
                    connection,
                    job.id,
                )
                self._last_status = "ready"
            finally:
                record_operational_metric(connection, "worker.jobs", 1.0,
                    dimension_key=f"analysis:{outcome}")
                record_operational_metric(connection, "worker.latency_ms",
                    (time.perf_counter() - started_at) * 1000.0, dimension_key="analysis")

            return True
        finally:
            connection.close()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "name": "analysis",
            "status": self._last_status,
            "worker_id": self.worker_id,
        }

class DiscordWorker:
    def __init__(
        self,
        database_path: DatabaseTarget,
        registry: ActionRegistry,
        *,
        worker_id: str = "discord-1",
        poll_interval: float = 0.5,
    ) -> None:
        self.database_path = database_path
        self.registry = registry
        self.worker_id = worker_id
        self.poll_interval = poll_interval

        self._stop_event = threading.Event()
        self._logger = logging.getLogger(
            "qbot4k.pipeline.discord"
        )
        self._last_status = "idle"

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        self._stop_event.clear()
        self._last_status = "ready"

        while not self._stop_event.is_set():
            try:
                processed = self.process_next_job()

                if not processed:
                    self._stop_event.wait(
                        self.poll_interval
                    )
            except Exception:
                self._last_status = "degraded"
                self._logger.exception(
                    "Action worker loop failed"
                )
                self._stop_event.wait(1)

        self._last_status = "stopped"

    def process_next_job(self) -> bool:
        connection = connect_database(
            self.database_path
        )

        try:
            row = claim_processing_job(
                connection,
                stage="action",
                worker_id=self.worker_id,
            )

            if row is None:
                self._last_status = "ready"
                return False

            job = AnalysisJob.from_row(row)
            started_at = time.perf_counter()
            outcome = "completed"

            try:
                self.registry.dispatch(
                    connection,
                    job,
                )
            except PermanentActionError as exc:
                outcome = "permanent_failure"
                permanently_fail_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                record_dead_letter(connection, job_id=job.id, error=exc, payload=job.payload)
                _fail_moderation_actions_for_job(connection, job)
                self._last_status = "degraded"
                self._logger.error(
                    "Action permanently failed "
                    "job_id=%s type=%s error=%s",
                    job.id,
                    job.job_type,
                    exc,
                )
            except Exception as exc:
                outcome = "retry"
                retrying = retry_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                if not retrying:
                    record_dead_letter(connection, job_id=job.id, error=exc, payload=job.payload)
                    _fail_moderation_actions_for_job(connection, job)
                self._last_status = "degraded"
                self._logger.exception(
                    "Action failed "
                    "job_id=%s type=%s retry=%s",
                    job.id,
                    job.job_type,
                    retrying,
                )
            else:
                complete_processing_job(
                    connection,
                    job.id,
                )
                self._last_status = "ready"
            finally:
                record_operational_metric(connection, "worker.jobs", 1.0,
                    dimension_key=f"action:{outcome}")
                record_operational_metric(connection, "worker.latency_ms",
                    (time.perf_counter() - started_at) * 1000.0, dimension_key="action")

            return True
        finally:
            connection.close()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "name": "discord",
            "status": self._last_status,
            "worker_id": self.worker_id,
        }


def _fail_moderation_actions_for_job(
    connection,
    job: AnalysisJob,
) -> None:
    if not job.job_type.strip().casefold().endswith(".moderation.execute"):
        return
    try:
        message_id = int(job.payload.get("message_id"))
    except (TypeError, ValueError):
        return
    with connection:
        connection.execute(
            """
            UPDATE moderation_actions
            SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
                error_message = ?
            WHERE message_id = ? AND status = 'pending'
            """,
            ("Associated action job exhausted retries", message_id),
        )
