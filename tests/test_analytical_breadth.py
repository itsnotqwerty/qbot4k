from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.contexts import TenantContext
from src.db import collect_observation, connect_database, ensure_platform_account, initialize_database
from src.intelligence.analytics import (
    analytics_snapshot,
    propagation_path,
    record_evaluation_label,
    refresh_cohort_baselines,
    refresh_community_cohort_baselines,
    refresh_emerging_topics,
    refresh_graph_analytics,
    refresh_identity_suggestions,
    review_identity_suggestion,
    run_model_evaluation,
)
from src.intelligence.content import analyze_observation_content
from src.intelligence.community import create_community, create_organization, create_workspace
from src.intelligence.events import (
    GenericEventAnalysisPipeline,
    collect_external_feed_item,
    observation_from_discord_event,
    observation_from_twitch_irc_event,
)
from src.intelligence.search import observation_pivots, save_query, search_observations
from src.intelligence.workflows import (
    record_community_temporal_signal_run,
    record_temporal_signal_run,
)
from src.models import Observation
from src.pipeline.message_analysis import AnalysisJob


def _database(tmp_path: Path):
    connection = connect_database(tmp_path / "analytics.db")
    initialize_database(connection)
    return connection


def test_community_health_outcome_analytics_are_complete_and_tenant_scoped(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        other_community_id = create_community(
            connection, workspace_id=1, name="Other analytics", slug="other-analytics"
        )
        with connection:
            user_id = int(connection.execute(
                "INSERT INTO users(primary_display_name) VALUES ('Repeat member')"
            ).lastrowid)
            account_id = int(connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
                   VALUES ('discord','repeat-member','repeat-member',?)""", (user_id,),
            ).lastrowid)
            connection.execute(
                """INSERT INTO community_memberships(
                       community_id,platform_account_id,joined_at,left_at
                   ) VALUES (1,?,'2026-08-24T10:00:00+00:00',NULL)""", (account_id,),
            )
            message_ids = []
            for index in range(2):
                message_ids.append(int(connection.execute(
                    """INSERT INTO messages(
                           platform,platform_message_id,platform_account_id,user_id,community_id,
                           channel_id,content_raw,content_normalized,sent_at
                       ) VALUES ('discord',?,?,?,1,'analytics','match','match',?)""",
                    (f"analytics-match-{index}", account_id, user_id,
                     f"2026-08-24T10:0{index}:00+00:00"),
                ).lastrowid))
            action_ids = []
            for index, action_type in enumerate(("warn", "timeout")):
                action_ids.append(int(connection.execute(
                    """INSERT INTO moderation_actions(
                           community_id,platform,message_id,target_platform_account_id,user_id,
                           action_type,actor_type,status,created_at
                       ) VALUES (1,'discord',?,?,?,?,'operator','completed',?)""",
                    (message_ids[index], account_id, user_id, action_type,
                     f"2026-08-24T11:0{index}:00+00:00"),
                ).lastrowid))
            rule_id = int(connection.execute(
                """INSERT INTO moderation_rules(
                       community_id,name,rule_type,pattern,severity
                   ) VALUES (1,'Precision rule','exact_term','match','high')"""
            ).lastrowid)
            for index, resolution in enumerate(("confirmed", "dismissed")):
                connection.execute(
                    """INSERT INTO rule_matches(message_id,moderation_rule_id,severity,reason_code)
                       VALUES (?,?,'high','rule_match')""", (message_ids[index], rule_id),
                )
                connection.execute(
                    """INSERT INTO review_queue(
                           message_id,status,severity,queue_reason_code,resolution,resolved_at
                       ) VALUES (?,'resolved','high','rule_match',?,CURRENT_TIMESTAMP)""",
                    (message_ids[index], resolution),
                )
            connection.execute(
                """INSERT INTO member_reports(
                       community_id,subject_platform_account_id,category,summary,severity,status,
                       resolution,resolved_at
                   ) VALUES (1,?,'spam','Resolved report','high','resolved','substantiated',CURRENT_TIMESTAMP)""",
                (account_id,),
            )
            connection.execute(
                """INSERT INTO member_appeals(
                       community_id,moderation_action_id,appellant_platform_account_id,reason,
                       severity,status,disposition,resolved_at
                   ) VALUES (1,?,?,'Context','high','resolved','reversed',CURRENT_TIMESTAMP)""",
                (action_ids[1], account_id),
            )
            connection.execute(
                """INSERT INTO moderation_actions(
                       community_id,platform,target_platform_account_id,user_id,action_type,
                       actor_type,status
                   ) VALUES (?,'discord',?,?,'ban','operator','completed')""",
                (other_community_id, account_id, user_id),
            )
        snapshot = analytics_snapshot(connection, community_id=1)
        assert snapshot["growth"] == [{
            "metric_date": "2026-08-24", "joins": 1, "leaves": 0, "net_growth": 1,
        }]
        assert snapshot["repeat_offenses"][0]["action_count"] == 2
        assert snapshot["report_outcomes"][0]["outcome"] == "substantiated"
        assert snapshot["appeal_outcomes"][0]["outcome"] == "reversed"
        precision = next(item for item in snapshot["rule_precision"] if item["name"] == "Precision rule")
        assert precision["matches"] == 2
        assert precision["precision"] == 0.5
        assert analytics_snapshot(connection, community_id=other_community_id)["repeat_offenses"] == []
    finally:
        connection.close()


def test_non_message_events_external_feeds_and_content_understanding(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        event = observation_from_discord_event("MESSAGE_REACTION_ADD", {
            "message_id": "m1", "channel_id": "c1", "guild_id": "g1", "user_id": "actor-1",
            "emoji": {"name": "eyes"},
        }, community_id=1)
        assert event is not None and event.event_type == "reaction.added"
        result = collect_observation(connection, event)
        GenericEventAnalysisPipeline(tmp_path / "analytics.db").analyze_event(
            connection, AnalysisJob(1, 1, "analysis", "analyze.reaction.added", result.observation_id, {}, 0, 5),
        )
        assert connection.execute("SELECT COUNT(*) FROM content_analysis").fetchone()[0] == 1

        external = collect_external_feed_item(
            connection, community_id=1, source_key="newswire", external_event_id="item-1",
            text="I will attack the site at https://target.example now", context_id="public-feed",
        )
        with connection:
            analysis = analyze_observation_content(
                connection, external.observation_id, tenant=TenantContext(1)
            )
        assert analysis.intent_label == "threat"
        assert analysis.threat_level == "critical"
        assert {entity[0] for entity in analysis.entities} >= {"url", "domain"}
        assert connection.execute("SELECT last_observed_at FROM external_feed_sources").fetchone()[0]

        twitch = observation_from_twitch_irc_event(
            "@user-id=9;tmi-sent-ts=1786406400000 :alice!alice@alice.tmi.twitch.tv JOIN #room",
            community_id=1,
        )
        assert twitch is not None and twitch.event_type == "member.joined"
    finally:
        connection.close()


def test_full_text_filters_saved_queries_and_pivots(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        result = collect_observation(connection, Observation(
            platform="discord", event_type="message.created", external_event_id="search-1",
            community_id=1,
            actor_platform_user_id="42", actor_username="Analyst", container_id="ops", context_id="guild-a",
            text="Investigate cobalt lantern at https://evidence.example/path",
            occurred_at="2026-08-11T10:00:00+00:00",
        ))
        with connection:
            analyze_observation_content(
                connection, result.observation_id, tenant=TenantContext(1)
            )
        hits = search_observations(
            connection, community_id=1, query='"cobalt lantern"', start_at="2026-08-11T00:00:00+00:00",
            event_type="message.created", entity_type="domain", entity_value="evidence.example",
        )
        assert [item["id"] for item in hits] == [result.observation_id]
        query_id = save_query(connection, "Cobalt watch", "cobalt", {"context_id": "guild-a"})
        assert query_id > 0
        pivots = observation_pivots(connection, result.observation_id, community_id=1)
        assert pivots["context_id"] == "guild-a"
        assert any(item["entity_type"] == "domain" for item in pivots["entities"])
    finally:
        connection.close()


def test_topics_graph_identity_cohorts_and_evaluation(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        for index, context in enumerate(("community-a", "community-b", "community-c")):
            result = collect_observation(connection, Observation(
                platform="discord", event_type="message.created", external_event_id=f"topic-{index}",
                community_id=1,
                actor_platform_user_id=f"topic-user-{index}", actor_username=f"Topic{index}",
                container_id=context, context_id=context,
                text="signal orchid wave https://unusual.example/story",
                occurred_at=f"2026-08-11T0{index}:00:00+00:00",
            ))
            with connection:
                analyze_observation_content(
                    connection, result.observation_id, tenant=TenantContext(1)
                )
        topic_count = refresh_emerging_topics(
            connection, community_id=1, now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
        )
        assert topic_count > 0
        domain = connection.execute("SELECT * FROM emerging_topics WHERE topic_key='domain:unusual.example'").fetchone()
        assert domain is not None and domain["community_count"] == 3 and domain["velocity"] > 0

        users = []
        for name in ("Alpha", "Bridge", "Omega", "Delta", "Echo", "Foxtrot"):
            users.append(int(connection.execute("INSERT INTO users(primary_display_name) VALUES (?)", (name,)).lastrowid))
        for source, target in zip(users, users[1:]):
            connection.execute(
                          """INSERT INTO entity_relationships(community_id,source_user_id,target_user_id,relationship_type,context_key,
                   strength,evidence_count,first_observed_at,last_observed_at) VALUES (?,?,?,'mention','g',2,2,'2026-08-10','2026-08-11')""",
                          (1, source, target),
            )
        assert refresh_graph_analytics(connection) >= 3
        assert propagation_path(connection, users[0], users[2]) == users[:3]
        assert connection.execute("SELECT is_bridge FROM graph_metrics WHERE user_id=?", (users[1],)).fetchone()[0] == 1

        left_account = ensure_platform_account(connection, platform="discord", platform_user_id="same_name", username="SignalWatcher", guild_or_channel_context="room")
        right_account = ensure_platform_account(connection, platform="twitch", platform_user_id="same-name", username="Signal_Watcher", guild_or_channel_context="room")
        connection.execute("UPDATE platform_accounts SET user_id=? WHERE id=?", (users[0], left_account))
        connection.execute("UPDATE platform_accounts SET user_id=? WHERE id=?", (users[2], right_account))
        assert refresh_identity_suggestions(connection) > 0
        suggestion = connection.execute(
            "SELECT id,status,evidence_json FROM identity_link_suggestions WHERE left_platform_account_id=? AND right_platform_account_id=?",
            (min(left_account, right_account), max(left_account, right_account)),
        ).fetchone()
        assert suggestion is not None and suggestion["status"] == "pending" and "manual_approval_required" in suggestion["evidence_json"]
        review_identity_suggestion(connection, int(suggestion["id"]), "rejected", operator_id=7)
        assert connection.execute("SELECT status FROM identity_link_suggestions WHERE id=?", (suggestion["id"],)).fetchone()[0] == "rejected"

        for index, user_id in enumerate(users):
            connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username,user_id) VALUES ('cohort',?,?,?)""",
                (f"cohort-{index}", f"Cohort{index}", user_id),
            )
            connection.execute(
                """INSERT INTO derived_signal_windows(user_id,signal_key,window_name,analyzer_version,value_real,
                   confidence,evidence_count,calculated_at) VALUES (?,'risk.composite','24h',2,?,0.9,10,'2026-08-11')""",
                (user_id, (10.0, 12.0, 90.0, 14.0, 16.0, 18.0)[index]),
            )
        baselines, anomalies = refresh_cohort_baselines(connection)
        assert baselines >= 1 and anomalies >= 1

        record_evaluation_label(connection, label_key="risk", label_value="negative", user_id=users[0])
        record_evaluation_label(connection, label_key="risk", label_value="positive", user_id=users[2])
        run_id = run_model_evaluation(connection)
        assert connection.execute("SELECT sample_size FROM model_evaluation_runs WHERE id=?", (run_id,)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM threshold_backtests WHERE evaluation_run_id=?", (run_id,)).fetchone()[0] == 3
    finally:
        connection.close()


def test_emerging_topic_refresh_and_snapshot_are_tenant_isolated(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        organization_id = create_organization(
            connection, name="Analytics Organization", slug="analytics-organization"
        )
        workspace_id = create_workspace(
            connection, organization_id=organization_id,
            name="Analytics Workspace", slug="analytics-workspace",
        )
        community_id = create_community(
            connection, workspace_id=workspace_id,
            name="Analytics Community", slug="analytics-community",
        )
        with connection:
            for tenant_id, label in ((1, "alpha-private"), (community_id, "bravo-visible")):
                for index in range(2):
                    connection.execute(
                        """INSERT INTO observations(
                               platform,community_id,event_type,external_event_id,
                               context_id,text_raw,occurred_at
                           ) VALUES ('discord',?,'message.created',?,?,?,?)""",
                        (
                            tenant_id, f"{label}-{index}", f"{label}-context-{index}",
                            f"{label} signal", f"2026-08-11T0{index}:00:00+00:00",
                        ),
                    )

        now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
        assert refresh_emerging_topics(connection, now=now, community_id=1) > 0
        assert refresh_emerging_topics(connection, now=now, community_id=community_id) > 0
        assert connection.execute(
            "SELECT COUNT(*) FROM emerging_topics WHERE community_id=1 AND label='alpha-private'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM emerging_topics WHERE community_id=? AND label='bravo-visible'",
            (community_id,),
        ).fetchone()[0] == 1

        snapshot = analytics_snapshot(connection, community_id=community_id)
        labels = {str(topic["label"]) for topic in snapshot["topics"]}
        assert "bravo-visible" in labels
        assert "alpha-private" not in labels

        refresh_emerging_topics(connection, now=now, community_id=community_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM emerging_topics WHERE community_id=1 AND label='alpha-private'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_graph_refresh_and_snapshot_are_tenant_isolated(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        organization_id = create_organization(
            connection, name="Graph Organization", slug="graph-organization"
        )
        workspace_id = create_workspace(
            connection, organization_id=organization_id,
            name="Graph Workspace", slug="graph-workspace",
        )
        community_id = create_community(
            connection, workspace_id=workspace_id,
            name="Graph Community", slug="graph-community",
        )
        with connection:
            user_ids = [
                int(connection.execute(
                    "INSERT INTO users(primary_display_name) VALUES (?)", (name,)
                ).lastrowid)
                for name in ("Tenant A User", "Shared User", "Tenant B User")
            ]
            connection.execute(
                """INSERT INTO entity_relationships(
                       community_id,source_user_id,target_user_id,relationship_type,
                       context_key,strength,evidence_count,first_observed_at,last_observed_at
                   ) VALUES (1,?,?,'mention','tenant-a',2,2,'2026-08-10','2026-08-11')""",
                (user_ids[0], user_ids[1]),
            )
            connection.execute(
                """INSERT INTO entity_relationships(
                       community_id,source_user_id,target_user_id,relationship_type,
                       context_key,strength,evidence_count,first_observed_at,last_observed_at
                   ) VALUES (?, ?,?,'mention','tenant-b',2,2,'2026-08-10','2026-08-11')""",
                (community_id, user_ids[1], user_ids[2]),
            )

        assert refresh_graph_analytics(connection, community_id=1) == 2
        assert refresh_graph_analytics(connection, community_id=community_id) == 2
        shared_rows = connection.execute(
            "SELECT community_id FROM community_graph_metrics WHERE user_id=? ORDER BY community_id",
            (user_ids[1],),
        ).fetchall()
        assert [int(row[0]) for row in shared_rows] == [1, community_id]

        snapshot = analytics_snapshot(connection, community_id=community_id)
        graph_user_ids = {int(item["user_id"]) for item in snapshot["graph"]}
        assert graph_user_ids == {user_ids[1], user_ids[2]}

        refresh_graph_analytics(connection, community_id=community_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM community_graph_metrics WHERE community_id=1"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_identity_suggestions_and_reviews_are_tenant_isolated(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        organization_id = create_organization(
            connection, name="Identity Organization", slug="identity-organization"
        )
        workspace_id = create_workspace(
            connection, organization_id=organization_id,
            name="Identity Workspace", slug="identity-workspace",
        )
        community_id = create_community(
            connection, workspace_id=workspace_id,
            name="Identity Community", slug="identity-community",
        )
        with connection:
            for tenant_id, prefix in ((1, "alpha"), (community_id, "bravo")):
                for index, platform in enumerate(("discord", "twitch")):
                    user_id = int(connection.execute(
                        "INSERT INTO users(primary_display_name) VALUES (?)",
                        (f"{prefix}-{index}",),
                    ).lastrowid)
                    account_id = int(connection.execute(
                        """INSERT INTO platform_accounts(
                               platform,platform_user_id,username,user_id
                           ) VALUES (?,?,?,?)""",
                        (platform, f"{prefix}-id-{index}", f"{prefix}_same", user_id),
                    ).lastrowid)
                    connection.execute(
                        """INSERT INTO messages(
                               platform,platform_message_id,platform_account_id,user_id,
                               community_id,channel_id,content_raw,content_normalized,sent_at
                           ) VALUES (?,?,?,?,?,'identity','identity','identity',?)""",
                        (
                            platform, f"{prefix}-message-{index}", account_id, user_id,
                            tenant_id, "2026-08-26T12:00:00+00:00",
                        ),
                    )

        assert refresh_identity_suggestions(connection, community_id=1) == 1
        assert refresh_identity_suggestions(connection, community_id=community_id) == 1
        alpha_id = int(connection.execute(
            "SELECT id FROM community_identity_link_suggestions WHERE community_id=1"
        ).fetchone()[0])
        bravo_id = int(connection.execute(
            "SELECT id FROM community_identity_link_suggestions WHERE community_id=?",
            (community_id,),
        ).fetchone()[0])

        snapshot = analytics_snapshot(connection, community_id=community_id)
        assert [int(item["id"]) for item in snapshot["identity_suggestions"]] == [bravo_id]

        try:
            review_identity_suggestion(
                connection, alpha_id, "rejected", community_id=community_id
            )
        except ValueError as exc:
            assert str(exc) == "pending suggestion not found"
        else:
            raise AssertionError("cross-tenant identity review was accepted")

        review_identity_suggestion(
            connection, bravo_id, "rejected", community_id=community_id
        )
        assert connection.execute(
            "SELECT status FROM community_identity_link_suggestions WHERE id=?",
            (bravo_id,),
        ).fetchone()[0] == "rejected"
        assert connection.execute(
            "SELECT status FROM community_identity_link_suggestions WHERE id=?",
            (alpha_id,),
        ).fetchone()[0] == "pending"
    finally:
        connection.close()


def test_community_signals_and_cohorts_are_tenant_isolated(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        organization_id = create_organization(
            connection, name="Signal Organization", slug="signal-organization"
        )
        workspace_id = create_workspace(
            connection, organization_id=organization_id,
            name="Signal Workspace", slug="signal-workspace",
        )
        community_id = create_community(
            connection, workspace_id=workspace_id,
            name="Signal Community", slug="signal-community",
        )
        with connection:
            shared_user_id = int(connection.execute(
                "INSERT INTO users(primary_display_name) VALUES ('Shared Signal User')"
            ).lastrowid)
            shared_account_id = int(connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
                   VALUES ('discord','shared-signal','shared-signal',?)""",
                (shared_user_id,),
            ).lastrowid)
            for tenant_id, count in ((1, 1), (community_id, 3)):
                for index in range(count):
                    connection.execute(
                        """INSERT INTO messages(
                               platform,platform_message_id,platform_account_id,user_id,
                               community_id,channel_id,content_raw,content_normalized,sent_at
                           ) VALUES ('discord',?,?,?,?,?,'signal','signal',?)""",
                        (
                            f"signal-{tenant_id}-{index}", shared_account_id, shared_user_id,
                            tenant_id, f"channel-{tenant_id}", "2026-08-26T12:00:00+00:00",
                        ),
                    )

        timestamp = "2026-08-26T13:00:00+00:00"
        assert record_community_temporal_signal_run(
            connection, community_id=1, user_id=shared_user_id, calculated_at=timestamp
        ) is not None
        assert record_community_temporal_signal_run(
            connection, community_id=community_id, user_id=shared_user_id, calculated_at=timestamp
        ) is not None
        signal_counts = connection.execute(
            """SELECT community_id,value_real FROM community_derived_signal_windows
               WHERE user_id=? AND signal_key='activity.message_count' AND window_name='24h'
               ORDER BY community_id""",
            (shared_user_id,),
        ).fetchall()
        assert [(int(row[0]), float(row[1])) for row in signal_counts] == [
            (1, 1.0), (community_id, 3.0),
        ]

        assert record_temporal_signal_run(
            connection, community_id=1, user_id=shared_user_id,
            calculated_at="2026-08-26T14:00:00+00:00",
        ) is not None
        legacy_count = connection.execute(
            """SELECT value_real FROM derived_signal_windows
               WHERE user_id=? AND signal_key='activity.message_count' AND window_name='24h'""",
            (shared_user_id,),
        ).fetchone()
        assert legacy_count is not None
        assert float(legacy_count[0]) == 1.0

        with connection:
            cohort_user_ids = []
            for index, value in enumerate((0.8, 0.9, 1.0, 1.1, 1.2, 10.0)):
                user_id = int(connection.execute(
                    "INSERT INTO users(primary_display_name) VALUES (?)", (f"Cohort {index}",)
                ).lastrowid)
                cohort_user_ids.append(user_id)
                account_id = int(connection.execute(
                    """INSERT INTO platform_accounts(platform,platform_user_id,username,user_id)
                       VALUES ('twitch',?,?,?)""",
                    (f"cohort-{index}", f"cohort-{index}", user_id),
                ).lastrowid)
                connection.execute(
                    """INSERT INTO messages(
                           platform,platform_message_id,platform_account_id,user_id,
                           community_id,channel_id,content_raw,content_normalized,sent_at
                       ) VALUES ('twitch',?,?,?,?,?,'cohort','cohort',?)""",
                    (
                        f"cohort-message-{index}", account_id, user_id, community_id,
                        "cohort-channel", timestamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO community_derived_signal_windows(
                           community_id,user_id,signal_key,window_name,analyzer_version,
                           value_real,confidence,evidence_count,calculated_at
                       ) VALUES (?,?,'risk.composite','24h',1,?,0.9,20,?)""",
                    (community_id, user_id, value, timestamp),
                )
            connection.execute(
                """INSERT INTO community_cohort_anomalies(
                       community_id,user_id,cohort_type,cohort_key,signal_key,
                       observed_value,baseline_mean,z_score,direction,confidence,calculated_at
                   ) VALUES (1,?,'platform','discord','risk.composite',9,1,8,'above',0.9,?)""",
                (shared_user_id, timestamp),
            )

        baselines, anomalies = refresh_community_cohort_baselines(
            connection, community_id=community_id, calculated_at=timestamp
        )
        assert baselines > 0
        assert anomalies > 0
        assert connection.execute(
            "SELECT COUNT(*) FROM community_cohort_anomalies WHERE community_id=1"
        ).fetchone()[0] == 1
        snapshot = analytics_snapshot(connection, community_id=community_id)
        assert snapshot["cohort_anomalies"]
        assert all(int(item["community_id"]) == community_id for item in snapshot["cohort_anomalies"])
    finally:
        connection.close()


def test_every_analytics_table_column_sorts_in_both_directions(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        with connection:
            user_ids = [int(connection.execute("INSERT INTO users(primary_display_name) VALUES (?)", (name,)).lastrowid) for name in ("Zulu", "Alpha", "Middle")]
            topic_rows = [
                ("term:zulu", "term", "Zulu", 2.0, 1, 0.2),
                ("phrase:alpha", "phrase", "Alpha", 9.0, 3, 0.9),
                ("domain:middle", "domain", "Middle", 5.0, 2, 0.5),
            ]
            for key, kind, label, velocity, communities, unusualness in topic_rows:
                connection.execute(
                                """INSERT INTO emerging_topics(community_id,topic_key,topic_kind,label,current_count,baseline_rate,velocity,
                       context_count,community_count,unusualness,details_json,calculated_at)
                                    VALUES (1,?,?,?,3,1,?,1,?,?,'{}','2026-08-11')""",
                                (key, kind, label, velocity, communities, unusualness),
                )
            graph_rows = [
                (user_ids[0], 3.0, 1.0, 4.0, 0.2, 0.1, 3, 0, 0.3),
                (user_ids[1], 1.0, 3.0, 7.0, 0.9, 0.8, 1, 1, 0.9),
                (user_ids[2], 2.0, 2.0, 5.0, 0.5, 0.4, 2, 0, 0.6),
            ]
            connection.executemany(
                """INSERT INTO graph_metrics(user_id,in_degree,out_degree,weighted_degree,betweenness,pagerank,
                   cluster_id,is_bridge,influence_score,calculated_at) VALUES (?,?,?,?,?,?,?,?,?,'2026-08-11')""",
                graph_rows,
            )
            account_ids = []
            for index in range(6):
                account_ids.append(int(connection.execute(
                    "INSERT INTO platform_accounts(platform,platform_user_id,username,user_id) VALUES (?,?,?,?)",
                    (f"platform-{index}", f"account-{index}", f"Account{index}", user_ids[index % 3]),
                ).lastrowid))
            identity_rows = [
                (account_ids[0], account_ids[1], 0.2, "rejected"),
                (account_ids[2], account_ids[3], 0.9, "pending"),
                (account_ids[4], account_ids[5], 0.5, "approved"),
            ]
            connection.executemany(
                """INSERT INTO identity_link_suggestions(left_platform_account_id,right_platform_account_id,
                   confidence,status,evidence_json,model_version) VALUES (?,?,?,?,'{}',1)""",
                identity_rows,
            )
            cohort_rows = [
                (user_ids[0], "zulu", "signal.z", -3.0, "below", 0.2),
                (user_ids[1], "alpha", "signal.a", 1.0, "above", 0.9),
                (user_ids[2], "middle", "signal.m", 2.0, "above", 0.5),
            ]
            connection.executemany(
                """INSERT INTO cohort_anomalies(user_id,cohort_type,cohort_key,signal_key,observed_value,
                   baseline_mean,z_score,direction,confidence,calculated_at)
                   VALUES (?,'platform',?,?,1,0,?,?,?,'2026-08-11')""",
                cohort_rows,
            )
            evaluation_rows = [
                ("model.z", 1, 10, "2026-08-11T01:00:00+00:00"),
                ("model.a", 3, 30, "2026-08-11T03:00:00+00:00"),
                ("model.m", 2, 20, "2026-08-11T02:00:00+00:00"),
            ]
            connection.executemany(
                """INSERT INTO model_evaluation_runs(model_key,model_version,sample_size,metrics_json,
                   score_distribution_json,calculated_at) VALUES (?,?,?,'{}','{}',?)""",
                evaluation_rows,
            )

        visible_columns = {
            "topics": ("topic_kind", "label", "velocity", "community_count", "unusualness"),
            "graph": ("user_id", "pagerank", "betweenness", "is_bridge", "cluster_id", "influence_score"),
            "identity_suggestions": ("id", "left_platform_account_id", "right_platform_account_id", "confidence", "status"),
            "cohort_anomalies": ("user_id", "cohort_key", "signal_key", "z_score", "direction", "confidence"),
            "evaluation": ("model_key", "model_version", "sample_size", "calculated_at"),
        }
        tenant_snapshot = analytics_snapshot(connection, community_id=1)
        assert tenant_snapshot["identity_suggestions"] == []
        assert tenant_snapshot["cohort_anomalies"] == []
        assert tenant_snapshot["evaluation"] == []
        baseline = analytics_snapshot(connection)
        for table_name, columns in visible_columns.items():
            for column in columns:
                values = [row[column] for row in baseline[table_name]]
                key = (lambda value: value.casefold()) if values and isinstance(values[0], str) else (lambda value: value)
                expected = sorted(values, key=key)
                ascending = analytics_snapshot(connection, sorts={table_name: (column, "asc")})[table_name]
                descending = analytics_snapshot(connection, sorts={table_name: (column, "desc")})[table_name]
                assert [row[column] for row in ascending] == expected
                assert [row[column] for row in descending] == list(reversed(expected))
    finally:
        connection.close()
