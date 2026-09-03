from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    community_id: int
    installation_id: int | None = None

    def __post_init__(self) -> None:
        if int(self.community_id) <= 0:
            raise ValueError("community_id must be a positive integer")
        if self.installation_id is not None and int(self.installation_id) <= 0:
            raise ValueError("installation_id must be a positive integer")

    @classmethod
    def require(
        cls, community_id: object, *, installation_id: object | None = None
    ) -> TenantContext:
        if community_id is None or community_id == "":
            raise ValueError("community_id is required")
        return cls(
            community_id=int(community_id),
            installation_id=(
                int(installation_id) if installation_id not in (None, "") else None
            ),
        )


@dataclass(frozen=True)
class ActorAttribution:
    actor_type: str
    actor_id: int | None = None

    def __post_init__(self) -> None:
        normalized_type = self.actor_type.strip().casefold()
        if normalized_type not in {"system", "operator", "provider"}:
            raise ValueError("actor_type must be system, operator, or provider")
        if normalized_type == "operator" and self.actor_id is None:
            raise ValueError("operator actor attribution requires actor_id")
        if self.actor_id is not None and int(self.actor_id) <= 0:
            raise ValueError("actor_id must be a positive integer")
        object.__setattr__(self, "actor_type", normalized_type)
