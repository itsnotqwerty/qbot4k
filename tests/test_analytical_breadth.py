from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.db import collect_observation, connect_database, ensure_platform_account, initialize_database
from src.intelligence.analytics import (
    analytics_snapshot,
    propagation_path,
    record_evaluation_label,
    refresh_cohort_baselines,
    refresh_emerging_topics,
    refresh_graph_analytics,
    refresh_identity_suggestions,
    review_identity_suggestion,
    run_model_evaluation,
)
from src.intelligence.content import analyze_observation_content
from src.intelligence.events import (
    GenericEventAnalysisPipeline,
    collect_external_feed_item,
    observation_from_discord_event,
    observation_from_twitch_irc_event,
)
from src.intelligence.search import observation_pivots, save_query, search_observations
from src.models import Observation
from src.pipeline.message_analysis import AnalysisJob


def _database(tmp_path: Path):
    connection = connect_database(tmp_path / "analytics.db")
    initialize_database(connection)
    return connection


def test_non_message_events_external_feeds_and_content_understanding(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        event = observation_from_discord_event("MESSAGE_REACTION_ADD", {
            "message_id": "m1", "channel_id": "c1", "guild_id": "g1", "user_id": "actor-1",
            "emoji": {"name": "eyes"},
        })
        assert event is not None and event.event_type == "reaction.added"
        result = collect_observation(connection, event)
        GenericEventAnalysisPipeline(tmp_path / "analytics.db").analyze_event(
            connection, AnalysisJob(1, "analysis", "analyze.reaction.added", result.observation_id, {}, 0, 5),
        )
        assert connection.execute("SELECT COUNT(*) FROM content_analysis").fetchone()[0] == 1

        external = collect_external_feed_item(
            connection, source_key="newswire", external_event_id="item-1",
            text="I will attack the site at https://target.example now", context_id="public-feed",
        )
        with connection:
            analysis = analyze_observation_content(connection, external.observation_id)
        assert analysis.intent_label == "threat"
        assert analysis.threat_level == "critical"
        assert {entity[0] for entity in analysis.entities} >= {"url", "domain"}
        assert connection.execute("SELECT last_observed_at FROM external_feed_sources").fetchone()[0]

        twitch = observation_from_twitch_irc_event(
            "@user-id=9;tmi-sent-ts=1786406400000 :alice!alice@alice.tmi.twitch.tv JOIN #room"
        )
        assert twitch is not None and twitch.event_type == "member.joined"
    finally:
        connection.close()


def test_full_text_filters_saved_queries_and_pivots(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    try:
        result = collect_observation(connection, Observation(
            platform="discord", event_type="message.created", external_event_id="search-1",
            actor_platform_user_id="42", actor_username="Analyst", container_id="ops", context_id="guild-a",
            text="Investigate cobalt lantern at https://evidence.example/path",
            occurred_at="2026-08-11T10:00:00+00:00",
        ))
        with connection:
            analyze_observation_content(connection, result.observation_id)
        hits = search_observations(
            connection, query='"cobalt lantern"', start_at="2026-08-11T00:00:00+00:00",
            event_type="message.created", entity_type="domain", entity_value="evidence.example",
        )
        assert [item["id"] for item in hits] == [result.observation_id]
        query_id = save_query(connection, "Cobalt watch", "cobalt", {"context_id": "guild-a"})
        assert query_id > 0
        pivots = observation_pivots(connection, result.observation_id)
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
                actor_platform_user_id=f"topic-user-{index}", actor_username=f"Topic{index}",
                container_id=context, context_id=context,
                text="signal orchid wave https://unusual.example/story",
                occurred_at=f"2026-08-11T0{index}:00:00+00:00",
            ))
            with connection:
                analyze_observation_content(connection, result.observation_id)
        topic_count = refresh_emerging_topics(connection, now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc))
        assert topic_count > 0
        domain = connection.execute("SELECT * FROM emerging_topics WHERE topic_key='domain:unusual.example'").fetchone()
        assert domain is not None and domain["community_count"] == 3 and domain["velocity"] > 0

        users = []
        for name in ("Alpha", "Bridge", "Omega", "Delta", "Echo", "Foxtrot"):
            users.append(int(connection.execute("INSERT INTO users(primary_display_name) VALUES (?)", (name,)).lastrowid))
        for source, target in zip(users, users[1:]):
            connection.execute(
                """INSERT INTO entity_relationships(source_user_id,target_user_id,relationship_type,context_key,
                   strength,evidence_count,first_observed_at,last_observed_at) VALUES (?,?,'mention','g',2,2,'2026-08-10','2026-08-11')""",
                (source, target),
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
                    """INSERT INTO emerging_topics(topic_key,topic_kind,label,current_count,baseline_rate,velocity,
                       context_count,community_count,unusualness,details_json,calculated_at)
                       VALUES (?,?,?,3,1,?,1,?,?,'{}','2026-08-11')""",
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
