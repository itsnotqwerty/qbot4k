from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.contexts import ActorAttribution, TenantContext
from src.db import (
    collect_observation,
    connect_database,
    enqueue_processing_job,
    initialize_database,
    record_moderation_action,
)
from src.intelligence.announcements import create_announcement
from src.intelligence.community import create_community, register_installation
from src.intelligence.quotas import (
    TenantQuotaExceededError,
    configure_tenant_quota,
    consume_tenant_quota,
)
from src.pipeline.handlers import claim_processing_job, complete_processing_job
from src.models import Observation


class TenantQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "quotas.sqlite3")
        initialize_database(self.connection)
        with self.connection:
            self.connection.execute(
                """INSERT INTO operator_accounts(id,discord_user_id,discord_username,role)
                   VALUES (10,'quota-owner','quota-owner','owner')"""
            )
        self.peer_id = create_community(
            self.connection, workspace_id=1, name="Quota peer", slug="quota-peer",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_quota_is_atomic_audited_and_tenant_isolated(self) -> None:
        tenant = TenantContext(1)
        configure_tenant_quota(
            self.connection, tenant=tenant, actor=ActorAttribution("operator", 10),
            quota_type="ingestion", limit_count=2, window_seconds=60,
        )
        now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(consume_tenant_quota(
            self.connection, tenant=tenant, quota_type="ingestion", now=now,
        ), 1)
        self.assertEqual(consume_tenant_quota(
            self.connection, tenant=tenant, quota_type="ingestion", now=now,
        ), 0)
        with self.assertRaises(TenantQuotaExceededError) as exceeded:
            consume_tenant_quota(
                self.connection, tenant=tenant, quota_type="ingestion", now=now,
            )
        self.assertEqual(exceeded.exception.quota_type, "ingestion")
        self.assertEqual(self.connection.execute(
            """SELECT usage_count FROM tenant_quota_usage
               WHERE community_id=1 AND quota_type='ingestion'"""
        ).fetchone()[0], 2)
        self.assertGreater(consume_tenant_quota(
            self.connection, tenant=TenantContext(self.peer_id),
            quota_type="ingestion", now=now,
        ), 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='tenant.quota_updated' AND actor_id=10"
        ).fetchone()[0], 1)

    def test_job_claims_rotate_between_tenants_before_serving_backlog(self) -> None:
        with self.connection:
            first_observation = int(self.connection.execute(
                """INSERT INTO observations(
                       community_id,platform,event_type,external_event_id,attributes_json,
                       raw_payload_json,occurred_at)
                   VALUES (1,'discord','message.created','fair-a','{}','{}',CURRENT_TIMESTAMP)
                   RETURNING id"""
            ).fetchone()[0])
            peer_observation = int(self.connection.execute(
                """INSERT INTO observations(
                       community_id,platform,event_type,external_event_id,attributes_json,
                       raw_payload_json,occurred_at)
                   VALUES (?,'discord','message.created','fair-b','{}','{}',CURRENT_TIMESTAMP)
                   RETURNING id""", (self.peer_id,),
            ).fetchone()[0])
        enqueue_processing_job(
            self.connection, stage="analysis", job_type="message.created",
            observation_id=first_observation, idempotency_key="fair-a-1",
        )
        enqueue_processing_job(
            self.connection, stage="analysis", job_type="message.created",
            observation_id=first_observation, idempotency_key="fair-a-2",
        )
        enqueue_processing_job(
            self.connection, stage="analysis", job_type="message.created",
            observation_id=peer_observation, idempotency_key="fair-b-1",
        )
        claimed_communities = []
        for index in range(3):
            job = claim_processing_job(
                self.connection, stage="analysis", worker_id=f"fair-{index}",
            )
            self.assertIsNotNone(job)
            assert job is not None
            claimed_communities.append(int(job["community_id"]))
            complete_processing_job(self.connection, int(job["id"]))
        self.assertEqual(claimed_communities, [1, self.peer_id, 1])

    def test_ingestion_jobs_announcements_and_moderation_apply_backpressure(self) -> None:
        tenant = TenantContext(1)
        actor = ActorAttribution("operator", 10)
        for quota_type in ("ingestion", "jobs", "announcements", "moderation"):
            configure_tenant_quota(
                self.connection, tenant=tenant, actor=actor, quota_type=quota_type,
                limit_count=1, window_seconds=60,
            )
        first = collect_observation(self.connection, Observation(
            platform="discord", event_type="message.created", external_event_id="quota-event-1",
            community_id=1, actor_platform_user_id="quota-subject",
            actor_username="quota-subject", occurred_at=datetime.now(timezone.utc).isoformat(),
        ))
        with self.assertRaises(TenantQuotaExceededError):
            collect_observation(self.connection, Observation(
                platform="discord", event_type="message.created", external_event_id="quota-event-2",
                community_id=1, occurred_at=datetime.now(timezone.utc).isoformat(),
            ))
        with self.assertRaises(TenantQuotaExceededError):
            enqueue_processing_job(
                self.connection, stage="analysis", job_type="quota.extra",
                observation_id=first.observation_id, idempotency_key="quota-extra-job",
            )
        account_id = int(self.connection.execute(
            "SELECT id FROM platform_accounts WHERE platform_user_id='quota-subject'"
        ).fetchone()[0])
        record_moderation_action(
            self.connection, platform="discord", message_id=None,
            target_platform_account_id=account_id, action_type="warn", reason="quota test",
            community_id=1,
        )
        with self.assertRaises(TenantQuotaExceededError):
            record_moderation_action(
                self.connection, platform="discord", message_id=None,
                target_platform_account_id=account_id, action_type="warn", reason="quota test",
                community_id=1,
            )
        installation_id = register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="quota-guild", display_name="Quota guild", status="active",
        )
        create_announcement(
            self.connection, community_id=1, platform="discord",
            target_external_id="quota-channel", body="First", created_by_operator_id=10,
            target_installation_id=installation_id,
        )
        with self.assertRaises(TenantQuotaExceededError):
            create_announcement(
                self.connection, community_id=1, platform="discord",
                target_external_id="quota-channel", body="Second", created_by_operator_id=10,
                target_installation_id=installation_id,
            )


if __name__ == "__main__":
    unittest.main()
