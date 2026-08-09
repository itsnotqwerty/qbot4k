from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from .message_analysis import AnalysisJob


class PermanentActionError(Exception):
    pass


class DiscordMessageSender(Protocol):
    def send_message(
        self,
        channel_id: str,
        payload: dict[str, object],
    ) -> None:
        ...


ActionHandler = Callable[
    [sqlite3.Connection, AnalysisJob],
    None,
]


class ActionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ActionHandler] = {}

    def register(
        self,
        job_type: str,
        handler: ActionHandler,
    ) -> None:
        normalized = job_type.strip().casefold()

        if not normalized:
            raise ValueError("job_type must not be empty")

        if normalized in self._handlers:
            raise ValueError(
                f"Action handler already registered: {normalized}"
            )

        self._handlers[normalized] = handler

    def dispatch(
        self,
        connection: sqlite3.Connection,
        job: AnalysisJob,
    ) -> None:
        handler = self._handlers.get(
            job.job_type.strip().casefold()
        )

        if handler is None:
            raise PermanentActionError(
                f"No action handler registered for "
                f"{job.job_type!r}"
            )

        handler(connection, job)


class DiscordMessageActionHandler:
    def __init__(
        self,
        sender: DiscordMessageSender,
    ) -> None:
        self.sender = sender

    def __call__(
        self,
        connection: sqlite3.Connection,
        job: AnalysisJob,
    ) -> None:
        channel_id = str(
            job.payload.get("channel_id") or ""
        ).strip()

        rendered_reply = job.payload.get(
            "rendered_reply"
        )

        if not channel_id:
            raise PermanentActionError(
                "Discord message action has no channel_id"
            )

        if not isinstance(rendered_reply, dict):
            raise PermanentActionError(
                "Discord rendered_reply must be an object"
            )

        self.sender.send_message(
            channel_id,
            rendered_reply,
        )

class TwitchMessageSender(Protocol):
    def send_message(
        self,
        channel_id: str,
        message: str,
    ) -> None:
        ...


class TwitchMessageActionHandler:
    def __init__(
        self,
        sender: TwitchMessageSender,
    ) -> None:
        self.sender = sender

    def __call__(
        self,
        connection: sqlite3.Connection,
        job: AnalysisJob,
    ) -> None:
        channel_id = str(
            job.payload.get("channel_id") or ""
        ).strip()

        rendered_reply = job.payload.get(
            "rendered_reply"
        )

        if not channel_id:
            raise PermanentActionError(
                "Twitch message action has no channel_id"
            )

        if not isinstance(rendered_reply, str):
            raise PermanentActionError(
                "Twitch rendered_reply must be a string"
            )

        if not rendered_reply.strip():
            raise PermanentActionError(
                "Twitch rendered_reply must not be empty"
            )

        self.sender.send_message(
            channel_id,
            rendered_reply,
        )