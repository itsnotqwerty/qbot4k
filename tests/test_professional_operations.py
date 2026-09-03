from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.db import connect_database, initialize_database, mark_moderation_action_completed
from src.contexts import ActorAttribution, TenantContext
from src.intelligence.liveops import live_operations_snapshot
from src.intelligence.professional_ops import (
    activate_scheduled_on_call,
    activate_playbook,
    assign_incident,
    conversation_context,
    create_notification_destination,
    dispatch_pending_notifications,
    escalate_incident,
    generate_post_stream_briefing,
    handoff_shift,
    list_moderation_shift_schedule,
    project_stream_session,
    record_audience_event,
    refresh_stream_cohorts,
    route_incident_to_on_call,
    schedule_moderation_shift,
    upsert_campaign_incident,
)
from src.intelligence.community import (
    create_community,
    create_member_appeal,
    create_member_report,
    grant_operator_role,
    register_installation,
    resolve_member_queue_item,
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
        self.twitch_installation_id = register_installation(
            self.connection, community_id=1, platform="twitch",
            external_community_id="12345", display_name="Operations",
        )

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

    def test_stream_projection_rejects_missing_tenant_context(self) -> None:
        observation = {
            "id": 1, "event_type": "stream.started", "community_id": None,
            "attributes_json": "{}", "platform": "twitch", "context_id": "channel",
            "container_id": "channel", "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaisesRegex(ValueError, "community_id is required"):
            project_stream_session(self.connection, observation)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stream_sessions").fetchone()[0], 0
        )

    def test_member_report_and_appeal_lifecycle_is_tenant_safe(self) -> None:
        with self.connection:
            user_id = int(self.connection.execute(
                "INSERT INTO users(primary_display_name) VALUES ('Appealing member')"
            ).lastrowid)
            account_id = int(self.connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
                   VALUES ('discord','appeal-member','appeal-member',?)""", (user_id,),
            ).lastrowid)
            observation_id = self._observation("message.created", datetime.now(timezone.utc).isoformat())
            message_id = int(self.connection.execute(
                """INSERT INTO messages(
                       community_id,platform,platform_message_id,platform_account_id,
                       observation_id,channel_id,content_raw,content_normalized,sent_at
                   ) VALUES (1,'discord','appeal-message',?,?,'channel','message','message',CURRENT_TIMESTAMP)""",
                (account_id, observation_id),
            ).lastrowid)
            action_id = int(self.connection.execute(
                """INSERT INTO moderation_actions(
                       community_id,platform,message_id,target_platform_account_id,user_id,
                       action_type,actor_type,actor_id,status
                   ) VALUES (1,'discord',?,?,?,'ban','operator',?,'completed')""",
                (message_id, account_id, user_id, self.operator_one),
            ).lastrowid)
        grant_operator_role(self.connection, operator_id=self.operator_one, community_id=1, role="admin")
        grant_operator_role(self.connection, operator_id=self.operator_two, community_id=1, role="moderator")
        report_id = create_member_report(
            self.connection, community_id=1, subject_platform_account_id=account_id,
            category="harassment", summary="Repeated unwanted messages", severity="high",
        )
        appeal_id = create_member_appeal(
            self.connection, community_id=1, moderation_action_id=action_id,
            appellant_platform_account_id=account_id, reason="Context was missed", severity="high",
        )
        appeal = self.connection.execute(
            "SELECT assigned_operator_id,status FROM member_appeals WHERE id=?", (appeal_id,)
        ).fetchone()
        self.assertEqual((int(appeal[0]), str(appeal[1])), (self.operator_two, "open"))
        with self.assertRaises(PermissionError):
            resolve_member_queue_item(
                self.connection, tenant=TenantContext(1),
                actor=ActorAttribution("operator", self.operator_one), queue_type="appeal",
                item_id=appeal_id, resolution="upheld", note="Original decision",
            )
        resolve_member_queue_item(
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", self.operator_two), queue_type="appeal",
            item_id=appeal_id, resolution="reversed", note="Independent review",
        )
        resolve_member_queue_item(
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", self.operator_two), queue_type="report",
            item_id=report_id, resolution="substantiated", note="Evidence reviewed",
        )
        with self.assertRaises(LookupError):
            resolve_member_queue_item(
                self.connection, tenant=TenantContext(999),
                actor=ActorAttribution("operator", self.operator_two), queue_type="report",
                item_id=report_id, resolution="dismissed", note="Wrong tenant",
            )
        self.assertEqual(self.connection.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE action_type IN ('member_report.created','member_appeal.created',
                                     'member_report.resolved','member_appeal.resolved')"""
        ).fetchone()[0], 4)

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

    @patch("src.intelligence.professional_ops.urlopen")
    def test_notification_dispatch_is_tenant_scoped(self, mock_urlopen) -> None:
        other_community_id = create_community(
            self.connection, workspace_id=1, name="Other Operations", slug="other-operations"
        )
        destination_ids = [
            create_notification_destination(
                self.connection, community_id=community_id, destination_type="generic_webhook",
                name=f"Destination {community_id}", target=f"https://example.test/{community_id}",
            )
            for community_id in (1, other_community_id)
        ]
        with self.connection:
            for destination_id in destination_ids:
                self.connection.execute(
                    "INSERT INTO notification_deliveries(destination_id,payload_json) VALUES (?,?)",
                    (destination_id, '{"title":"Tenant incident"}'),
                )
        response = mock_urlopen.return_value.__enter__.return_value
        response.status = 204
        response.read.return_value = b""

        self.assertEqual(
            dispatch_pending_notifications(self.connection, tenant=TenantContext(1)), 1
        )
        statuses = self.connection.execute(
            """SELECT n.community_id,d.status FROM notification_deliveries d
               JOIN notification_destinations n ON n.id=d.destination_id
               ORDER BY n.community_id"""
        ).fetchall()
        self.assertEqual([(int(row[0]), str(row[1])) for row in statuses], [
            (1, "delivered"), (other_community_id, "pending"),
        ])

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
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", self.operator_one),
            incoming_operator_id=self.operator_two, note="Watch linked accounts",
        )
        self.assertGreater(shift_id, 0)
        self.assertEqual(self.connection.execute(
            "SELECT assigned_operator_id FROM operations_incidents WHERE id=?", (incident_id,)
        ).fetchone()[0], self.operator_two)
        run = activate_playbook(
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", self.operator_two),
            playbook_key="raid-lockdown", incident_id=incident_id,
        )
        self.assertEqual(run["name"], "Raid Lockdown")
        self.assertEqual(len(run["steps"]), 4)

        other_community_id = create_community(
            self.connection, workspace_id=1, name="Foreign Ops", slug="foreign-ops"
        )
        with self.assertRaisesRegex(ValueError, "tenant incident was not found"):
            activate_playbook(
                self.connection, tenant=TenantContext(other_community_id),
                actor=ActorAttribution("operator", self.operator_two),
                playbook_key="raid-lockdown", incident_id=incident_id,
            )

    def test_scheduled_shifts_route_incidents_to_current_tenant_on_call(self) -> None:
        grant_operator_role(
            self.connection, operator_id=self.operator_one, community_id=1, role="admin"
        )
        grant_operator_role(
            self.connection, operator_id=self.operator_two, community_id=1, role="moderator"
        )
        starts_at = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        ends_at = starts_at + timedelta(hours=4)
        schedule_id = schedule_moderation_shift(
            self.connection, tenant=TenantContext(1),
            actor=ActorAttribution("operator", self.operator_one), operator_id=self.operator_two,
            starts_at=starts_at.isoformat(), ends_at=ends_at.isoformat(),
        )
        with self.assertRaisesRegex(ValueError, "overlaps"):
            schedule_moderation_shift(
                self.connection, tenant=TenantContext(1),
                actor=ActorAttribution("operator", self.operator_one), operator_id=self.operator_one,
                starts_at=(starts_at + timedelta(hours=1)).isoformat(),
                ends_at=(ends_at + timedelta(hours=1)).isoformat(),
            )
        incident_id = int(self.connection.execute(
            """INSERT INTO operations_incidents(
                   community_id,incident_type,severity,title,summary
               ) VALUES (1,'coverage','high','Coverage incident','Needs on-call')"""
        ).lastrowid)
        routed_operator = route_incident_to_on_call(
            self.connection, community_id=1, incident_id=incident_id,
            routed_by_operator_id=self.operator_one, now=starts_at + timedelta(hours=1),
        )
        schedule = list_moderation_shift_schedule(self.connection, community_id=1)
        activity = self.connection.execute(
            "SELECT activity_type,payload_json FROM incident_activity WHERE incident_id=?",
            (incident_id,),
        ).fetchone()

        self.assertEqual(routed_operator, self.operator_two)
        self.assertEqual(int(schedule[0]["id"]), schedule_id)
        self.assertEqual(schedule[0]["status"], "active")
        self.assertEqual(activity["activity_type"], "routed_on_call")
        self.assertEqual(json.loads(activity["payload_json"])["assigned_operator_id"], self.operator_two)
        self.assertIsNone(activate_scheduled_on_call(
            self.connection, community_id=1, now=ends_at
        ))

        second_community = create_community(
            self.connection, workspace_id=1, name="Second Ops", slug="second-ops"
        )
        grant_operator_role(
            self.connection, operator_id=self.operator_one,
            community_id=second_community, role="admin",
        )
        schedule_moderation_shift(
            self.connection, tenant=TenantContext(second_community),
            actor=ActorAttribution("operator", self.operator_one), operator_id=self.operator_one,
            starts_at=starts_at.isoformat(), ends_at=ends_at.isoformat(),
        )
        with self.assertRaisesRegex(ValueError, "open incident was not found"):
            route_incident_to_on_call(
                self.connection, community_id=second_community, incident_id=incident_id,
                routed_by_operator_id=self.operator_one, now=starts_at + timedelta(hours=1),
            )

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
                   community_id,platform,target_platform_account_id,action_type,actor_type,reason,status
               ) VALUES (1,'twitch',?,'timeout','operator','test','pending')""", (account_id,)
        ).lastrowid)
        other_community_id = create_community(
            self.connection, workspace_id=1, name="Other Moderation", slug="other-moderation"
        )
        with self.assertRaisesRegex(LookupError, "tenant moderation action not found"):
            mark_moderation_action_completed(
                self.connection, action_id, tenant=TenantContext(other_community_id),
                provider_status="204", provider_response={"ok": True},
            )
        mark_moderation_action_completed(
            self.connection, action_id, tenant=TenantContext(1),
            provider_status="204", provider_response={"ok": True}
        )
        status = self.connection.execute(
            "SELECT status,provider_confirmed_at FROM moderation_actions WHERE id=?", (action_id,)
        ).fetchone()
        self.assertEqual(status[0], "completed")
        self.assertIsNotNone(status[1])

    def test_twitch_control_requires_live_control_installation_capability(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE community_installations SET capabilities_json='[\"events\"]' WHERE id=?",
                (self.twitch_installation_id,),
            )
        control = TwitchControlPlane(_TokenManager())
        control._request = lambda *args, **kwargs: self.fail("provider request must not run")

        with self.assertRaisesRegex(PermissionError, "does not support live_controls"):
            control.set_shield_mode(
                self.connection, community_id=1, broadcaster="12345", active=True,
                operator_id=self.operator_one,
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM twitch_control_actions").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
