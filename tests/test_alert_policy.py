from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import collect_observation, connect_database, initialize_database, operational_readiness_snapshot
from src.intelligence.analytics import emit_analytics_alerts
from src.intelligence.workflows import _upsert_relationship, intelligence_summary
from src.models import Observation


class AlertPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "alerts.sqlite3"
        self.connection = connect_database(self.database_path)
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def _seed_baseline_days(self) -> None:
        for index, day in enumerate((7, 8, 9)):
            collect_observation(self.connection, Observation(
                platform="external:baseline",
                event_type="external.item",
                external_event_id=f"baseline-{index}",
                actor_platform_user_id=f"baseline-user-{index}",
                actor_username=f"Baseline{index}",
                container_id="baseline",
                context_id="baseline",
                text="ordinary historical activity",
                occurred_at=f"2026-08-{day:02d}T10:00:00+00:00",
            ))

    def _seed_topics(self, count: int = 12) -> None:
        for index in range(count):
            unusualness = 30.0 - index
            self.connection.execute(
                """INSERT INTO emerging_topics(
                     topic_key,topic_kind,label,current_count,baseline_rate,velocity,
                     context_count,community_count,unusualness,details_json,calculated_at
                   ) VALUES (?,?,?,12,1,11,3,3,?,'{}','2026-08-11T12:00:00+00:00')""",
                (f"term:signal-{index}", "term", f"signal-{index}", unusualness),
            )

    def test_topic_alerts_require_warmup_and_expire_legacy_noise(self) -> None:
        with self.connection:
            self._seed_topics(1)
            self.connection.execute(
                """INSERT INTO intelligence_alerts(
                     alert_type,severity,title,summary,confidence,dedupe_key
                   ) VALUES ('emerging_topic','medium','Emerging Topic','legacy',0.5,
                             'topic:term:legacy:2026-08-11')"""
            )
            created = emit_analytics_alerts(
                self.connection, calculated_at="2026-08-11T12:00:00+00:00"
            )
        self.assertEqual(created, 0)
        legacy = self.connection.execute(
            "SELECT status,disposition FROM intelligence_alerts WHERE dedupe_key='topic:term:legacy:2026-08-11'"
        ).fetchone()
        self.assertEqual(tuple(legacy), ("resolved", "expired"))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM intelligence_alerts WHERE dedupe_key='topic:term:signal-0'"
        ).fetchone()[0], 0)

    def test_topics_are_limited_stable_expiring_and_respect_manual_resolution(self) -> None:
        with self.connection:
            self._seed_baseline_days()
            self._seed_topics()
            first_created = emit_analytics_alerts(
                self.connection, calculated_at="2026-08-11T12:00:00+00:00"
            )
            second_created = emit_analytics_alerts(
                self.connection, calculated_at="2026-08-12T12:00:00+00:00"
            )
        self.assertEqual(first_created, 10)
        self.assertEqual(second_created, 0)
        rows = self.connection.execute(
            """SELECT dedupe_key,status FROM intelligence_alerts
               WHERE alert_type='emerging_topic' ORDER BY dedupe_key"""
        ).fetchall()
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(not str(row[0]).endswith("2026-08-11") for row in rows))

        manually_resolved_key = str(rows[0][0])
        with self.connection:
            self.connection.execute(
                """UPDATE intelligence_alerts SET status='resolved',disposition='benign',
                   resolved_at='2026-08-12T13:00:00+00:00' WHERE dedupe_key=?""",
                (manually_resolved_key,),
            )
            self.connection.execute("DELETE FROM emerging_topics")
            emit_analytics_alerts(self.connection, calculated_at="2026-08-12T14:00:00+00:00")
        dispositions = dict(self.connection.execute(
            "SELECT dedupe_key,disposition FROM intelligence_alerts WHERE alert_type='emerging_topic'"
        ).fetchall())
        self.assertEqual(dispositions[manually_resolved_key], "benign")
        self.assertEqual(set(dispositions.values()), {"benign", "expired"})

        topic_key = manually_resolved_key.removeprefix("topic:")
        with self.connection:
            self.connection.execute(
                """INSERT INTO emerging_topics(
                     topic_key,topic_kind,label,current_count,baseline_rate,velocity,
                     context_count,community_count,unusualness,details_json,calculated_at
                   ) VALUES (?,'term','manual-topic',12,1,11,3,3,25,'{}','2026-08-12')""",
                (topic_key,),
            )
            emit_analytics_alerts(self.connection, calculated_at="2026-08-12T15:00:00+00:00")
        manual = self.connection.execute(
            "SELECT status,disposition FROM intelligence_alerts WHERE dedupe_key=?",
            (manually_resolved_key,),
        ).fetchone()
        self.assertEqual(tuple(manual), ("resolved", "benign"))

    def test_cohort_alerts_require_sample_size_and_confidence_then_expire(self) -> None:
        user_id = int(self.connection.execute(
            "INSERT INTO users(primary_display_name) VALUES ('Subject')"
        ).lastrowid)
        with self.connection:
            self.connection.execute(
                """INSERT INTO cohort_baselines(
                     cohort_type,cohort_key,signal_key,sample_size,mean_value,stddev_value,
                     median_value,p90_value,calculated_at
                   ) VALUES ('platform','discord','risk.composite',5,10,2,10,12,'2026-08-11')"""
            )
            self.connection.execute(
                """INSERT INTO cohort_anomalies(
                     user_id,cohort_type,cohort_key,signal_key,observed_value,baseline_mean,
                     z_score,direction,confidence,calculated_at
                   ) VALUES (?,'platform','discord','risk.composite',90,10,8,'above',0.9,'2026-08-11')""",
                (user_id,),
            )
            self.assertEqual(emit_analytics_alerts(
                self.connection, calculated_at="2026-08-11T12:00:00+00:00"
            ), 0)
            self.connection.execute(
                "UPDATE cohort_baselines SET sample_size=6 WHERE cohort_type='platform'"
            )
            self.assertEqual(emit_analytics_alerts(
                self.connection, calculated_at="2026-08-11T13:00:00+00:00"
            ), 1)
            self.connection.execute("UPDATE cohort_anomalies SET confidence=0.5")
            emit_analytics_alerts(self.connection, calculated_at="2026-08-11T14:00:00+00:00")
        row = self.connection.execute(
            "SELECT status,disposition,dedupe_key FROM intelligence_alerts WHERE alert_type='cohort_anomaly'"
        ).fetchone()
        self.assertEqual(tuple(row[:2]), ("resolved", "expired"))
        self.assertFalse(str(row[2]).endswith("2026-08-11"))

    def test_untriaged_count_excludes_non_open_workflow_states(self) -> None:
        with self.connection:
            for index, status in enumerate(("open", "acknowledged", "suppressed", "in_case", "resolved")):
                self.connection.execute(
                    """INSERT INTO intelligence_alerts(
                         alert_type,severity,title,summary,confidence,status,dedupe_key
                       ) VALUES ('test','low','Test','Test',0.5,?,?)""",
                    (status, f"count-{index}"),
                )
        self.assertEqual(intelligence_summary(self.connection).open_alerts, 1)
        self.assertEqual(operational_readiness_snapshot(self.connection)["counters"]["open_alerts"], 1)

    def test_coordination_requires_six_observations_and_old_noise_expires(self) -> None:
        with self.connection:
            source = int(self.connection.execute(
                "INSERT INTO users(primary_display_name) VALUES ('Source')"
            ).lastrowid)
            target = int(self.connection.execute(
                "INSERT INTO users(primary_display_name) VALUES ('Target')"
            ).lastrowid)
            for index in range(5):
                _upsert_relationship(
                    self.connection, source, target, "mention", "channel",
                    f"2026-08-11T12:0{index}:00+00:00", {},
                )
            relationship_id = int(self.connection.execute(
                "SELECT id FROM entity_relationships"
            ).fetchone()[0])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM intelligence_alerts WHERE alert_type='coordination_pattern'"
        ).fetchone()[0], 0)

        with self.connection:
            _upsert_relationship(
                self.connection, source, target, "mention", "channel",
                "2026-08-11T12:05:00+00:00", {},
            )
        alert = self.connection.execute(
            "SELECT status,severity,dedupe_key FROM intelligence_alerts WHERE alert_type='coordination_pattern'"
        ).fetchone()
        self.assertEqual(tuple(alert), (
            "open", "low", f"relationship:{relationship_id}:coordination"
        ))

        with self.connection:
            self.connection.execute(
                "UPDATE entity_relationships SET evidence_count=3 WHERE id=?", (relationship_id,)
            )
            emit_analytics_alerts(self.connection, calculated_at="2026-08-11T13:00:00+00:00")
        expired = self.connection.execute(
            "SELECT status,disposition FROM intelligence_alerts WHERE alert_type='coordination_pattern'"
        ).fetchone()
        self.assertEqual(tuple(expired), ("resolved", "expired"))


if __name__ == "__main__":
    unittest.main()
