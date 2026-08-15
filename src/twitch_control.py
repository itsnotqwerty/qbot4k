from __future__ import annotations

import json
import sqlite3
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .twitch_auth import TwitchTokenManager


CHAT_SETTING_FIELDS = {
    "emote_mode", "follower_mode", "follower_mode_duration", "non_moderator_chat_delay",
    "non_moderator_chat_delay_duration", "slow_mode", "slow_mode_wait_time",
    "subscriber_mode", "unique_chat_mode",
}


class TwitchControlPlane:
    def __init__(self, token_manager: TwitchTokenManager) -> None:
        self.token_manager = token_manager

    def set_shield_mode(
        self,
        connection: sqlite3.Connection,
        *,
        community_id: int,
        broadcaster: str,
        active: bool,
        operator_id: int,
    ) -> dict[str, object]:
        return self._execute(
            connection, community_id=community_id, broadcaster=broadcaster,
            operator_id=operator_id, control_type="shield_mode",
            endpoint="moderation/shield_mode", method="PUT", payload={"is_active": bool(active)},
        )

    def update_chat_settings(
        self,
        connection: sqlite3.Connection,
        *,
        community_id: int,
        broadcaster: str,
        settings: Mapping[str, object],
        operator_id: int,
    ) -> dict[str, object]:
        payload = {str(key): value for key, value in settings.items() if str(key) in CHAT_SETTING_FIELDS}
        if not payload:
            raise ValueError("no supported Twitch chat settings were supplied")
        for key in ("follower_mode_duration", "slow_mode_wait_time", "non_moderator_chat_delay_duration"):
            if key in payload:
                payload[key] = max(0, min(int(payload[key]), 129_600 if key == "follower_mode_duration" else 120))
        return self._execute(
            connection, community_id=community_id, broadcaster=broadcaster,
            operator_id=operator_id, control_type="chat_settings",
            endpoint="chat/settings", method="PATCH", payload=payload,
        )

    def get_chat_settings(self, broadcaster: str) -> Mapping[str, object]:
        validation = self.token_manager.validate_token()
        broadcaster_id = self._resolve_broadcaster_id(
            broadcaster, validation.access_token, validation.client_id
        )
        query = urlencode({"broadcaster_id": broadcaster_id, "moderator_id": validation.user_id})
        _, response = self._request(
            f"https://api.twitch.tv/helix/chat/settings?{query}", "GET",
            validation.access_token, validation.client_id,
        )
        return response

    def _execute(
        self,
        connection: sqlite3.Connection,
        *,
        community_id: int,
        broadcaster: str,
        operator_id: int,
        control_type: str,
        endpoint: str,
        method: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        validation = self.token_manager.validate_token()
        if not validation.user_id:
            raise RuntimeError("Twitch token validation omitted moderator user_id")
        broadcaster_id = self._resolve_broadcaster_id(
            broadcaster, validation.access_token, validation.client_id
        )
        with connection:
            cursor = connection.execute(
                """INSERT INTO twitch_control_actions(
                       community_id,operator_id,broadcaster_id,control_type,requested_json
                   ) VALUES (?,?,?,?,?)""",
                (int(community_id), int(operator_id), broadcaster_id, control_type,
                 json.dumps(dict(payload), sort_keys=True)),
            )
        action_id = int(cursor.lastrowid)
        query = urlencode({"broadcaster_id": broadcaster_id, "moderator_id": validation.user_id})
        try:
            provider_status, response = self._request(
                f"https://api.twitch.tv/helix/{endpoint}?{query}", method,
                validation.access_token, validation.client_id, payload,
            )
        except Exception as exc:
            with connection:
                connection.execute(
                    """UPDATE twitch_control_actions SET status='failed',error_message=? WHERE id=?""",
                    (str(exc)[:1000], action_id),
                )
            raise
        with connection:
            connection.execute(
                """UPDATE twitch_control_actions SET status='confirmed',provider_status=?,
                       provider_response_json=?,confirmed_at=CURRENT_TIMESTAMP,error_message=NULL
                   WHERE id=?""",
                (provider_status, json.dumps(dict(response), sort_keys=True), action_id),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       actor_type,actor_id,action_type,entity_type,entity_id,payload_json
                   ) VALUES ('operator',?,'twitch.control_confirmed','twitch_control_action',?,?)""",
                (int(operator_id), action_id, json.dumps({"control_type": control_type,
                                                         "provider_status": provider_status}, sort_keys=True)),
            )
        return {"action_id": action_id, "status": "confirmed",
                "provider_status": provider_status, "response": dict(response)}

    def _resolve_broadcaster_id(self, broadcaster: str, access_token: str, client_id: str) -> str:
        value = broadcaster.strip().removeprefix("#").casefold()
        if value.isdigit():
            return value
        _, payload = self._request(
            "https://api.twitch.tv/helix/users?" + urlencode({"login": value}),
            "GET", access_token, client_id,
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            raise ValueError(f"Twitch broadcaster {value!r} was not found")
        broadcaster_id = str(rows[0].get("id") or "").strip()
        if not broadcaster_id:
            raise RuntimeError("Twitch users response omitted broadcaster id")
        return broadcaster_id

    @staticmethod
    def _request(
        url: str,
        method: str,
        access_token: str,
        client_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[str, Mapping[str, object]]:
        request = Request(
            url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id,
                     "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw) if raw.strip() else {}
                if not isinstance(parsed, Mapping):
                    parsed = {"data": parsed}
                return str(response.status), parsed
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Twitch control failed: HTTP {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Twitch control unavailable: {exc.reason}") from exc
