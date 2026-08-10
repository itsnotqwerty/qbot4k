from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..commands import (
    CommandContext,
    CommandRegistry,
    build_default_command_registry,
    render_command_reply,
)
from ..db import enqueue_processing_job, get_observation, persist_normalized_message, normalized_message_from_observation
from ..models import NormalizedMessage
from ..intelligence.signals import refresh_user_derived_signals


MESSAGE_ANALYSIS_JOB_TYPE = "analyze.message.created"
MESSAGE_ANALYZER_VERSION = 1


class PermanentAnalysisError(Exception):
    """The analysis cannot succeed without changing its input or code."""


@dataclass(frozen=True)
class AnalysisJob:
    id: int
    stage: str
    job_type: str
    observation_id: int | None
    payload: Mapping[str, object]
    attempts: int
    max_attempts: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AnalysisJob":
        raw_payload = json.loads(str(row["payload_json"] or "{}"))
        if not isinstance(raw_payload, dict):
            raise PermanentAnalysisError("Analysis job payload must be a JSON object")

        return cls(
            id=int(row["id"]),
            stage=str(row["stage"]),
            job_type=str(row["job_type"]),
            observation_id=(
                int(row["observation_id"])
                if row["observation_id"] is not None
                else None
            ),
            payload=raw_payload,
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )


class MessageAnalysisPipeline:
    """Projects message observations and analyzes commands without platform I/O."""

    def __init__(
        self,
        database_path: Path,
        *,
        command_registry: CommandRegistry | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.command_registry = command_registry or build_default_command_registry()

    def analyze_message_created(
        self,
        connection: sqlite3.Connection,
        job: AnalysisJob,
    ) -> None:
        observation_id = _validate_message_job(job)
        observation = get_observation(connection, observation_id)
        if observation is None:
            raise PermanentAnalysisError(
                f"Observation {observation_id} does not exist"
            )

        event_type = str(observation["event_type"])
        if event_type != "message.created":
            raise PermanentAnalysisError(
                f"Expected message.created, received {event_type!r}"
            )

        try:
            message = normalized_message_from_observation(observation)
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentAnalysisError(
                f"Malformed observation {observation_id}: {exc}"
            ) from exc

        # Message projection, command mutations, the recorded command result, and
        # the outbound action job commit together. A failure rolls all of them back.
        with connection:
            _project_message(connection, message, observation_id)
            self._analyze_command(connection, message, observation_id)
            user_row = connection.execute(
                """
                SELECT platform_accounts.user_id
                FROM messages
                INNER JOIN platform_accounts
                    ON platform_accounts.id = messages.platform_account_id
                WHERE messages.observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
            if user_row is not None and user_row[0] is not None:
                refresh_user_derived_signals(connection, int(user_row[0]))

    def _analyze_command(
        self,
        connection: sqlite3.Connection,
        message: NormalizedMessage,
        observation_id: int,
    ) -> None:
        previous = connection.execute(
            """
            SELECT command_name, rendered_payload_json
            FROM command_analysis_results
            WHERE observation_id = ? AND analyzer_version = ?
            """,
            (observation_id, MESSAGE_ANALYZER_VERSION),
        ).fetchone()

        if previous is not None:
            rendered_payload_json = previous["rendered_payload_json"]
            if rendered_payload_json is not None:
                _enqueue_command_action(
                    connection,
                    observation_id=observation_id,
                    platform=message.platform,
                    channel_id=message.channel_id,
                    rendered_payload_json=str(rendered_payload_json),
                )
            return

        context = CommandContext(
            platform=message.platform,
            database_path=self.database_path,
            connection=connection,
            author_platform_user_id=message.platform_user_id,
            author_username=message.username,
            channel_id=message.channel_id,
            guild_id=(
                str(message.metadata["guild_id"])
                if message.metadata.get("guild_id")
                else None
            ),
            message_id=message.platform_message_id,
            content=message.content_raw,
        )
        parsed_command = self.command_registry.parse_command(message.content_raw)
        command_name = parsed_command[0] if parsed_command is not None else ""
        reply = self.command_registry.dispatch(message.content_raw, context)

        rendered_payload_json: str | None = None
        if reply is not None:
            rendered = render_command_reply(reply, message.platform)
            rendered_payload_json = json.dumps(
                rendered,
                sort_keys=True,
                separators=(",", ":"),
            )

        connection.execute(
            """
            INSERT INTO command_analysis_results (
                observation_id,
                analyzer_version,
                command_name,
                matched,
                rendered_payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                MESSAGE_ANALYZER_VERSION,
                command_name or None,
                int(reply is not None),
                rendered_payload_json,
            ),
        )

        if rendered_payload_json is not None:
            _enqueue_command_action(
                connection,
                observation_id=observation_id,
                platform=message.platform,
                channel_id=message.channel_id,
                rendered_payload_json=rendered_payload_json,
            )


def analyze_message_created(
    connection: sqlite3.Connection,
    job: AnalysisJob,
    *,
    database_path: Path,
    command_registry: CommandRegistry | None = None,
) -> None:
    """Convenience entry point for an AnalysisRegistry handler."""
    pipeline = MessageAnalysisPipeline(
        database_path,
        command_registry=command_registry,
    )
    pipeline.analyze_message_created(connection, job)


def _validate_message_job(job: AnalysisJob) -> int:
    if job.stage != "analysis":
        raise PermanentAnalysisError(
            f"Message analyzer received a {job.stage!r} job"
        )
    if job.job_type != MESSAGE_ANALYSIS_JOB_TYPE:
        raise PermanentAnalysisError(
            f"Message analyzer cannot process {job.job_type!r}"
        )
    if job.observation_id is None:
        raise PermanentAnalysisError("Message analysis job has no observation_id")
    return job.observation_id


def _project_message(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
    observation_id: int,
) -> None:
    existing = connection.execute(
        "SELECT id FROM messages WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    if existing is not None:
        return

    result = persist_normalized_message(
        connection,
        message,
        observation_id=observation_id,
    )
    if result.status == "duplicate":
        if message.platform_message_id is not None:
            connection.execute(
                """
                UPDATE messages
                SET observation_id = ?
                WHERE platform = ?
                  AND platform_message_id = ?
                  AND observation_id IS NULL
                """,
                (
                    observation_id,
                    message.platform,
                    message.platform_message_id,
                ),
            )
        return
    if result.status != "persisted":
        raise PermanentAnalysisError(
            f"Unexpected message persistence status {result.status!r}"
        )


def _enqueue_command_action(
    connection: sqlite3.Connection,
    *,
    observation_id: int,
    platform: str,
    channel_id: str,
    rendered_payload_json: str,
) -> None:
    rendered_payload = json.loads(rendered_payload_json)
    enqueue_processing_job(
        connection,
        stage="action",
        job_type=f"{platform}.message.send",
        observation_id=observation_id,
        payload={
            "channel_id": channel_id,
            "rendered_reply": rendered_payload,
        },
        idempotency_key=f"observation:{observation_id}:command-reply:v1",
        priority=50,
    )
