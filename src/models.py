from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping
import json
import sqlite3

from .contexts import TenantContext


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

def observation_from_message(message: NormalizedMessage) -> Observation:
    tenant = TenantContext.require(
        message.metadata.get("community_id"),
        installation_id=message.metadata.get("installation_id"),
    )
    return Observation(
        platform=message.platform,
        event_type="message.created",
        external_event_id=message.platform_message_id,
        actor_platform_user_id=message.platform_user_id,
        actor_username=message.username,
        container_id=message.channel_id,
        context_id=message.guild_or_channel_context,
        text=message.content_raw,
        occurred_at=message.sent_at,
        attributes={
            **dict(message.metadata),
            "role_names": list(message.role_names),
            "is_moderator": message.is_moderator,
        },
        community_id=tenant.community_id,
        installation_id=tenant.installation_id,
        raw_payload=dict(message.metadata),
    )

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
class Observation:
    platform: str
    event_type: str
    occurred_at: str
    community_id: int
    installation_id: int | None = None

    external_event_id: str | None = None
    actor_platform_user_id: str | None = None
    actor_username: str | None = None
    target_platform_user_id: str | None = None
    container_id: str | None = None
    context_id: str | None = None
    text: str | None = None

    attributes: Mapping[str, object] = field(default_factory=dict)
    raw_payload: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

@dataclass(frozen=True)
class ObservationResult:
    status: str
    observation_id: int | None
    actor_platform_account_id: int | None
    target_platform_account_id: int | None = None


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
    

@dataclass(frozen=True)
class CollectedObservation:
    observation_id: int
    status: str
    analysis_job_id: int | None

@dataclass(frozen=True)
class CollectionResult:
    status: str
    platform: str
    observation_id: int | None = None
    analysis_job_id: int | None = None
    reason: str | None = None

@dataclass(frozen=True)
class ProcessingJob:
    id: int
    community_id: int
    stage: str
    job_type: str
    observation_id: int | None
    payload: Mapping[str, object]
    attempts: int
    max_attempts: int

    @classmethod
    def from_row(
        cls,
        row: sqlite3.Row,
    ) -> "ProcessingJob":
        payload = json.loads(str(row["payload_json"]))

        if not isinstance(payload, dict):
            raise ValueError(
                "Processing job payload must be a JSON object"
            )

        return cls(
            id=int(row["id"]),
            community_id=TenantContext.require(row["community_id"]).community_id,
            stage=str(row["stage"]),
            job_type=str(row["job_type"]),
            observation_id=(
                int(row["observation_id"])
                if row["observation_id"] is not None
                else None
            ),
            payload=payload,
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )
