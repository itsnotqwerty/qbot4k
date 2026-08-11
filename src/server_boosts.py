from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import (
    ensure_canonical_user_for_platform_account,
    ensure_platform_account,
    record_server_boost_request,
    reward_server_boost_request,
)
from .models import NormalizedMessage


@dataclass(frozen=True)
class ServerBoostResult:
    command_name: str
    event_type: str
    request_id: int | None


def server_boost_command_name(content: str, interaction_command_name: str = "") -> str | None:
    normalized = content.casefold().strip()
    if normalized:
        first_token = normalized.split(None, 1)[0]
        if first_token in {"/bump", "/boop"}:
            return first_token

    interaction_name = interaction_command_name.casefold().strip().lstrip("/")
    if interaction_name in {"bump", "boop"}:
        return f"/{interaction_name}"
    return None


def detect_server_boost_success(
    content: str,
    interaction_command_name: str = "",
    embed_text: str = "",
) -> str | None:
    normalized = f"{content}\n{embed_text}".casefold()
    success_signals = {
        "/bump": (
            "bump done",
            "bumped successfully",
            "bump successful",
            "server bumped",
            "bump complete",
            "bump completed",
        ),
        "/boop": (
            "boop done",
            "booped successfully",
            "boop successful",
            "server booped",
            "boop complete",
            "boop completed",
        ),
    }
    for command_name, phrases in success_signals.items():
        if any(phrase in normalized for phrase in phrases):
            return command_name

    inferred_command = server_boost_command_name("", interaction_command_name)
    if inferred_command is not None:
        success_keywords = (
            "done",
            "success",
            "successful",
            "completed",
            "complete",
        )
        if any(keyword in normalized for keyword in success_keywords):
            return inferred_command
    return None


def is_server_boost_confirmation(message: NormalizedMessage) -> bool:
    if not bool(message.metadata.get("author_is_bot")):
        return False
    return _success_command(message) is not None


def process_discord_server_boost(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
    *,
    platform_account_id: int | None = None,
) -> ServerBoostResult | None:
    if message.platform != "discord":
        return None

    successful_command = _success_command(message)
    if successful_command is not None:
        request_id = _reward_from_interaction(
            connection,
            message,
            successful_command,
        )
        if request_id is None:
            request_id = reward_server_boost_request(
                connection,
                platform="discord",
                channel_id=message.channel_id,
                command_names=(successful_command,),
            )
        return ServerBoostResult(
            command_name=successful_command,
            event_type="success",
            request_id=request_id,
        )

    command_name = server_boost_command_name(
        message.content_raw,
        str(message.metadata.get("interaction_command_name") or ""),
    )
    if command_name is None:
        return None

    if platform_account_id is None:
        account = connection.execute(
            """
            SELECT id
            FROM platform_accounts
            WHERE platform = 'discord' AND platform_user_id = ?
            """,
            (message.platform_user_id,),
        ).fetchone()
        platform_account_id = int(account[0]) if account is not None else None
    if platform_account_id is None:
        return ServerBoostResult(command_name=command_name, event_type="request", request_id=None)

    request_id = record_server_boost_request(
        connection,
        platform="discord",
        channel_id=message.channel_id,
        requester_platform_account_id=platform_account_id,
        command_name=command_name,
    )
    return ServerBoostResult(
        command_name=command_name,
        event_type="request",
        request_id=request_id,
    )


def _success_command(message: NormalizedMessage) -> str | None:
    return detect_server_boost_success(
        message.content_raw,
        str(message.metadata.get("interaction_command_name") or ""),
        str(message.metadata.get("embed_text") or ""),
    )


def _reward_from_interaction(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
    command_name: str,
) -> int | None:
    interaction_user_id = str(message.metadata.get("interaction_user_id") or "").strip()
    if not interaction_user_id:
        return None

    interaction_username = str(
        message.metadata.get("interaction_username") or f"user_{interaction_user_id}"
    ).strip()
    account_id = ensure_platform_account(
        connection,
        platform="discord",
        platform_user_id=interaction_user_id,
        username=interaction_username,
        guild_or_channel_context=message.guild_or_channel_context or message.channel_id,
    )
    ensure_canonical_user_for_platform_account(
        connection,
        platform_account_id=account_id,
        preferred_display_name=interaction_username,
    )
    record_server_boost_request(
        connection,
        platform="discord",
        channel_id=message.channel_id,
        requester_platform_account_id=account_id,
        command_name=command_name,
    )
    return reward_server_boost_request(
        connection,
        platform="discord",
        channel_id=message.channel_id,
        command_names=(command_name,),
    )
