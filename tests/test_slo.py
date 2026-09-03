from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.contexts import TenantContext
from src.db import connect_database, initialize_database, record_operational_metric
from src.intelligence.community import create_community, register_installation
from src.intelligence.slo import collect_tenant_slo_snapshot, list_tenant_slo_samples


class TenantSloTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "slo.sqlite3")
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_snapshot_tracks_all_indicators_and_isolates_tenant_evidence(self) -> None:
        other_community_id = create_community(
            self.connection, workspace_id=1, name="SLO peer", slug="slo-peer",
        )
        with self.connection:
            account_id = int(self.connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username)
                   VALUES ('discord','slo-user','slo-user') RETURNING id"""
            ).fetchone()[0])
            observation_id = int(self.connection.execute(
                """INSERT INTO observations(
                       community_id,platform,event_type,external_event_id,text_raw,
                       attributes_json,raw_payload_json,occurred_at,ingested_at
                   ) VALUES (1,'discord','message.created','slo-event','text','{}','{}',
                             '2026-09-03T11:59:59+00:00','2026-09-03T12:00:00+00:00') RETURNING id"""
            ).fetchone()[0])
            message_id = int(self.connection.execute(
                """INSERT INTO messages(
                       observation_id,community_id,platform,platform_message_id,
                       platform_account_id,channel_id,content_raw,content_normalized,sent_at
                   ) VALUES (?,1,'discord','slo-message',?,'slo-channel','text','text',
                             '2026-09-03T12:00:00+00:00') RETURNING id""",
                (observation_id, account_id),
            ).fetchone()[0])
            self.connection.execute(
                """INSERT INTO intelligence_alerts(
                       community_id,observation_id,alert_type,severity,title,summary,
                       confidence,dedupe_key,created_at)
                   VALUES (1,?,'slo','high','SLO alert','SLO alert',0.9,'slo-alert',
                           '2026-09-03T12:00:05+00:00')""", (observation_id,),
            )
            self.connection.execute(
                """INSERT INTO moderation_actions(
                       community_id,platform,message_id,target_platform_account_id,
                       action_type,actor_type,status,created_at,provider_confirmed_at)
                   VALUES (1,'discord',?,?,'timeout','system','completed',
                           '2026-09-03T12:00:00+00:00','2026-09-03T12:00:20+00:00')""",
                (message_id, account_id),
            )
            self.connection.execute(
                """INSERT INTO member_reports(
                       community_id,subject_platform_account_id,category,summary,severity,created_at)
                   VALUES (1,?,'slo','Old report','high','2026-09-03T11:40:00+00:00')""",
                (account_id,),
            )
            self.connection.execute(
                """INSERT INTO service_reliability_buckets(service_name,bucket_start,is_up,status)
                   VALUES ('web','2026-09-03T11:55:00+00:00',1,'ready')"""
            )
            self.connection.execute(
                """INSERT INTO dead_letter_events(
                       community_id,stage,error_class,error_message,payload_json)
                   VALUES (1,'analysis','RuntimeError','failed','{}')"""
            )
        installation_id = register_installation(
            self.connection, community_id=1, platform="discord",
            external_community_id="slo-guild", display_name="SLO guild", status="active",
        )
        with self.connection:
            self.connection.execute(
                """UPDATE community_installations SET health_status='healthy',
                   last_health_check_at='2026-09-03T12:00:00+00:00' WHERE id=?""",
                (installation_id,),
            )
        record_operational_metric(
            self.connection, "backup.success", 100.0,
            observed_at="2026-09-03T11:00:00+00:00",
        )

        samples = collect_tenant_slo_snapshot(
            self.connection, tenant=TenantContext(1),
            observed_at="2026-09-03T12:00:00+00:00",
        )
        by_name = {sample.metric_name: sample for sample in samples}
        self.assertEqual(len(by_name), 8)
        self.assertAlmostEqual(by_name["webhook_acceptance_ms"].value, 1000.0, delta=1.0)
        self.assertAlmostEqual(by_name["event_to_alert_ms"].value, 5000.0, delta=1.0)
        self.assertAlmostEqual(by_name["moderation_confirmation_ms"].value, 20000.0, delta=1.0)
        self.assertEqual(by_name["queue_age_seconds"].status, "breached")
        self.assertEqual(by_name["connector_health_percent"].value, 100.0)
        self.assertEqual(by_name["dashboard_availability_percent"].value, 100.0)
        self.assertEqual(by_name["open_dead_letters"].value, 1.0)
        self.assertAlmostEqual(by_name["backup_freshness_seconds"].value, 3600.0, delta=1.0)
        self.assertEqual(len(list_tenant_slo_samples(
            self.connection, tenant=TenantContext(1),
        )), 8)

        peer = collect_tenant_slo_snapshot(
            self.connection, tenant=TenantContext(other_community_id),
            observed_at="2026-09-03T12:00:00+00:00",
        )
        peer_by_name = {sample.metric_name: sample for sample in peer}
        self.assertEqual(peer_by_name["open_dead_letters"].value, 0.0)
        self.assertEqual(peer_by_name["connector_health_percent"].status, "no_data")
        self.assertEqual(peer_by_name["queue_age_seconds"].status, "no_data")
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM tenant_slo_samples WHERE community_id=?",
            (other_community_id,),
        ).fetchone()[0], 8)


if __name__ == "__main__":
    unittest.main()
