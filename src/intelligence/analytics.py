from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .userprofiles import link_platform_account


_TOKENS = re.compile(r"[\w'-]{3,}", re.UNICODE)
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_STOP = {"the", "and", "that", "this", "with", "from", "have", "your", "you", "for", "are", "was", "not", "but", "they", "our", "will", "just", "into", "about", "http", "https", "www"}


def refresh_emerging_topics(connection: sqlite3.Connection, *, now: datetime | None = None) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_start = (current - timedelta(hours=24)).isoformat()
    baseline_start = (current - timedelta(days=8)).isoformat()
    rows = connection.execute(
        """SELECT id, text_raw, context_id, container_id, occurred_at FROM observations
           WHERE occurred_at>=? AND text_raw IS NOT NULL AND trim(text_raw)<>''""", (baseline_start,)
    ).fetchall()
    occurrences: dict[str, list[tuple[int, str, str, str, str]]] = defaultdict(list)
    for row in rows:
        text = str(row[1])
        tokens = [token.casefold() for token in _TOKENS.findall(text) if token.casefold() not in _STOP]
        context = str(row[2] or row[3] or "")
        seen: set[str] = set()
        for token in tokens:
            seen.add(f"term:{token}")
        for left, right in zip(tokens, tokens[1:]):
            seen.add(f"phrase:{left} {right}")
        for match in _URL.finditer(text):
            domain = (urlparse(match.group(0).rstrip(".,;!?)")).hostname or "").casefold().removeprefix("www.")
            if domain:
                seen.add(f"domain:{domain}")
        for key in seen:
            kind, label = key.split(":", 1)
            occurrences[key].append((int(row[0]), context, str(row[4]), kind, label))

    calculated = current.isoformat()
    connection.execute("DELETE FROM emerging_topics")
    connection.execute("DELETE FROM topic_evidence")
    inserted = 0
    for key, items in occurrences.items():
        recent = [item for item in items if item[2] >= current_start]
        if not recent:
            continue
        baseline_count = len(items) - len(recent)
        baseline_rate = baseline_count / 7.0
        velocity = len(recent) - baseline_rate
        contexts = {item[1] for item in recent if item[1]}
        kind, label = key.split(":", 1)
        minimum = 1 if kind == "domain" and len(contexts) > 1 else 2
        if len(recent) < minimum:
            continue
        unusualness = max(0.0, velocity) * math.log2(2 + len(contexts))
        cluster_terms = sorted({
            item.split(":", 1)[1]
            for item in occurrences
            if item.startswith("phrase:")
            and set(label.split()) & set(item.split(":", 1)[1].split())
        })[:12] if kind == "phrase" else []
        connection.execute(
            """INSERT INTO emerging_topics(topic_key, topic_kind, label, current_count, baseline_rate,
               velocity, context_count, community_count, unusualness, first_observed_at, last_observed_at,
               details_json, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, kind, label, len(recent), baseline_rate, velocity, len(contexts), len(contexts), unusualness,
             min(item[2] for item in items), max(item[2] for item in items),
             json.dumps({"cluster_terms": cluster_terms, "cross_community_diffusion": len(contexts) > 1}), calculated),
        )
        connection.execute(
            """INSERT INTO topic_history(topic_key, topic_kind, current_count, baseline_rate, velocity,
               context_count, community_count, unusualness, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, kind, len(recent), baseline_rate, velocity, len(contexts), len(contexts), unusualness, calculated),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO topic_evidence(topic_key, observation_id, context_key, occurred_at) VALUES (?, ?, ?, ?)",
            ((key, item[0], item[1], item[2]) for item in recent[:25]),
        )
        inserted += 1
    return inserted


def refresh_graph_analytics(connection: sqlite3.Connection, *, calculated_at: str | None = None) -> int:
    rows = connection.execute("SELECT source_user_id, target_user_id, strength FROM entity_relationships").fetchall()
    nodes = {int(value) for row in rows for value in row[:2]}
    if not nodes:
        connection.execute("DELETE FROM graph_metrics")
        return 0
    outgoing: dict[int, dict[int, float]] = defaultdict(dict)
    incoming: dict[int, dict[int, float]] = defaultdict(dict)
    undirected: dict[int, set[int]] = defaultdict(set)
    for source, target, strength in rows:
        source, target, weight = int(source), int(target), float(strength)
        outgoing[source][target] = outgoing[source].get(target, 0.0) + weight
        incoming[target][source] = incoming[target].get(source, 0.0) + weight
        undirected[source].add(target); undirected[target].add(source)
    pagerank = {node: 1.0 / len(nodes) for node in nodes}
    for _ in range(30):
        next_rank = {node: 0.15 / len(nodes) for node in nodes}
        dangling = sum(pagerank[node] for node in nodes if not outgoing[node])
        for node in nodes:
            next_rank[node] += 0.85 * dangling / len(nodes)
        for source, targets in outgoing.items():
            total = sum(targets.values())
            for target, weight in targets.items():
                next_rank[target] += 0.85 * pagerank[source] * weight / total
        pagerank = next_rank
    betweenness = _brandes(nodes, undirected)
    bridges = _articulation_points(nodes, undirected)
    clusters = _components(nodes, undirected)
    max_weight = max(sum(outgoing[n].values()) + sum(incoming[n].values()) for n in nodes) or 1.0
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
    connection.execute("DELETE FROM graph_metrics")
    for node in nodes:
        in_degree = sum(incoming[node].values()); out_degree = sum(outgoing[node].values())
        weighted = in_degree + out_degree
        influence = 0.65 * pagerank[node] + 0.35 * weighted / max_weight
        values = (node, in_degree, out_degree, weighted, betweenness[node], pagerank[node], clusters[node], int(node in bridges), influence, timestamp)
        connection.execute("INSERT INTO graph_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        connection.execute("""INSERT INTO graph_metric_history(user_id, in_degree, out_degree, weighted_degree,
            betweenness, pagerank, cluster_id, is_bridge, influence_score, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
    return len(nodes)


def propagation_path(connection: sqlite3.Connection, source_user_id: int, target_user_id: int) -> list[int]:
    adjacency: dict[int, set[int]] = defaultdict(set)
    for row in connection.execute("SELECT source_user_id, target_user_id FROM entity_relationships"):
        adjacency[int(row[0])].add(int(row[1]))
    queue = deque([(source_user_id, [source_user_id])]); seen = {source_user_id}
    while queue:
        node, path = queue.popleft()
        if node == target_user_id:
            return path
        for nxt in adjacency[node] - seen:
            seen.add(nxt); queue.append((nxt, path + [nxt]))
    return []


def refresh_identity_suggestions(connection: sqlite3.Connection, *, minimum_confidence: float = 0.55) -> int:
    accounts = connection.execute(
        "SELECT id, platform, platform_user_id, username, user_id, guild_or_channel_context FROM platform_accounts ORDER BY id"
    ).fetchall()
    inserted = 0
    for index, left in enumerate(accounts):
        for right in accounts[index + 1:]:
            if left[1] == right[1] or (left[4] is not None and left[4] == right[4]):
                continue
            name_score = SequenceMatcher(None, _identity_name(str(left[3])), _identity_name(str(right[3]))).ratio()
            id_score = SequenceMatcher(None, _identity_name(str(left[2])), _identity_name(str(right[2]))).ratio()
            context_match = bool(left[5] and right[5] and str(left[5]).casefold() == str(right[5]).casefold())
            confidence = min(0.99, 0.55 * name_score + 0.30 * id_score + 0.15 * int(context_match))
            if confidence < minimum_confidence:
                continue
            evidence = {"username_similarity": round(name_score, 4), "identifier_similarity": round(id_score, 4),
                        "shared_context": context_match, "manual_approval_required": True}
            cursor = connection.execute(
                """INSERT INTO identity_link_suggestions(left_platform_account_id, right_platform_account_id,
                   confidence, evidence_json, model_version) VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(left_platform_account_id, right_platform_account_id, model_version) DO UPDATE SET
                   confidence=excluded.confidence, evidence_json=excluded.evidence_json, updated_at=CURRENT_TIMESTAMP
                   WHERE identity_link_suggestions.status='pending'""",
                (int(left[0]), int(right[0]), confidence, json.dumps(evidence, sort_keys=True)),
            )
            inserted += int(cursor.rowcount > 0)
    return inserted


def review_identity_suggestion(connection: sqlite3.Connection, suggestion_id: int, decision: str, *, operator_id: int | None = None) -> None:
    decision = decision.strip().casefold()
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    row = connection.execute(
        """SELECT s.status, l.platform, l.platform_user_id, l.user_id, r.platform, r.platform_user_id, r.user_id
           FROM identity_link_suggestions s JOIN platform_accounts l ON l.id=s.left_platform_account_id
           JOIN platform_accounts r ON r.id=s.right_platform_account_id WHERE s.id=?""", (suggestion_id,),
    ).fetchone()
    if row is None or row[0] != "pending":
        raise ValueError("pending suggestion not found")
    if decision == "approved":
        if row[3] is not None:
            target_user, platform, platform_user_id = int(row[3]), str(row[4]), str(row[5])
        elif row[6] is not None:
            target_user, platform, platform_user_id = int(row[6]), str(row[1]), str(row[2])
        else:
            raise ValueError("accounts must have a canonical user before approval")
        link_platform_account(connection, platform=platform, platform_user_id=platform_user_id, user_id=target_user, operator_id=operator_id)
    connection.execute(
        "UPDATE identity_link_suggestions SET status=?, reviewed_by_operator_id=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (decision, operator_id, suggestion_id),
    )


def refresh_cohort_baselines(connection: sqlite3.Connection, *, calculated_at: str | None = None) -> tuple[int, int]:
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
    signals = connection.execute(
        "SELECT user_id, signal_key, value_real, confidence FROM derived_signal_windows WHERE window_name='24h'"
    ).fetchall()
    current_values = {(int(row[0]), str(row[1])): (float(row[2]), float(row[3])) for row in signals}
    memberships: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for user_id, platform in connection.execute("SELECT DISTINCT user_id, platform FROM platform_accounts WHERE user_id IS NOT NULL"):
        memberships[int(user_id)].add(("platform", str(platform)))
    for user_id, context in connection.execute(
        """SELECT DISTINCT account.user_id, COALESCE(o.context_id,o.container_id)
           FROM observations o JOIN platform_accounts account ON account.id=o.actor_platform_account_id
           WHERE account.user_id IS NOT NULL AND COALESCE(o.context_id,o.container_id) IS NOT NULL"""
    ):
        memberships[int(user_id)].add(("community", str(context)))
    cohorts: dict[tuple[str, str, str], list[tuple[int, float, float]]] = defaultdict(list)
    for (user_id, signal), (value, confidence) in current_values.items():
        for cohort_type, cohort_key in memberships[user_id]:
            cohorts[(cohort_type, cohort_key, signal)].append((user_id, value, confidence))
    connection.execute("DELETE FROM cohort_baselines")
    connection.execute("DELETE FROM cohort_anomalies")
    baselines = anomalies = 0
    for (cohort_type, cohort_key, signal), values in cohorts.items():
        numbers = [value for _, value, _ in values]
        mean = statistics.fmean(numbers); stddev = statistics.pstdev(numbers) if len(numbers) > 1 else 0.0
        ordered = sorted(numbers); p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        connection.execute(
            """INSERT INTO cohort_baselines(cohort_type, cohort_key, signal_key, sample_size, mean_value,
               stddev_value, median_value, p90_value, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cohort_type, cohort_key, signal, len(numbers), mean, stddev, statistics.median(numbers), p90, timestamp),
        ); baselines += 1
        if len(numbers) < 3 or stddev == 0:
            continue
        for user_id, value, confidence in values:
            z = (value - mean) / stddev
            if abs(z) < 1.25:
                continue
            connection.execute(
                """INSERT INTO cohort_anomalies(user_id, cohort_type, cohort_key, signal_key, observed_value,
                   baseline_mean, z_score, direction, confidence, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, cohort_type, cohort_key, signal, value, mean, z, "above" if z > 0 else "below", confidence, timestamp),
            ); anomalies += 1

    histories: dict[tuple[int, str], list[float]] = defaultdict(list)
    for user_id, signal, value in connection.execute(
        "SELECT user_id, signal_key, value_real FROM derived_signal_history WHERE window_name='24h' ORDER BY calculated_at"
    ):
        histories[(int(user_id), str(signal))].append(float(value))
    for (user_id, signal), numbers in histories.items():
        if len(numbers) < 3 or (user_id, signal) not in current_values:
            continue
        mean = statistics.fmean(numbers); stddev = statistics.pstdev(numbers)
        ordered = sorted(numbers); p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        connection.execute(
            """INSERT INTO cohort_baselines(cohort_type, cohort_key, signal_key, sample_size, mean_value,
               stddev_value, median_value, p90_value, calculated_at) VALUES ('self', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id), signal, len(numbers), mean, stddev, statistics.median(numbers), p90, timestamp),
        ); baselines += 1
        value, confidence = current_values[(user_id, signal)]
        if stddev:
            z = (value - mean) / stddev
            if abs(z) >= 1.25:
                connection.execute(
                    """INSERT INTO cohort_anomalies(user_id, cohort_type, cohort_key, signal_key, observed_value,
                       baseline_mean, z_score, direction, confidence, calculated_at) VALUES (?, 'self', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, str(user_id), signal, value, mean, z, "above" if z > 0 else "below", confidence, timestamp),
                ); anomalies += 1
    return baselines, anomalies


def record_evaluation_label(connection: sqlite3.Connection, *, label_key: str, label_value: str,
                            observation_id: int | None = None, alert_id: int | None = None,
                            user_id: int | None = None, operator_id: int | None = None,
                            source: str = "operator") -> int:
    if alert_id is not None:
        previous = connection.execute(
            "SELECT id FROM evaluation_labels WHERE alert_id=? AND label_key=? AND source=? ORDER BY id DESC LIMIT 1",
            (alert_id, label_key.strip(), source),
        ).fetchone()
        if previous is not None:
            connection.execute(
                """UPDATE evaluation_labels SET observation_id=?, user_id=?, label_value=?, operator_id=?, created_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (observation_id, user_id, label_value.strip().casefold(), operator_id, int(previous[0])),
            )
            return int(previous[0])
    cursor = connection.execute(
        """INSERT INTO evaluation_labels(observation_id, alert_id, user_id, label_key, label_value,
           operator_id, source) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (observation_id, alert_id, user_id, label_key.strip(), label_value.strip().casefold(), operator_id, source),
    )
    return int(cursor.lastrowid)


def run_model_evaluation(connection: sqlite3.Connection, *, model_key: str = "risk.composite", model_version: int = 2) -> int:
    rows = connection.execute(
        """SELECT l.label_value, COALESCE(h.value_real, d.value_real, u.current_reputation_score) score,
                  a.alert_type FROM evaluation_labels l
           LEFT JOIN intelligence_alerts a ON a.id=l.alert_id
           LEFT JOIN derived_signal_history h ON h.id=a.signal_history_id AND h.signal_key=?
           LEFT JOIN derived_signals d ON d.user_id=COALESCE(l.user_id,a.user_id) AND d.signal_key=?
           LEFT JOIN users u ON u.id=COALESCE(l.user_id,a.user_id)
           WHERE l.label_value IN ('positive','negative')""", (model_key, model_key),
    ).fetchall()
    thresholds = (25.0, 50.0, 75.0)
    backtests = []
    for threshold in thresholds:
        tp = fp = tn = fn = 0
        for label, score, _ in rows:
            predicted = float(score or 0) >= threshold
            positive = str(label) == "positive"
            tp += predicted and positive; fp += predicted and not positive
            tn += (not predicted) and (not positive); fn += (not predicted) and positive
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        fpr = fp / (fp + tn) if fp + tn else 0.0
        backtests.append((threshold, tp, fp, tn, fn, precision, recall, fpr))
    false_positive_types = Counter(str(row[2] or "untyped") for row in rows if row[0] == "negative" and float(row[1] or 0) >= 50)
    score_values = [float(row[0]) for row in connection.execute("SELECT current_reputation_score FROM users")]
    distribution = {f"{low}-{low+24}": sum(low <= value < low + 25 for value in score_values) for low in range(0, 100, 25)}
    current = next(item for item in backtests if item[0] == 50.0)
    metrics = {"precision": current[5], "recall": current[6], "false_positive_rate": current[7],
               "false_positive_alert_types": dict(false_positive_types), "labeled_positive": sum(row[0] == "positive" for row in rows),
               "labeled_negative": sum(row[0] == "negative" for row in rows)}
    cursor = connection.execute(
        """INSERT INTO model_evaluation_runs(model_key, model_version, sample_size, metrics_json,
           score_distribution_json, calculated_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (model_key, model_version, len(rows), json.dumps(metrics, sort_keys=True), json.dumps(distribution, sort_keys=True), datetime.now(timezone.utc).isoformat()),
    )
    run_id = int(cursor.lastrowid)
    connection.executemany(
        """INSERT INTO threshold_backtests(evaluation_run_id, threshold, true_positive, false_positive,
           true_negative, false_negative, precision, recall, false_positive_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ((run_id, *item) for item in backtests),
    )
    return run_id


def analytics_snapshot(
    connection: sqlite3.Connection,
    *,
    sorts: dict[str, tuple[str, str]] | None = None,
) -> dict[str, object]:
    sorts = sorts or {}

    def order_clause(table: str, default: tuple[str, str], columns: dict[str, str], tie_breaker: str) -> str:
        sort_by, sort_dir = sorts.get(table, default)
        column = columns.get(sort_by, columns[default[0]])
        direction = "ASC" if sort_dir == "asc" else "DESC"
        return f"{column} {direction}, {tie_breaker}"

    topic_order = order_clause("topics", ("unusualness", "desc"), {
        "topic_kind": "topic_kind COLLATE NOCASE", "label": "label COLLATE NOCASE",
        "velocity": "velocity", "community_count": "community_count", "unusualness": "unusualness",
    }, "id DESC")
    graph_order = order_clause("graph", ("influence_score", "desc"), {
        "user_id": "user_id", "pagerank": "pagerank", "betweenness": "betweenness",
        "is_bridge": "is_bridge", "cluster_id": "cluster_id", "influence_score": "influence_score",
    }, "user_id ASC")
    identity_order = order_clause("identity_suggestions", ("confidence", "desc"), {
        "id": "id", "left_platform_account_id": "left_platform_account_id",
        "right_platform_account_id": "right_platform_account_id", "confidence": "confidence",
        "status": "status COLLATE NOCASE",
    }, "id DESC")
    cohort_order = order_clause("cohort_anomalies", ("z_score", "desc"), {
        "user_id": "user_id", "cohort_key": "cohort_key COLLATE NOCASE",
        "signal_key": "signal_key COLLATE NOCASE", "z_score": "z_score",
        "direction": "direction COLLATE NOCASE", "confidence": "confidence",
    }, "user_id ASC")
    evaluation_order = order_clause("evaluation", ("calculated_at", "desc"), {
        "model_key": "model_key COLLATE NOCASE", "model_version": "model_version",
        "sample_size": "sample_size", "calculated_at": "calculated_at",
    }, "id DESC")
    return {
        "topics": [dict(row) for row in connection.execute("SELECT * FROM emerging_topics ORDER BY " + topic_order + " LIMIT 25")],
        "graph": [dict(row) for row in connection.execute("SELECT * FROM graph_metrics ORDER BY " + graph_order + " LIMIT 25")],
        "graph_temporal_changes": graph_temporal_changes(connection),
        "identity_suggestions": [dict(row) for row in connection.execute("SELECT * FROM identity_link_suggestions ORDER BY " + identity_order + " LIMIT 25")],
        "cohort_anomalies": [dict(row) for row in connection.execute("SELECT * FROM cohort_anomalies ORDER BY " + cohort_order + " LIMIT 25")],
        "evaluation": [dict(row) for row in connection.execute("SELECT * FROM model_evaluation_runs ORDER BY " + evaluation_order + " LIMIT 25")],
    }


def graph_temporal_changes(connection: sqlite3.Connection, *, limit: int = 25) -> list[dict[str, object]]:
    rows = connection.execute(
        """WITH ranked AS (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY calculated_at DESC, id DESC) AS rn
             FROM graph_metric_history
           )
           SELECT current.user_id, current.influence_score,
                  current.influence_score-previous.influence_score AS influence_delta,
                  current.weighted_degree-previous.weighted_degree AS degree_delta,
                  current.cluster_id, previous.cluster_id AS previous_cluster_id,
                  current.is_bridge, previous.is_bridge AS previous_is_bridge
           FROM ranked current JOIN ranked previous ON previous.user_id=current.user_id AND previous.rn=2
           WHERE current.rn=1 ORDER BY ABS(current.influence_score-previous.influence_score) DESC LIMIT ?""",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _identity_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _components(nodes: set[int], graph: dict[int, set[int]]) -> dict[int, int]:
    result: dict[int, int] = {}; cluster = 0
    for start in nodes:
        if start in result: continue
        cluster += 1; stack = [start]; result[start] = cluster
        while stack:
            node = stack.pop()
            for nxt in graph[node]:
                if nxt not in result: result[nxt] = cluster; stack.append(nxt)
    return result


def _brandes(nodes: set[int], graph: dict[int, set[int]]) -> dict[int, float]:
    centrality = {node: 0.0 for node in nodes}
    for source in nodes:
        stack: list[int] = []; parents = {node: [] for node in nodes}; sigma = dict.fromkeys(nodes, 0.0); sigma[source] = 1.0
        distance = dict.fromkeys(nodes, -1); distance[source] = 0; queue = deque([source])
        while queue:
            node = queue.popleft(); stack.append(node)
            for nxt in graph[node]:
                if distance[nxt] < 0: queue.append(nxt); distance[nxt] = distance[node] + 1
                if distance[nxt] == distance[node] + 1: sigma[nxt] += sigma[node]; parents[nxt].append(node)
        dependency = dict.fromkeys(nodes, 0.0)
        while stack:
            child = stack.pop()
            for parent in parents[child]:
                dependency[parent] += (sigma[parent] / sigma[child]) * (1 + dependency[child])
            if child != source: centrality[child] += dependency[child]
    scale = max(1, (len(nodes) - 1) * (len(nodes) - 2))
    return {node: value / scale for node, value in centrality.items()}


def _articulation_points(nodes: set[int], graph: dict[int, set[int]]) -> set[int]:
    time = 0; discovered: dict[int, int] = {}; low: dict[int, int] = {}; parent: dict[int, int | None] = {}; result: set[int] = set()
    def visit(node: int) -> None:
        nonlocal time
        time += 1; discovered[node] = low[node] = time; children = 0
        for nxt in graph[node]:
            if nxt not in discovered:
                parent[nxt] = node; children += 1; visit(nxt); low[node] = min(low[node], low[nxt])
                if parent.get(node) is None and children > 1: result.add(node)
                if parent.get(node) is not None and low[nxt] >= discovered[node]: result.add(node)
            elif nxt != parent.get(node): low[node] = min(low[node], discovered[nxt])
    for node in nodes:
        if node not in discovered: parent[node] = None; visit(node)
    return result
