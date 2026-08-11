from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from src.db import connect_database, get_observation
from src.models import CollectionResult, IngestionResult
from src.pipeline.actions import (
    ActionRegistry,
    DiscordMessageActionHandler,
    ModerationActionHandler,
)
from src.pipeline.analysis import AnalysisRegistry
from src.pipeline.message_analysis import MESSAGE_ANALYSIS_JOB_TYPE, MessageAnalysisPipeline
from src.pipeline.workers import AnalysisWorker


def drain_analysis(database_path: Path) -> None:
    registry = AnalysisRegistry()
    pipeline = MessageAnalysisPipeline(database_path)
    registry.register(MESSAGE_ANALYSIS_JOB_TYPE, pipeline.analyze_message_created)
    worker = AnalysisWorker(database_path, registry, poll_interval=0)
    while worker.process_next_job():
        pass


def ingest_and_analyze(
    connector: object,
    payload: Mapping[str, object],
    *,
    reply_sink: Callable[[str], None] | None = None,
) -> IngestionResult | CollectionResult:
    result = connector.ingest_message(payload)

    if result.status == "duplicate":
        platform = "discord" if connector.__class__.__name__ == "DiscordConnector" else "twitch"
        external_id = payload.get("id") if platform == "discord" else payload.get("message_id")
        connection = connect_database(Path(connector.database_path))
        try:
            row = connection.execute(
                "SELECT id, platform_account_id FROM messages WHERE platform = ? AND platform_message_id = ?",
                (platform, str(external_id)),
            ).fetchone()
        finally:
            connection.close()
        return IngestionResult(
            status="duplicate",
            platform=platform,
            message_id=int(row[0]) if row is not None else None,
            platform_account_id=int(row[1]) if row is not None else None,
        )
    if result.status != "persisted" or result.observation_id is None:
        return result

    database_path = Path(connector.database_path)
    drain_analysis(database_path)

    connection = connect_database(database_path)
    try:
        row = connection.execute(
            """
            SELECT messages.id, messages.platform_account_id, messages.platform
            FROM messages
            WHERE messages.observation_id = ?
            """,
            (result.observation_id,),
        ).fetchone()
        if row is None:
            return result

        projected = IngestionResult(
            status="persisted",
            platform=str(row[2]),
            platform_account_id=int(row[1]),
            message_id=int(row[0]),
        )

        observation = get_observation(connection, result.observation_id)
        if observation is None:
            return projected

        if projected.platform == "discord":
            from src.discord import normalize_discord_message

            normalized = normalize_discord_message(payload)
            connector._execute_pending_moderation_actions(connection, normalized, projected)
            if connector._bot_token:
                registry = ActionRegistry()
                registry.register("discord.message.send", DiscordMessageActionHandler(connector))
                registry.register(
                    "discord.moderation.execute",
                    ModerationActionHandler(connector),
                )
                from src.pipeline.workers import DiscordWorker

                worker = DiscordWorker(database_path, registry, poll_interval=0)
                while worker.process_next_job():
                    pass
        elif projected.platform == "twitch":
            from src.twitch import normalize_twitch_message

            normalized = normalize_twitch_message(payload)
            connector._process_join_command(connection, normalized, projected)
            if reply_sink is not None:
                reply = connector._dispatch_registered_command(connection, normalized)
                if reply is not None:
                    from src.commands import render_command_reply

                    reply_sink(str(render_command_reply(reply, "twitch")))

        return projected
    finally:
        connection.close()
