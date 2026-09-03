from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_type_hints

from src.contexts import ActorAttribution, TenantContext
from src.dashboard.moderation import (
    execute_bulk_moderation,
    list_moderation_rules,
    list_open_reviews,
    resolve_review,
)
from src.dashboard.overview import load_overview_snapshot
from src.dashboard.users import search_users
from src.db import (
    connect_database,
    initialize_database,
    load_enabled_moderation_rules,
    persist_normalized_message,
    record_moderation_action,
    upsert_moderation_rule,
)
from src.intelligence.search import observation_pivots, search_observations
from src.intelligence.analytics import refresh_emerging_topics
from src.intelligence.liveops import live_operations_snapshot
from src.intelligence.community import (
    emergency_remove_operator_access,
    invite_operator,
    resolve_member_queue_item,
    revoke_operator_invitation,
    transfer_community_ownership,
)
from src.intelligence.professional_ops import (
    activate_playbook,
    handoff_shift,
    moderator_workload_report,
    schedule_moderation_shift,
)
from src.intelligence.userprofiles import add_user_note
from src.intelligence.workflows import (
    add_case_entity,
    add_case_evidence,
    add_case_note,
    create_case_from_alert,
    dispose_alert,
    update_alert_workflow,
    update_case,
)
from src.models import NormalizedMessage
from src.twitch_eventsub import observation_from_eventsub


class ContextContractTests(unittest.TestCase):
    def test_dashboard_repository_boundaries_require_community_context(self) -> None:
        for function in (
            list_open_reviews, list_moderation_rules, load_overview_snapshot, search_users,
            observation_from_eventsub,
            search_observations, observation_pivots, upsert_moderation_rule,
            load_enabled_moderation_rules,
            refresh_emerging_topics, live_operations_snapshot, moderator_workload_report,
            create_case_from_alert, update_case, add_case_entity, add_case_evidence,
            add_case_note, update_alert_workflow, dispose_alert, add_user_note,
        ):
            parameter = inspect.signature(function).parameters["community_id"]
            self.assertIs(parameter.default, inspect.Parameter.empty, function.__name__)

        review_parameters = inspect.signature(resolve_review).parameters
        self.assertEqual(review_parameters["tenant"].annotation, "TenantContext")
        self.assertEqual(review_parameters["actor"].annotation, "ActorAttribution")

    def test_tenant_context_requires_positive_identifiers(self) -> None:
        for community_id in (None, "", 0, -1):
            with self.subTest(community_id=community_id):
                with self.assertRaises(ValueError):
                    TenantContext.require(community_id)

        with self.assertRaises(ValueError):
            TenantContext.require(1, installation_id=0)

    def test_operator_attribution_requires_actor_identity(self) -> None:
        with self.assertRaises(ValueError):
            ActorAttribution("operator")
        with self.assertRaises(ValueError):
            ActorAttribution("operator", 0)

        actor = ActorAttribution(" Operator ", 7)
        self.assertEqual((actor.actor_type, actor.actor_id), ("operator", 7))

    def test_production_mutation_boundaries_use_tenant_and_actor_contracts(self) -> None:
        boundaries = (
            resolve_review,
            execute_bulk_moderation,
            resolve_member_queue_item,
            invite_operator,
            revoke_operator_invitation,
            transfer_community_ownership,
            emergency_remove_operator_access,
            handoff_shift,
            schedule_moderation_shift,
            activate_playbook,
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary.__name__):
                hints = get_type_hints(boundary)
                self.assertIs(hints.get("tenant"), TenantContext)
                self.assertIs(hints.get("actor"), ActorAttribution)

    def test_repository_writes_preserve_tenant_and_actor_contracts(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "contexts.sqlite3")
            try:
                initialize_database(connection)
                with connection:
                    connection.execute(
                        "INSERT INTO communities(id,workspace_id,name,slug) VALUES (2,1,'Second','second')"
                    )
                message = NormalizedMessage(
                    platform="discord",
                    platform_message_id="message-2",
                    platform_user_id="target-2",
                    username="target",
                    channel_id="channel-2",
                    content_raw="hello",
                    sent_at=datetime.now(timezone.utc).isoformat(),
                    metadata={"community_id": 2},
                )
                result = persist_normalized_message(connection, message)
                message_row = connection.execute(
                    "SELECT community_id,platform_account_id FROM messages WHERE id=?",
                    (result.message_id,),
                ).fetchone()
                action_id = record_moderation_action(
                    connection,
                    platform="discord",
                    message_id=result.message_id,
                    target_platform_account_id=int(message_row[1]),
                    action_type="timeout",
                    reason="test",
                    actor_type="operator",
                    actor_id=7,
                    community_id=2,
                )
                action_row = connection.execute(
                    "SELECT community_id,actor_type,actor_id FROM moderation_actions WHERE id=?",
                    (action_id,),
                ).fetchone()

                self.assertEqual(int(message_row[0]), 2)
                self.assertEqual(tuple(action_row), (2, "operator", 7))
            finally:
                connection.close()

    def test_message_persistence_rejects_missing_tenant_context(self) -> None:
        with TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "missing-context.sqlite3")
            try:
                initialize_database(connection)
                message = NormalizedMessage(
                    platform="discord",
                    platform_message_id="missing-context",
                    platform_user_id="target",
                    username="target",
                    channel_id="channel",
                    content_raw="hello",
                    sent_at=datetime.now(timezone.utc).isoformat(),
                )
                with self.assertRaisesRegex(ValueError, "community_id is required"):
                    persist_normalized_message(connection, message)
                count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
