from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import AppSettings
from src.db import (
    collect_observation,
    connect_database,
    initialize_database,
    persist_normalized_message,
)
from src.intelligence.campaigns import analyze_coordination_campaign
from src.intelligence.community import (
    create_community,
    create_organization,
    create_workspace,
    grant_operator_role,
    operator_has_permission,
    register_installation,
    resolve_community_id,
)
from src.intelligence.profiles import refresh_community_profile
from src.intelligence.governance import (
    authorize_api_client,
    create_api_client,
    create_legal_hold,
    release_legal_hold,
)
from src.intelligence.recovery import flush_raw_event_archive, record_dead_letter, replay_dead_letter
from src.models import NormalizedMessage, Observation
from src.pipeline.handlers import claim_processing_job, permanently_fail_processing_job
from src.platform_audit import run_platform_audit
from src.twitch_eventsub import observation_from_eventsub, verify_eventsub_signature


class ProfessionalPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "platform.sqlite3"
        self.connection = connect_database(self.database_path)
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_tenant_installations_and_roles_are_community_scoped(self) -> None:
        organization = create_organization(self.connection, name="Agency", slug="agency")
        workspace = create_workspace(
            self.connection, organization_id=organization, name="Roster", slug="roster"
        )
        community = create_community(
            self.connection, workspace_id=workspace, name="Streamer A", slug="streamer-a"
        )
        register_installation(
            self.connection, community_id=community, platform="twitch",
            external_community_id="12345", display_name="Streamer A",
            scopes=("moderator:manage:banned_users",),
        )
        operator = self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('9','analyst','analyst')"
        ).lastrowid
        grant_operator_role(
            self.connection, operator_id=int(operator), community_id=community, role="analyst"
        )
        self.assertEqual(resolve_community_id(
            self.connection, platform="twitch", external_community_id="12345"
        ), community)
        self.assertTrue(operator_has_permission(
            self.connection, operator_id=int(operator), community_id=community,
            permission="intelligence.read",
        ))
        self.assertFalse(operator_has_permission(
            self.connection, operator_id=int(operator), community_id=1,
            permission="intelligence.read",
        ))

    def test_eventsub_signature_and_normalization(self) -> None:
        secret = "a-professional-secret"
        message_id = "event-1"
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "subscription": {"id": "sub-1", "type": "channel.chat.message"},
            "event": {
                "broadcaster_user_id": "12345", "chatter_user_id": "42",
                "chatter_user_name": "viewer", "message_id": "message-1",
                "message": {"text": "hello community"},
            },
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(
            secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(verify_eventsub_signature(
            secret, message_id=message_id, timestamp=timestamp, body=body, signature=signature
        ))
        observation = observation_from_eventsub(payload, message_id=message_id, community_id=7)
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.event_type, "message.created")
        self.assertEqual(observation.community_id, 7)
        self.assertEqual(observation.actor_platform_user_id, "42")

    def test_raw_archive_dead_letter_and_replay(self) -> None:
        result = collect_observation(self.connection, Observation(
            platform="twitch", event_type="stream.started", external_event_id="stream-1",
            actor_platform_user_id="broadcaster", actor_username="broadcaster",
            container_id="channel", context_id="channel", text="Live",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            raw_payload={"stream_id": "stream-1"},
        ))
        self.assertEqual(flush_raw_event_archive(
            self.connection, self.root / "raw-events"
        ), 1)
        archive_path = self.connection.execute(
            "SELECT archive_path FROM raw_event_archive WHERE observation_id=?",
            (result.observation_id,),
        ).fetchone()[0]
        self.assertTrue(Path(str(archive_path)).is_file())
        job = claim_processing_job(self.connection, stage="analysis", worker_id="test")
        assert job is not None
        permanently_fail_processing_job(self.connection, int(job["id"]), "fixture failure")
        dead_letter = record_dead_letter(
            self.connection, job_id=int(job["id"]), error=RuntimeError("fixture failure")
        )
        replay_job = replay_dead_letter(self.connection, dead_letter)
        self.assertGreater(replay_job, int(job["id"]))

    def test_profiles_and_campaigns_are_multi_axis_and_community_scoped(self) -> None:
        observation_ids: list[int] = []
        user_ids: list[int] = []
        for index, actor in enumerate(("actor-a", "actor-b", "actor-a"), start=1):
            message = NormalizedMessage(
                platform="twitch", platform_user_id=actor, username=actor,
                channel_id="12345", content_raw="Join the giveaway at https://bad.example/deal now",
                sent_at=datetime.now(timezone.utc).isoformat(), platform_message_id=f"msg-{index}",
                guild_or_channel_context="12345", metadata={"community_id": 1},
            )
            collected = collect_observation(self.connection, Observation(
                platform="twitch", event_type="message.created", external_event_id=f"msg-{index}",
                actor_platform_user_id=actor, actor_username=actor, container_id="12345",
                context_id="12345", text=message.content_raw, occurred_at=message.sent_at,
                attributes={"community_id": 1}, community_id=1,
            ))
            persisted = persist_normalized_message(
                self.connection, message, observation_id=collected.observation_id,
                moderation_shadow_mode=True,
            )
            observation_ids.append(int(collected.observation_id))
            user_id = self.connection.execute(
                "SELECT user_id FROM messages WHERE id=?", (persisted.message_id,)
            ).fetchone()[0]
            user_ids.append(int(user_id))
        campaign_id = analyze_coordination_campaign(self.connection, observation_ids[-1])
        self.assertIsNotNone(campaign_id)
        profile = refresh_community_profile(
            self.connection, community_id=1, user_id=user_ids[0]
        )
        self.assertGreaterEqual(profile.engagement, 0)
        self.assertGreaterEqual(profile.risk, 0)
        self.assertNotEqual(profile.trust, profile.engagement)

    def test_platform_audit_reports_no_schema_failure(self) -> None:
        settings = AppSettings.from_env({
            "QBOT_DATABASE_PATH": str(self.database_path),
            "QBOT_BACKUP_DIR": str(self.root / "backups"),
            "QBOT_RAW_ARCHIVE_DIR": str(self.root / "raw"),
            "QBOT_ENABLED_SERVICES": "jobs,analysis",
        })
        result = run_platform_audit(self.connection, settings)
        self.assertNotEqual(result["status"], "fail")

    def test_governance_keys_are_hashed_and_legal_holds_are_audited(self) -> None:
        client_id, plaintext = create_api_client(
            self.connection, organization_id=1, name="automation",
            scopes=("events.write",), rate_limit_per_minute=2,
        )
        stored = str(self.connection.execute(
            "SELECT key_hash FROM api_clients WHERE id=?", (client_id,)
        ).fetchone()[0])
        self.assertNotIn(plaintext, stored)
        self.assertEqual(authorize_api_client(
            self.connection, plaintext_key=plaintext, required_scope="events.write"
        ), client_id)
        self.assertIsNone(authorize_api_client(
            self.connection, plaintext_key=plaintext, required_scope="admin"
        ))
        hold_id = create_legal_hold(
            self.connection, community_id=1, reason="active investigation"
        )
        release_legal_hold(self.connection, hold_id=hold_id)
        actions = [str(row[0]) for row in self.connection.execute(
            "SELECT action_type FROM audit_log WHERE entity_type='legal_hold' ORDER BY id"
        ).fetchall()]
        self.assertEqual(actions, ["legal_hold.created", "legal_hold.released"])


if __name__ == "__main__":
    unittest.main()
