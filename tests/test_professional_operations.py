from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import connect_database, initialize_database, mark_moderation_action_completed
from src.intelligence.liveops import live_operations_snapshot
from src.intelligence.professional_ops import (
    activate_playbook,
    assign_incident,
    conversation_context,
    escalate_incident,
    generate_post_stream_briefing,
    handoff_shift,
    record_audience_event,
    refresh_stream_cohorts,
    upsert_campaign_incident,
)
from src.twitch_auth import TwitchTokenValidation
from src.twitch_control import TwitchControlPlane


class _TokenManager:
    def validate_token(self) -> TwitchTokenValidation:
        return TwitchTokenValidation("token", "operator", "client", "900")


class ProfessionalOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "ops.sqlite3")
        initialize_database(self.connection)
        self.operator_one = int(self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('op-1','alpha','admin')"
        ).lastrowid)
        self.operator_two = int(self.connection.execute(
            "INSERT INTO operator_accounts(discord_user_id,discord_username,role) VALUES ('op-2','bravo','moderator')"
        ).lastrowid)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _observation(self, event_type: str, occurred_at: str, *, text: str = "", attributes=None) -> int:
        return int(self.connection.execute(
            """INSERT INTO observations(
                   community_id,platform,event_type,external_event_id,context_id,text_raw,
                   attributes_json,raw_payload_json,occurred_at
               ) VALUES (1,'twitch',?,?,?,?,?,'{}',?)""",
            (event_type, f"{event_type}-{occurred_at}-{text}", "channel", text,
             json.dumps(attributes or {}), occurred_at),
        ).lastrowid)

    def test_conversation_context_returns_before_finding_and_after(self) -> None:
        base = datetime.now(timezone.utc) - timedelta(minutes=2)
        first = self._observation("message.created", base.isoformat(), text="first")
        finding = self._observation("message.created", (base + timedelta(seconds=1)).isoformat(), text="finding")
        last = self._observation("message.created", (base + timedelta(seconds=2)).isoformat(), text="last")
        payload = conversation_context(self.connection, finding, before=5, after=5)
        self.assertEqual([item["observation_id"] for item in payload["items"]], [first, finding, last])
        self.assertTrue(payload["items"][1]["is_finding"])

    def test_campaign_findings_are_grouped_into_one_operations_incident(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        campaign_id = int(self.connection.execute(
            """INSERT INTO coordination_campaigns(
                   community_id,campaign_key,campaign_type,severity,message_count,actor_count,
                   confidence,first_observed_at,last_observed_at
               ) VALUES (1,'repeat:one','repeated_message','high',8,6,.91,?,?)""", (now, now),
        ).lastrowid)
        for index in range(3):
            observation_id = self._observation("message.created", now, text=f"spam {index}")
            self.connection.execute(
                "INSERT INTO coordination_campaign_members(campaign_id,observation_id,similarity) VALUES (?,?,.95)",
                (campaign_id, observation_id),
            )
            self.connection.execute(
                """INSERT INTO intelligence_alerts(
                       community_id,observation_id,alert_type,severity,title,summary,confidence,dedupe_key
                   ) VALUES (1,?,'content','high','Spam','Repeated spam',.9,?)""",
                (observation_id, f"alert-{index}"),
            )
        incident_id = upsert_campaign_incident(self.connection, campaign_id)
        self.assertEqual(upsert_campaign_incident(self.connection, campaign_id), incident_id)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM operations_incidents WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM intelligence_alerts WHERE status='grouped'"
        ).fetchone()[0], 3)

    def test_assignment_escalation_handoff_and_playbook_state(self) -> None:
        incident_id = int(self.connection.execute(
            """INSERT INTO operations_incidents(
                   community_id,incident_type,severity,title,summary
               ) VALUES (1,'raid','high','Raid','Attack in progress')"""
        ).lastrowid)
        assign_incident(self.connection, incident_id=incident_id, operator_id=self.operator_one,
                        assigned_by=self.operator_one)
        self.assertEqual(escalate_incident(
            self.connection, incident_id=incident_id, operator_id=self.operator_one, note="Need help"
        ), 1)
        shift_id = handoff_shift(
            self.connection, community_id=1, outgoing_operator_id=self.operator_one,
            incoming_operator_id=self.operator_two, note="Watch linked accounts",
        )
        self.assertGreater(shift_id, 0)
        self.assertEqual(self.connection.execute(
            "SELECT assigned_operator_id FROM operations_incidents WHERE id=?", (incident_id,)
        ).fetchone()[0], self.operator_two)
        run = activate_playbook(
            self.connection, community_id=1, playbook_key="raid-lockdown",
            operator_id=self.operator_two, incident_id=incident_id,
        )
        self.assertEqual(run["name"], "Raid Lockdown")
        self.assertEqual(len(run["steps"]), 4)

    def test_cohorts_briefing_audience_graph_and_live_snapshot(self) -> None:
        start = datetime.now(timezone.utc) - timedelta(minutes=30)
        session_id = int(self.connection.execute(
            """INSERT INTO stream_sessions(
                   community_id,platform,stream_key,title,started_at
               ) VALUES (1,'twitch','channel','Professional stream',?)""", (start.isoformat(),)
        ).lastrowid)
        account_id = int(self.connection.execute(
            "INSERT INTO platform_accounts(platform,platform_user_id,username) VALUES ('twitch','viewer-1','viewer')"
        ).lastrowid)
        observation_id = self._observation(
            "message.created", (start + timedelta(minutes=2)).isoformat(), text="hello",
            attributes={"subscriber": True, "badges": ["vip"]},
        )
        self.connection.execute(
            """INSERT INTO messages(
                   observation_id,platform,platform_message_id,platform_account_id,community_id,
                   channel_id,content_raw,content_normalized,sent_at
               ) VALUES (?,'twitch','message-1',?,1,'channel','hello','hello',?)""",
            (observation_id, account_id, (start + timedelta(minutes=2)).isoformat()),
        )
        cohorts = refresh_stream_cohorts(self.connection, session_id)
        self.assertEqual(cohorts["unique"], 1)
        self.assertEqual(cohorts["new"], 1)
        self.assertEqual(cohorts["subscriber"], 1)
        raid_observation = self._observation(
            "channel.raided", datetime.now(timezone.utc).isoformat(),
            attributes={"from_broadcaster_user_id": "source", "to_broadcaster_user_id": "channel", "viewers": 45},
        )
        row = self.connection.execute("SELECT * FROM observations WHERE id=?", (raid_observation,)).fetchone()
        self.assertIsNotNone(record_audience_event(self.connection, row))
        briefing_id = generate_post_stream_briefing(self.connection, session_id)
        self.assertGreater(briefing_id, 0)
        snapshot = live_operations_snapshot(self.connection, community_id=1)
        self.assertEqual(snapshot["cohorts"]["unique"]["members"], 1)
        self.assertEqual(snapshot["audience_graph"][0]["edge_type"], "raid")
        self.assertTrue(snapshot["briefings"])

    def test_twitch_control_and_moderation_status_are_provider_confirmed(self) -> None:
        control = TwitchControlPlane(_TokenManager())
        control._request = lambda url, method, token, client, payload=None: ("200", {"data": [{"is_active": True}]})
        result = control.set_shield_mode(
            self.connection, community_id=1, broadcaster="12345", active=True,
            operator_id=self.operator_one,
        )
        self.assertEqual(result["status"], "confirmed")
        account_id = int(self.connection.execute(
            "INSERT INTO platform_accounts(platform,platform_user_id,username) VALUES ('twitch','target','target')"
        ).lastrowid)
        action_id = int(self.connection.execute(
            """INSERT INTO moderation_actions(
                   platform,target_platform_account_id,action_type,actor_type,reason,status
               ) VALUES ('twitch',?,'timeout','operator','test','pending')""", (account_id,)
        ).lastrowid)
        mark_moderation_action_completed(
            self.connection, action_id, provider_status="204", provider_response={"ok": True}
        )
        status = self.connection.execute(
            "SELECT status,provider_confirmed_at FROM moderation_actions WHERE id=?", (action_id,)
        ).fetchone()
        self.assertEqual(status[0], "completed")
        self.assertIsNotNone(status[1])


if __name__ == "__main__":
    unittest.main()
