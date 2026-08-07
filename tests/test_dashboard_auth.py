from __future__ import annotations

import json
import unittest
from unittest import mock

from src.dashboard.auth import (
    DiscordIdentity,
    determine_operator_role,
    exchange_discord_code_for_token,
)


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
