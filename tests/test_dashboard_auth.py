from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest import mock

from src.dashboard.auth import (
    DashboardSession,
    DiscordIdentity,
    build_discord_install_url,
    build_twitch_oauth_url,
    build_session,
    create_session_cookie,
    create_discord_install_state,
    create_twitch_install_state,
    determine_operator_role,
    exchange_discord_code_for_token,
    parse_session_cookie,
    parse_discord_install_state,
    parse_twitch_install_state,
)
from src.twitch_auth import exchange_twitch_code_for_tokens


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class DashboardAuthTests(unittest.TestCase):
    def test_dashboard_session_round_trips_active_community(self) -> None:
        session = build_session("9", "sam", "owner", community_id=42)

        parsed = parse_session_cookie(
            "session-secret", create_session_cookie("session-secret", session)
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.community_id, 42)

    def test_legacy_dashboard_session_without_community_still_parses(self) -> None:
        legacy = DashboardSession(
            user_id="9", username="sam", role="admin",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )

        parsed = parse_session_cookie(
            "session-secret", create_session_cookie("session-secret", legacy)
        )

        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.community_id)

    def test_discord_install_state_is_tenant_bound_expiring_and_tamper_evident(self) -> None:
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        encoded = create_discord_install_state(
            "session-secret", operator_id="operator-1", community_id=42,
            guild_id="guild-9", now=now,
        )

        state = parse_discord_install_state("session-secret", encoded, now=now)

        self.assertIsNotNone(state)
        self.assertEqual(state.operator_id, "operator-1")
        self.assertEqual(state.community_id, 42)
        self.assertEqual(state.guild_id, "guild-9")
        self.assertIsNone(
            parse_discord_install_state(
                "session-secret", encoded + "tampered", now=now
            )
        )
        self.assertIsNone(
            parse_discord_install_state(
                "session-secret", encoded, now=now + timedelta(minutes=16)
            )
        )

    def test_discord_install_url_binds_guild_and_avoids_administrator_permission(self) -> None:
        url = build_discord_install_url(
            client_id="client-id", redirect_uri="https://example.test/install/discord/callback",
            state="signed-state", guild_id="guild-9",
        )

        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["client_id"], ["client-id"])
        self.assertEqual(set(params["scope"][0].split()), {"bot", "applications.commands"})
        self.assertEqual(params["guild_id"], ["guild-9"])
        self.assertEqual(params["disable_guild_select"], ["true"])
        self.assertEqual(params["state"], ["signed-state"])
        self.assertEqual(int(params["permissions"][0]) & 0x8, 0)

    def test_twitch_install_state_is_tenant_bound_scoped_and_expiring(self) -> None:
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        encoded = create_twitch_install_state(
            "session-secret", operator_id="operator-1", community_id=42,
            broadcaster_login="Streamer_A",
            scopes=("moderator:read:followers", "channel:read:subscriptions"),
            now=now,
        )
        state = parse_twitch_install_state("session-secret", encoded, now=now)
        self.assertIsNotNone(state)
        self.assertEqual((state.operator_id, state.community_id), ("operator-1", 42))
        self.assertEqual(state.broadcaster_login, "streamer_a")
        self.assertEqual(set(state.scopes), {
            "moderator:read:followers", "channel:read:subscriptions",
        })
        self.assertIsNone(parse_twitch_install_state("session-secret", encoded + "x", now=now))
        self.assertIsNone(parse_twitch_install_state(
            "session-secret", encoded, now=now + timedelta(minutes=21)
        ))

    def test_twitch_oauth_url_uses_reviewed_scopes(self) -> None:
        url = build_twitch_oauth_url(
            "client-id", "https://example.test/integrations/twitch/callback", "signed-state",
            ("moderator:read:followers", "channel:read:subscriptions"),
        )
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["client_id"], ["client-id"])
        self.assertEqual(params["state"], ["signed-state"])
        self.assertEqual(set(params["scope"][0].split()), {
            "moderator:read:followers", "channel:read:subscriptions",
        })
        with self.assertRaises(ValueError):
            build_twitch_oauth_url("client-id", "https://example.test/callback", "state", ("user:edit",))

    def test_exchange_twitch_code_returns_rotatable_grant(self) -> None:
        captured_request = None

        def _fake_urlopen(request, timeout=15):
            nonlocal captured_request
            captured_request = request
            return _FakeResponse({
                "access_token": "access-token", "refresh_token": "refresh-token",
                "scope": ["moderator:read:followers"],
            })

        with mock.patch("src.twitch_auth.urlopen", side_effect=_fake_urlopen):
            grant = exchange_twitch_code_for_tokens(
                "client-id", "client-secret", "https://example.test/callback", "oauth-code"
            )
        self.assertEqual((grant.access_token, grant.refresh_token), ("access-token", "refresh-token"))
        self.assertEqual(grant.scopes, ("moderator:read:followers",))
        self.assertEqual(captured_request.full_url, "https://id.twitch.tv/oauth2/token")
        self.assertIn("grant_type=authorization_code", captured_request.data.decode("utf-8"))

    def test_exchange_discord_code_for_token_posts_form_data(self) -> None:
        captured_request = None

        def _fake_urlopen(request, timeout=15):
            nonlocal captured_request
            captured_request = request
            return _FakeResponse({"access_token": "access-token-123"})

        with mock.patch("src.dashboard.auth.urlopen", side_effect=_fake_urlopen):
            token = exchange_discord_code_for_token(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/oauth/discord/callback",
                code="oauth-code",
            )

        self.assertEqual(token, "access-token-123")
        self.assertIsNotNone(captured_request)
        self.assertEqual(captured_request.method, "POST")
        self.assertEqual(captured_request.full_url, "https://discord.com/api/oauth2/token")
        self.assertEqual(captured_request.get_header("Accept"), "application/json")
        self.assertEqual(captured_request.get_header("User-agent"), "qbot4k/1.0")
        body = captured_request.data.decode("utf-8")
        self.assertIn("client_id=client-id", body)
        self.assertIn("client_secret=client-secret", body)
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("code=oauth-code", body)
        self.assertIn(
            "redirect_uri=https%3A%2F%2Fexample.test%2Foauth%2Fdiscord%2Fcallback",
            body,
        )

    def test_determine_operator_role_denies_without_allowlist(self) -> None:
        identity = DiscordIdentity(
            user_id="123",
            username="sam",
            guild_ids=("guild-1",),
            permissions={"guild-1": "0"},
        )

        role = determine_operator_role(identity, ())

        self.assertIsNone(role)


if __name__ == "__main__":
    unittest.main()
