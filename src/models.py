from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping


def normalize_message_content(content: str) -> str:
    collapsed = " ".join(content.split())
    return collapsed.casefold()


def coerce_timestamp(raw_value: str | datetime | None) -> str:
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            raw_value = raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc).isoformat()

    if isinstance(raw_value, str) and raw_value.strip():
        normalized = raw_value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class NormalizedMessage:
    platform: str
    platform_user_id: str
    username: str
    channel_id: str
    content_raw: str
    sent_at: str
    platform_message_id: str | None = None
    guild_or_channel_context: str | None = None
    content_normalized: str = field(init=False)
    role_names: tuple[str, ...] = ()
    is_moderator: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_normalized", normalize_message_content(self.content_raw))


@dataclass(frozen=True)
class IngestionResult:
    status: str
    platform: str
    platform_account_id: int | None = None
    message_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ConnectorHealth:
    name: str
    status: str
    details: Mapping[str, object] = field(default_factory=dict)