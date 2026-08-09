from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..db import (
    claim_processing_job,
    complete_processing_job,
    connect_database
)
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

class AnalysisWorker:
    def __init__(
        self,
        database_path: Path,
        registry: AnalysisRegistry,
        *,
        worker_id: str = "analysis-1",
        poll_interval: float = 0.5,
    ) -> None:
        self.database_path = Path(database_path)
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

            try:
                self.registry.dispatch(
                    connection,
                    job,
                )
            except PermanentAnalysisError as exc:
                permanently_fail_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                self._last_status = "degraded"
                self._logger.error(
                    "Analysis job permanently failed "
                    "job_id=%s type=%s error=%s",
                    job.id,
                    job.job_type,
                    exc,
                )
            except Exception as exc:
                retry_scheduled = retry_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
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
        database_path: Path,
        registry: ActionRegistry,
        *,
        worker_id: str = "discord-1",
        poll_interval: float = 0.5,
    ) -> None:
        self.database_path = Path(database_path)
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

            try:
                self.registry.dispatch(
                    connection,
                    job,
                )
            except PermanentActionError as exc:
                permanently_fail_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
                self._last_status = "degraded"
                self._logger.error(
                    "Action permanently failed "
                    "job_id=%s type=%s error=%s",
                    job.id,
                    job.job_type,
                    exc,
                )
            except Exception as exc:
                retrying = retry_processing_job(
                    connection,
                    job.id,
                    str(exc),
                )
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

            return True
        finally:
            connection.close()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "name": "discord",
            "status": self._last_status,
            "worker_id": self.worker_id,
        }