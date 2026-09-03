from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Observation, coerce_timestamp
from .contexts import TenantContext
from .twitch_auth import TwitchTokenManager


EVENT_TYPE_MAP = {
    "channel.chat.message": "message.created",
    "channel.chat.message_delete": "message.deleted",
    "channel.ban": "moderation.ban_added",
    "channel.unban": "moderation.ban_removed",
    "channel.moderate": "moderation.action",
    "stream.online": "stream.started",
    "stream.offline": "stream.ended",
    "channel.update": "stream.updated",
    "channel.follow": "channel.followed",
    "channel.subscribe": "channel.subscribed",
    "channel.subscription.gift": "channel.subscription_gifted",
    "channel.cheer": "channel.cheered",
    "channel.raid": "channel.raided",
    "channel.channel_points_custom_reward_redemption.add": "channel.reward_redeemed",
    "channel.warning.send": "channel.warning",
    "channel.warning.acknowledge": "channel.warning",
    "channel.suspicious_user.message": "channel.suspicious_user",
    "channel.suspicious_user.update": "channel.suspicious_user",
    "channel.shield_mode.begin": "channel.shield_mode",
    "channel.shield_mode.end": "channel.shield_mode",
    "channel.shared_chat.begin": "channel.shared_chat",
    "channel.shared_chat.update": "channel.shared_chat",
    "channel.shared_chat.end": "channel.shared_chat",
    "channel.ad_break.begin": "channel.ad_break",
    "channel.charity_campaign.donate": "channel.charity_donation",
}


def verify_eventsub_signature(
    secret: str,
    *,
    message_id: str,
    timestamp: str,
    body: bytes,
    signature: str,
    now: datetime | None = None,
    max_age_seconds: int = 600,
) -> bool:
    if not secret or not message_id or not timestamp or not signature:
        return False
    try:
        received = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    reference = now or datetime.now(timezone.utc)
    if abs((reference - received.astimezone(timezone.utc)).total_seconds()) > max_age_seconds:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), message_id.encode("utf-8") + timestamp.encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def observation_from_eventsub(
    payload: Mapping[str, object], *, message_id: str, community_id: int
) -> Observation | None:
    subscription = payload.get("subscription")
    event = payload.get("event")
    if not isinstance(subscription, Mapping) or not isinstance(event, Mapping):
        return None
    subscription_type = str(subscription.get("type") or "").strip()
    event_type = EVENT_TYPE_MAP.get(subscription_type)
    if event_type is None:
        return None
    broadcaster_id = str(
        event.get("broadcaster_user_id") or event.get("to_broadcaster_user_id")
        or event.get("from_broadcaster_user_id") or ""
    ).strip()
    actor_id = str(
        event.get("chatter_user_id") or event.get("user_id") or event.get("moderator_user_id")
        or event.get("from_broadcaster_user_id") or ""
    ).strip() or None
    actor_name = str(
        event.get("chatter_user_name") or event.get("user_name") or event.get("moderator_user_name")
        or event.get("from_broadcaster_user_name") or actor_id or ""
    ).strip() or None
    target_id = str(event.get("target_user_id") or event.get("to_broadcaster_user_id") or "").strip() or None
    message = event.get("message")
    text = str(message.get("text") or "") if isinstance(message, Mapping) else ""
    text = text or str(event.get("reason") or event.get("title") or "")
    event_id = str(event.get("message_id") or event.get("id") or message_id).strip()
    occurred_at = event.get("sent_at") or event.get("started_at") or event.get("ended_at")
    return Observation(
        platform="twitch", event_type=event_type, external_event_id=event_id,
        actor_platform_user_id=actor_id, actor_username=actor_name,
        target_platform_user_id=target_id, container_id=broadcaster_id or None,
        context_id=broadcaster_id or None, text=text or None,
        occurred_at=coerce_timestamp(str(occurred_at) if occurred_at else None),
        attributes={"eventsub_type": subscription_type, **dict(event)},
        raw_payload=dict(payload),
        community_id=TenantContext.require(community_id).community_id,
    )


def record_subscription(
    connection: sqlite3.Connection, *, community_id: int, subscription: Mapping[str, object]
) -> None:
    transport = subscription.get("transport")
    transport_map = transport if isinstance(transport, Mapping) else {}
    with connection:
        connection.execute(
            """INSERT INTO twitch_eventsub_subscriptions(
                   subscription_id,community_id,subscription_type,subscription_version,
                   condition_json,transport_method,status,callback_url,cost
               ) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(subscription_id) DO UPDATE SET status=excluded.status,
                   condition_json=excluded.condition_json,callback_url=excluded.callback_url,
                   cost=excluded.cost,updated_at=CURRENT_TIMESTAMP""",
            (str(subscription.get("id") or ""), int(community_id),
             str(subscription.get("type") or "unknown"), str(subscription.get("version") or "1"),
             json.dumps(subscription.get("condition") or {}, sort_keys=True),
             str(transport_map.get("method") or "webhook"), str(subscription.get("status") or "unknown"),
             str(transport_map.get("callback") or "") or None, int(subscription.get("cost") or 0)),
        )


def mark_subscription_event(connection: sqlite3.Connection, subscription_id: str, status: str | None = None) -> None:
    with connection:
        connection.execute(
            """UPDATE twitch_eventsub_subscriptions SET last_event_at=CURRENT_TIMESTAMP,
                   status=COALESCE(?,status),updated_at=CURRENT_TIMESTAMP WHERE subscription_id=?""",
            (status, subscription_id),
        )


class TwitchEventSubControlPlane:
    """Reconcile desired webhook subscriptions with Twitch's authoritative inventory."""

    def __init__(
        self,
        *,
        token_manager: TwitchTokenManager,
        callback_url: str,
        secret: str,
        community_id: int,
    ) -> None:
        if not callback_url.startswith("https://"):
            raise ValueError("EventSub callback URL must use HTTPS")
        if len(secret) < 16:
            raise ValueError("EventSub secret must be at least 16 characters")
        self.token_manager = token_manager
        self.callback_url = callback_url
        self.secret = secret
        self.community_id = TenantContext.require(community_id).community_id

    def reconcile(
        self,
        connection: sqlite3.Connection,
        desired: list[Mapping[str, object]],
    ) -> dict[str, int]:
        validation = self.token_manager.validate_token()
        existing = self._list_subscriptions(validation.access_token, validation.client_id)
        existing_keys = {
            self._subscription_key(item): item
            for item in existing
            if isinstance(item, Mapping)
        }
        created = 0
        for subscription in existing:
            if isinstance(subscription, Mapping) and subscription.get("id"):
                record_subscription(
                    connection, community_id=self.community_id, subscription=subscription
                )
        for definition in desired:
            normalized = {
                "type": str(definition.get("type") or "").strip(),
                "version": str(definition.get("version") or "1").strip(),
                "condition": dict(definition.get("condition") or {}),
            }
            if not normalized["type"] or not normalized["condition"]:
                raise ValueError("desired EventSub subscriptions require type and condition")
            if self._subscription_key(normalized) in existing_keys:
                continue
            created_subscription = self._create_subscription(
                validation.access_token, validation.client_id, normalized
            )
            record_subscription(
                connection, community_id=self.community_id, subscription=created_subscription
            )
            created += 1
        return {"existing": len(existing), "created": created, "desired": len(desired)}

    @staticmethod
    def _subscription_key(subscription: Mapping[str, object]) -> str:
        condition = subscription.get("condition")
        return json.dumps(
            [str(subscription.get("type") or ""), str(subscription.get("version") or "1"),
             condition if isinstance(condition, Mapping) else {}],
            sort_keys=True, separators=(",", ":"),
        )

    def _list_subscriptions(self, access_token: str, client_id: str) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        cursor: str | None = None
        for _ in range(20):
            url = "https://api.twitch.tv/helix/eventsub/subscriptions"
            if cursor:
                url += "?" + urlencode({"after": cursor})
            payload = self._request(url, "GET", access_token, client_id)
            data = payload.get("data")
            if isinstance(data, list):
                items.extend(item for item in data if isinstance(item, Mapping))
            pagination = payload.get("pagination")
            cursor = str(pagination.get("cursor") or "") if isinstance(pagination, Mapping) else ""
            if not cursor:
                break
        return items

    def _create_subscription(
        self, access_token: str, client_id: str, definition: Mapping[str, object]
    ) -> Mapping[str, object]:
        body = {
            **definition,
            "transport": {"method": "webhook", "callback": self.callback_url, "secret": self.secret},
        }
        payload = self._request(
            "https://api.twitch.tv/helix/eventsub/subscriptions", "POST",
            access_token, client_id, body,
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
            raise RuntimeError("Twitch EventSub create response omitted subscription data")
        return data[0]

    @staticmethod
    def _request(
        url: str,
        method: str,
        access_token: str,
        client_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        request = Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id,
                     "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                parsed = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Twitch EventSub request failed: HTTP {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Twitch EventSub request unavailable: {exc.reason}") from exc
        if not isinstance(parsed, Mapping):
            raise RuntimeError("Twitch EventSub response was not an object")
        return parsed
