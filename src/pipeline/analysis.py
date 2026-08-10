from __future__ import annotations

from ..db import get_observation, normalized_message_from_observation
from .message_analysis import persist_normalized_message
from collections.abc import Callable
from ..models import ProcessingJob
from .handlers import claim_processing_job, permanently_fail_processing_job, retry_processing_job, complete_processing_job
from .message_analysis import (
    AnalysisJob,
    PermanentAnalysisError,
)
import sqlite3

AnalysisHandler = Callable[
    [sqlite3.Connection, ProcessingJob], None
]

class UnknownAnalysisJobError(Exception):
    pass

class PermanentJobError(Exception):
    pass

def analyze_message_created(
    connection: sqlite3.Connection,
    job: ProcessingJob,
) -> None:
    if job.stage != "analysis":
        raise PermanentJobError(
            f"Message analyzer received a {job.stage!r} job"
        )

    if job.job_type != "analyze.message.created":
        raise PermanentJobError(
            f"Message analyzer cannot process {job.job_type!r}"
        )

    if job.observation_id is None:
        raise PermanentJobError(
            "Message analysis job has no observation_id"
        )

    observation = get_observation(
        connection,
        job.observation_id,
    )

    if observation is None:
        raise PermanentJobError(
            f"Observation {job.observation_id} does not exist"
        )

    event_type = str(observation["event_type"])

    if event_type != "message.created":
        raise PermanentJobError(
            f"Expected message.created, received {event_type!r}"
        )

    if observation["actor_platform_user_id"] is None:
        raise PermanentJobError(
            f"Observation {job.observation_id} has no actor"
        )

    if observation["container_id"] is None:
        raise PermanentJobError(
            f"Observation {job.observation_id} has no container"
        )

    try:
        normalized = normalized_message_from_observation(
            observation
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentJobError(
            f"Malformed observation {job.observation_id}: {exc}"
        ) from exc

    with connection:
        existing_message = connection.execute(
            """
            SELECT id
            FROM messages
            WHERE observation_id = ?
            """,
            (job.observation_id,),
        ).fetchone()

        if existing_message is not None:
            return

        result = persist_normalized_message(
            connection,
            normalized,
            observation_id=job.observation_id,
        )

        if result.status == "duplicate":
            # The platform message may have been stored before
            # observation_id was introduced. Link it to this
            # observation when possible.
            if normalized.platform_message_id is not None:
                connection.execute(
                    """
                    UPDATE messages
                    SET observation_id = ?
                    WHERE platform = ?
                      AND platform_message_id = ?
                      AND observation_id IS NULL
                    """,
                    (
                        job.observation_id,
                        normalized.platform,
                        normalized.platform_message_id,
                    ),
                )
            return

        if result.status != "persisted":
            raise PermanentJobError(
                "Unexpected message persistence status "
                f"{result.status!r}"
            )

def process_next_analysis_job(
    connection: sqlite3.Connection,
    *,
    registry: AnalysisRegistry,
    worker_id: str,
) -> bool:
    row = claim_processing_job(
        connection,
        stage="analysis",
        worker_id=worker_id,
    )

    if row is None:
        return False

    job = ProcessingJob.from_row(row)

    try:
        registry.dispatch(connection, job)
    except PermanentJobError as exc:
        permanently_fail_processing_job(
            connection,
            job.id,
            str(exc),
        )
    except Exception as exc:
        retry_processing_job(
            connection,
            job.id,
            str(exc),
        )
    else:
        complete_processing_job(
            connection,
            job.id,
        )

    return True


class AnalysisRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, AnalysisHandler] = {}

    def register(
        self,
        job_type: str,
        handler: AnalysisHandler,
    ) -> None:
        normalized = job_type.strip().casefold()

        if not normalized:
            raise ValueError("job_type must not be empty")

        if normalized in self._handlers:
            raise ValueError(
                f"Analysis handler already registered: {normalized}"
            )

        self._handlers[normalized] = handler

    def dispatch(
        self,
        connection: sqlite3.Connection,
        job: AnalysisJob,
    ) -> None:
        job_type = job.job_type.strip().casefold()
        handler = self._handlers.get(job_type)

        if handler is None:
            raise PermanentAnalysisError(
                f"No analysis handler registered for {job_type!r}"
            )

        handler(connection, job)

def build_analysis_registry() -> AnalysisRegistry:
    registry = AnalysisRegistry()

    registry.register(
        "analyze.message.created",
        analyze_message_created,
    )

    return registry
