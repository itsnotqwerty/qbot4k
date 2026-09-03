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

from .policy import (
    AUTO_EXPIRED_DISPOSITION,
    COHORT_MIN_CONFIDENCE,
    COHORT_MIN_SAMPLE_SIZE,
    COORDINATION_ALERT_MIN_EVIDENCE,
    TOPIC_ALERT_LIMIT,
    TOPIC_ALERT_POLICIES,
    TOPIC_BASELINE_MIN_ACTIVE_DAYS,
)
from .userprofiles import link_platform_account


_TOKENS = re.compile(r"[\w'-]{3,}", re.UNICODE)
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_STOP = {"the", "and", "that", "this", "with", "from", "have", "your", "you", "for", "are", "was", "not", "but", "they", "our", "will", "just", "into", "about", "http", "https", "www"}

def refresh_emerging_topics(
    connection: sqlite3.Connection, *, community_id: int, now: datetime | None = None
) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_start = (current - timedelta(hours=24)).isoformat()
    baseline_start = (current - timedelta(days=8)).isoformat()
    rows = connection.execute(
          """SELECT id, text_raw, context_id, container_id, occurred_at FROM observations
              WHERE community_id=? AND occurred_at>=?
                 AND text_raw IS NOT NULL AND trim(text_raw)<>''""",
          (community_id, baseline_start),
    ).fetchall()
    occurrences: dict[str, list[tuple[int, str, str, str, str]]] = defaultdict(list)
    for row in rows:
        text = str(row[1])
        tokens = [token.casefold() for token in _TOKENS.findall(text) if token.casefold() not in _STOP]
        # A channel/container is the useful local diffusion boundary. Sources
        # without one fall back to their broader guild/community context.
        context = str(row[3] or row[2] or "")
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
    connection.execute("DELETE FROM emerging_topics WHERE community_id=?", (community_id,))
    connection.execute("DELETE FROM topic_evidence WHERE community_id=?", (community_id,))
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
        stored_key = key if community_id == 1 else f"{community_id}:{key}"
        connection.execute(
            """INSERT INTO emerging_topics(community_id, topic_key, topic_kind, label, current_count, baseline_rate,
               velocity, context_count, community_count, unusualness, first_observed_at, last_observed_at,
               details_json, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (community_id, stored_key, kind, label, len(recent), baseline_rate, velocity,
             len(contexts), len(contexts), unusualness, min(item[2] for item in items),
             max(item[2] for item in items), json.dumps({
                 "cluster_terms": cluster_terms,
                 "cross_community_diffusion": len(contexts) > 1,
             }), calculated),
        )
        connection.execute(
            """INSERT INTO topic_history(community_id, topic_key, topic_kind, current_count, baseline_rate, velocity,
               context_count, community_count, unusualness, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (community_id, stored_key, kind, len(recent), baseline_rate, velocity,
             len(contexts), len(contexts), unusualness, calculated),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO topic_evidence(
                   community_id, topic_key, observation_id, context_key, occurred_at
               ) VALUES (?, ?, ?, ?, ?)""",
            ((community_id, stored_key, item[0], item[1], item[2]) for item in recent[:25]),
        )
        inserted += 1
    return inserted


def refresh_graph_analytics(
    connection: sqlite3.Connection, *, calculated_at: str | None = None,
    community_id: int | None = None,
) -> int:
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
    reference_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    relationship_scope = "" if community_id is None else " WHERE community_id=?"
    relationship_params: tuple[object, ...] = () if community_id is None else (community_id,)
    rows = connection.execute(
        "SELECT source_user_id, target_user_id, strength, last_observed_at "
        "FROM entity_relationships" + relationship_scope,
        relationship_params,
    ).fetchall()
    metrics_table = "graph_metrics" if community_id is None else "community_graph_metrics"
    history_table = "graph_metric_history" if community_id is None else "community_graph_metric_history"
    delete_params: tuple[object, ...] = () if community_id is None else (community_id,)
    delete_scope = "" if community_id is None else " WHERE community_id=?"
    nodes = {int(value) for row in rows for value in row[:2]}
    if not nodes:
        connection.execute(f"DELETE FROM {metrics_table}{delete_scope}", delete_params)
        return 0
    outgoing: dict[int, dict[int, float]] = defaultdict(dict)
    incoming: dict[int, dict[int, float]] = defaultdict(dict)
    undirected: dict[int, set[int]] = defaultdict(set)
    weighted_neighbors: dict[int, dict[int, float]] = defaultdict(dict)
    for source, target, strength, last_observed_at in rows:
        source, target = int(source), int(target)
        try:
            observed = datetime.fromisoformat(str(last_observed_at).replace("Z", "+00:00")).astimezone(timezone.utc)
            age_days = max(0.0, (reference_time - observed).total_seconds() / 86400.0)
        except ValueError:
            age_days = 0.0
        weight = float(strength) * math.exp(-math.log(2) * age_days / 30.0)
        outgoing[source][target] = outgoing[source].get(target, 0.0) + weight
        incoming[target][source] = incoming[target].get(source, 0.0) + weight
        undirected[source].add(target); undirected[target].add(source)
        weighted_neighbors[source][target] = weighted_neighbors[source].get(target, 0.0) + weight
        weighted_neighbors[target][source] = weighted_neighbors[target].get(source, 0.0) + weight
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
    clusters = _label_propagation(nodes, weighted_neighbors)
    max_weight = max(sum(outgoing[n].values()) + sum(incoming[n].values()) for n in nodes) or 1.0
    connection.execute(f"DELETE FROM {metrics_table}{delete_scope}", delete_params)
    for node in nodes:
        in_degree = sum(incoming[node].values()); out_degree = sum(outgoing[node].values())
        weighted = in_degree + out_degree
        influence = 0.65 * pagerank[node] + 0.35 * weighted / max_weight
        values = (node, in_degree, out_degree, weighted, betweenness[node], pagerank[node], clusters[node], int(node in bridges), influence, timestamp)
        if community_id is None:
            connection.execute("INSERT INTO graph_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            connection.execute("""INSERT INTO graph_metric_history(user_id, in_degree, out_degree, weighted_degree,
                betweenness, pagerank, cluster_id, is_bridge, influence_score, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
        else:
            scoped_values = (community_id, *values)
            connection.execute("""INSERT INTO community_graph_metrics(
                community_id,user_id,in_degree,out_degree,weighted_degree,betweenness,pagerank,
                cluster_id,is_bridge,influence_score,calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", scoped_values)
            connection.execute("""INSERT INTO community_graph_metric_history(
                community_id,user_id,in_degree,out_degree,weighted_degree,betweenness,pagerank,
                cluster_id,is_bridge,influence_score,calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", scoped_values)
    return len(nodes)


def propagation_path(connection: sqlite3.Connection, source_user_id: int, target_user_id: int) -> list[int]:
    adjacency: dict[int, list[tuple[int, str]]] = defaultdict(list)
    evidence_rows = connection.execute(
        """SELECT r.source_user_id,r.target_user_id,e.occurred_at
           FROM relationship_evidence e JOIN entity_relationships r ON r.id=e.relationship_id
           ORDER BY e.occurred_at,e.observation_id"""
    ).fetchall()
    if evidence_rows:
        for source, target, occurred_at in evidence_rows:
            adjacency[int(source)].append((int(target), str(occurred_at)))
    else:
        for source, target, occurred_at in connection.execute(
            "SELECT source_user_id,target_user_id,last_observed_at FROM entity_relationships"
        ):
            adjacency[int(source)].append((int(target), str(occurred_at)))
    queue = deque([(source_user_id, [source_user_id], "")])
    best_time: dict[int, str] = {source_user_id: ""}
    while queue:
        node, path, prior_time = queue.popleft()
        if node == target_user_id:
            return path
        for nxt, occurred_at in adjacency[node]:
            if occurred_at < prior_time:
                continue
            if nxt in path:
                continue
            if nxt in best_time and best_time[nxt] <= occurred_at:
                continue
            best_time[nxt] = occurred_at
            queue.append((nxt, path + [nxt], occurred_at))
    return []


def refresh_identity_suggestions(
    connection: sqlite3.Connection, *, minimum_confidence: float = 0.55,
    community_id: int | None = None,
) -> int:
    account_scope = "" if community_id is None else """
        WHERE EXISTS (
            SELECT 1 FROM messages
            WHERE messages.platform_account_id=platform_accounts.id
              AND messages.community_id=?
        )
    """
    accounts = connection.execute(
        "SELECT id, platform, platform_user_id, username, user_id, guild_or_channel_context "
        "FROM platform_accounts" + account_scope + " ORDER BY id",
        () if community_id is None else (community_id,),
    ).fetchall()
    inserted = 0
    for index, left in enumerate(accounts):
        for right in accounts[index + 1:]:
            if left[1] == right[1] or (left[4] is not None and left[4] == right[4]):
                continue
            left_name = _identity_name(str(left[3]))
            right_name = _identity_name(str(right[3]))
            context_match = bool(left[5] and right[5] and str(left[5]).casefold() == str(right[5]).casefold())
            # Candidate blocking avoids an unbounded all-pairs comparison while retaining
            # exact-prefix and shared-context candidates for analyst review.
            if not context_match and (
                not left_name or not right_name or left_name[0] != right_name[0]
                or abs(len(left_name) - len(right_name)) > 4
            ):
                continue
            name_score = SequenceMatcher(None, left_name, right_name).ratio()
            id_score = SequenceMatcher(None, _identity_name(str(left[2])), _identity_name(str(right[2]))).ratio()
            confidence = min(0.99, 0.55 * name_score + 0.30 * id_score + 0.15 * int(context_match))
            if confidence < minimum_confidence:
                continue
            evidence = {"username_similarity": round(name_score, 4), "identifier_similarity": round(id_score, 4),
                        "shared_context": context_match, "manual_approval_required": True}
            if community_id is None:
                cursor = connection.execute(
                    """INSERT INTO identity_link_suggestions(left_platform_account_id, right_platform_account_id,
                       confidence, evidence_json, model_version) VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(left_platform_account_id, right_platform_account_id, model_version) DO UPDATE SET
                       confidence=excluded.confidence, evidence_json=excluded.evidence_json, updated_at=CURRENT_TIMESTAMP
                       WHERE identity_link_suggestions.status='pending'""",
                    (int(left[0]), int(right[0]), confidence, json.dumps(evidence, sort_keys=True)),
                )
            else:
                cursor = connection.execute(
                    """INSERT INTO community_identity_link_suggestions(
                           community_id,left_platform_account_id,right_platform_account_id,
                           confidence,evidence_json,model_version
                       ) VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(community_id,left_platform_account_id,right_platform_account_id,model_version)
                       DO UPDATE SET confidence=excluded.confidence,
                           evidence_json=excluded.evidence_json,updated_at=CURRENT_TIMESTAMP
                       WHERE community_identity_link_suggestions.status='pending'""",
                    (community_id, int(left[0]), int(right[0]), confidence,
                     json.dumps(evidence, sort_keys=True)),
                )
            inserted += int(cursor.rowcount > 0)
    return inserted


def emit_analytics_alerts(
    connection: sqlite3.Connection, *, community_id: int, calculated_at: str | None = None
) -> int:
    """Promote material findings into bounded, stable, self-expiring triage work."""
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
    created = 0

    topic_rows = _qualifying_topic_alerts(
        connection, community_id
    ) if _topic_baseline_ready(connection, community_id, timestamp) else []
    topic_keys = {f"topic:{row['topic_key']}" for row in topic_rows}
    _auto_expire_alerts(connection, community_id, "emerging_topic", topic_keys, timestamp)
    for row in topic_rows:
        unusualness = float(row["unusualness"])
        created += _upsert_stable_analytics_alert(
            connection,
            community_id=community_id,
            dedupe_key=f"topic:{row['topic_key']}",
            alert_type="emerging_topic",
            severity="high" if unusualness >= 18 else "medium",
            title="Emerging Topic",
            summary=(f"{row['label']} rose to {row['current_count']} observations across "
                     f"{row['context_count']} contexts (unusualness {unusualness:.2f})."),
            confidence=min(0.99, unusualness / 20.0),
            observation_id=int(row["observation_id"]) if row["observation_id"] is not None else None,
            timestamp=timestamp,
        )

    cohort_rows = connection.execute(
        """SELECT a.user_id,a.cohort_type,a.cohort_key,a.signal_key,a.z_score,a.confidence
           FROM cohort_anomalies a
           JOIN cohort_baselines b ON b.cohort_type=a.cohort_type
             AND b.cohort_key=a.cohort_key AND b.signal_key=a.signal_key
           WHERE abs(a.z_score)>=3 AND a.confidence>=? AND b.sample_size>=?
           ORDER BY abs(a.z_score) DESC,a.confidence DESC""",
        (COHORT_MIN_CONFIDENCE, COHORT_MIN_SAMPLE_SIZE),
    ).fetchall()
    cohort_keys = {
        f"cohort:{row['user_id']}:{row['cohort_type']}:{row['cohort_key']}:{row['signal_key']}"
        for row in cohort_rows
    }
    _auto_expire_alerts(connection, community_id, "cohort_anomaly", cohort_keys, timestamp)
    for row in cohort_rows:
        created += _upsert_stable_analytics_alert(
            connection,
            community_id=community_id,
            dedupe_key=(f"cohort:{row['user_id']}:{row['cohort_type']}:"
                        f"{row['cohort_key']}:{row['signal_key']}"),
            alert_type="cohort_anomaly",
            severity="high",
            title="Cohort Anomaly",
            summary=(f"{row['signal_key']} deviates {float(row['z_score']):.2f}σ from "
                     f"{row['cohort_type']}:{row['cohort_key']} peers."),
            confidence=float(row["confidence"]),
            user_id=int(row["user_id"]),
            timestamp=timestamp,
        )

    graph_rows = connection.execute(
        """SELECT user_id,influence_score FROM graph_metrics
           WHERE is_bridge=1 AND influence_score>=0.45
           ORDER BY influence_score DESC"""
    ).fetchall()
    graph_keys = {f"graph-bridge:{row['user_id']}" for row in graph_rows}
    _auto_expire_alerts(connection, community_id, "graph_bridge", graph_keys, timestamp)
    for row in graph_rows:
        created += _upsert_stable_analytics_alert(
            connection,
            community_id=community_id,
            dedupe_key=f"graph-bridge:{row['user_id']}",
            alert_type="graph_bridge",
            severity="medium",
            title="Network Bridge",
            summary=f"Entity is a high-influence bridge ({float(row['influence_score']):.3f}).",
            confidence=min(0.95, float(row["influence_score"])),
            user_id=int(row["user_id"]),
            timestamp=timestamp,
        )

    coordination_rows = connection.execute(
        """SELECT id FROM entity_relationships
              WHERE community_id=? AND evidence_count>=? ORDER BY evidence_count DESC,id""",
          (int(community_id), COORDINATION_ALERT_MIN_EVIDENCE),
    ).fetchall()
    _auto_expire_alerts(
        connection,
        community_id,
        "coordination_pattern",
        {f"relationship:{row['id']}:coordination" for row in coordination_rows},
        timestamp,
    )
    return created


def _topic_baseline_ready(
    connection: sqlite3.Connection, community_id: int, timestamp: str
) -> bool:
    current = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    baseline_start = (current - timedelta(days=8)).isoformat()
    current_start = (current - timedelta(hours=24)).isoformat()
    active_days = int(connection.execute(
        """SELECT COUNT(DISTINCT date(occurred_at)) FROM observations
           WHERE occurred_at>=? AND occurred_at<?
           AND community_id=?
             AND text_raw IS NOT NULL AND trim(text_raw)<>''""",
       (baseline_start, current_start, int(community_id)),
    ).fetchone()[0])
    return active_days >= TOPIC_BASELINE_MIN_ACTIVE_DAYS


def _qualifying_topic_alerts(
    connection: sqlite3.Connection, community_id: int
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    parameters: list[object] = []
    for kind, (minimum_count, minimum_contexts, minimum_unusualness) in TOPIC_ALERT_POLICIES.items():
        clauses.append("(t.topic_kind=? AND t.current_count>=? AND t.context_count>=? AND t.unusualness>=?)")
        parameters.extend((kind, minimum_count, minimum_contexts, minimum_unusualness))
    return list(connection.execute(
        f"""SELECT t.topic_key,t.topic_kind,t.label,t.unusualness,t.current_count,t.context_count,
                   (SELECT e.observation_id FROM topic_evidence e WHERE e.topic_key=t.topic_key
                    AND e.community_id=t.community_id
                    ORDER BY e.occurred_at DESC,e.observation_id DESC LIMIT 1) observation_id
                FROM emerging_topics t WHERE t.community_id=? AND ({' OR '.join(clauses)})
            ORDER BY t.unusualness DESC,t.current_count DESC,t.topic_key
            LIMIT ?""",
        (int(community_id), *parameters, TOPIC_ALERT_LIMIT),
    ).fetchall())


def _auto_expire_alerts(
    connection: sqlite3.Connection,
    community_id: int,
    alert_type: str,
    active_dedupe_keys: set[str],
    timestamp: str,
) -> int:
    parameters: list[object] = [AUTO_EXPIRED_DISPOSITION, timestamp, timestamp, int(community_id), alert_type]
    exclusion = ""
    if active_dedupe_keys:
        exclusion = f" AND dedupe_key NOT IN ({','.join('?' for _ in active_dedupe_keys)})"
        parameters.extend(sorted(active_dedupe_keys))
    cursor = connection.execute(
        """UPDATE intelligence_alerts
           SET status='resolved',disposition=?,resolved_at=?,updated_at=?
                     WHERE community_id=? AND alert_type=?
                         AND status IN ('open','acknowledged','suppressed')""" + exclusion,
        parameters,
    )
    expired = int(cursor.rowcount)
    if expired:
        connection.execute(
            """INSERT INTO audit_log(actor_type,action_type,entity_type,payload_json)
               VALUES ('system','alert.auto_expired','intelligence_alert',?)""",
            (json.dumps({"alert_type": alert_type, "count": expired}, sort_keys=True),),
        )
    return expired


def _upsert_stable_analytics_alert(
    connection: sqlite3.Connection,
    *,
    community_id: int,
    dedupe_key: str,
    alert_type: str,
    severity: str,
    title: str,
    summary: str,
    confidence: float,
    timestamp: str,
    user_id: int | None = None,
    observation_id: int | None = None,
) -> int:
    existed = connection.execute(
        "SELECT 1 FROM intelligence_alerts WHERE community_id=? AND dedupe_key=?",
        (int(community_id), dedupe_key),
    ).fetchone() is not None
    connection.execute(
        """INSERT INTO intelligence_alerts(
                         community_id,user_id,observation_id,alert_type,severity,title,summary,confidence,dedupe_key,
             created_at,updated_at
                     ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(dedupe_key) DO UPDATE SET
             user_id=COALESCE(excluded.user_id,intelligence_alerts.user_id),
             observation_id=COALESCE(excluded.observation_id,intelligence_alerts.observation_id),
             severity=excluded.severity,title=excluded.title,summary=excluded.summary,
             confidence=excluded.confidence,
             status=CASE WHEN intelligence_alerts.status='resolved'
                               AND intelligence_alerts.disposition=? THEN 'open'
                         ELSE intelligence_alerts.status END,
             disposition=CASE WHEN intelligence_alerts.status='resolved'
                                    AND intelligence_alerts.disposition=? THEN NULL
                              ELSE intelligence_alerts.disposition END,
             resolved_at=CASE WHEN intelligence_alerts.status='resolved'
                                   AND intelligence_alerts.disposition=? THEN NULL
                              ELSE intelligence_alerts.resolved_at END,
             updated_at=excluded.updated_at""",
        (int(community_id), user_id, observation_id, alert_type, severity, title, summary, confidence,
         dedupe_key, timestamp, timestamp, AUTO_EXPIRED_DISPOSITION,
         AUTO_EXPIRED_DISPOSITION, AUTO_EXPIRED_DISPOSITION),
    )
    return 0 if existed else 1


def review_identity_suggestion(
    connection: sqlite3.Connection, suggestion_id: int, decision: str, *,
    operator_id: int | None = None, community_id: int | None = None,
) -> None:
    decision = decision.strip().casefold()
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    suggestions_table = (
        "identity_link_suggestions" if community_id is None
        else "community_identity_link_suggestions"
    )
    tenant_scope = "" if community_id is None else " AND s.community_id=?"
    row = connection.execute(
        f"""SELECT s.status, l.platform, l.platform_user_id, l.user_id, r.platform, r.platform_user_id, r.user_id
           FROM {suggestions_table} s JOIN platform_accounts l ON l.id=s.left_platform_account_id
           JOIN platform_accounts r ON r.id=s.right_platform_account_id
           WHERE s.id=?{tenant_scope}""",
        (suggestion_id,) if community_id is None else (suggestion_id, community_id),
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
        f"""UPDATE {suggestions_table} SET status=?, reviewed_by_operator_id=?,
            reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
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
        if len(numbers) < COHORT_MIN_SAMPLE_SIZE or stddev == 0:
            continue
        for user_id, value, confidence in values:
            if confidence < COHORT_MIN_CONFIDENCE:
                continue
            peer_numbers = [peer_value for peer_id, peer_value, _ in values if peer_id != user_id]
            if len(peer_numbers) < COHORT_MIN_SAMPLE_SIZE - 1:
                continue
            peer_mean = statistics.fmean(peer_numbers)
            peer_stddev = statistics.pstdev(peer_numbers)
            if peer_stddev == 0:
                continue
            z = (value - peer_mean) / peer_stddev
            if abs(z) < 1.25:
                continue
            connection.execute(
                """INSERT INTO cohort_anomalies(user_id, cohort_type, cohort_key, signal_key, observed_value,
                   baseline_mean, z_score, direction, confidence, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, cohort_type, cohort_key, signal, value, peer_mean, z, "above" if z > 0 else "below", confidence, timestamp),
            ); anomalies += 1

    histories: dict[tuple[int, str], list[float]] = defaultdict(list)
    for user_id, signal, value in connection.execute(
        "SELECT user_id, signal_key, value_real FROM derived_signal_history WHERE window_name='24h' ORDER BY calculated_at"
    ):
        histories[(int(user_id), str(signal))].append(float(value))
    for (user_id, signal), numbers in histories.items():
        if len(numbers) < COHORT_MIN_SAMPLE_SIZE or (user_id, signal) not in current_values:
            continue
        mean = statistics.fmean(numbers); stddev = statistics.pstdev(numbers)
        ordered = sorted(numbers); p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        connection.execute(
            """INSERT INTO cohort_baselines(cohort_type, cohort_key, signal_key, sample_size, mean_value,
               stddev_value, median_value, p90_value, calculated_at) VALUES ('self', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id), signal, len(numbers), mean, stddev, statistics.median(numbers), p90, timestamp),
        ); baselines += 1
        value, confidence = current_values[(user_id, signal)]
        if stddev and confidence >= COHORT_MIN_CONFIDENCE:
            z = (value - mean) / stddev
            if abs(z) >= 1.25:
                connection.execute(
                    """INSERT INTO cohort_anomalies(user_id, cohort_type, cohort_key, signal_key, observed_value,
                       baseline_mean, z_score, direction, confidence, calculated_at) VALUES (?, 'self', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, str(user_id), signal, value, mean, z, "above" if z > 0 else "below", confidence, timestamp),
                ); anomalies += 1
    return baselines, anomalies


def refresh_community_cohort_baselines(
    connection: sqlite3.Connection, *, community_id: int,
    calculated_at: str | None = None,
) -> tuple[int, int]:
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat()
    signals = connection.execute(
        """SELECT user_id,signal_key,value_real,confidence
           FROM community_derived_signal_windows
           WHERE community_id=? AND window_name='24h'""",
        (community_id,),
    ).fetchall()
    current_values = {
        (int(row[0]), str(row[1])): (float(row[2]), float(row[3])) for row in signals
    }
    memberships: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for user_id, platform in connection.execute(
        """SELECT DISTINCT COALESCE(messages.user_id,platform_accounts.user_id),
                          platform_accounts.platform
           FROM messages JOIN platform_accounts ON platform_accounts.id=messages.platform_account_id
           WHERE messages.community_id=?
             AND COALESCE(messages.user_id,platform_accounts.user_id) IS NOT NULL""",
        (community_id,),
    ):
        memberships[int(user_id)].add(("platform", str(platform)))
    for user_id, context in connection.execute(
        """SELECT DISTINCT account.user_id,COALESCE(o.context_id,o.container_id)
           FROM observations o JOIN platform_accounts account ON account.id=o.actor_platform_account_id
           WHERE o.community_id=? AND account.user_id IS NOT NULL
             AND COALESCE(o.context_id,o.container_id) IS NOT NULL""",
        (community_id,),
    ):
        memberships[int(user_id)].add(("community", str(context)))
    cohorts: dict[tuple[str, str, str], list[tuple[int, float, float]]] = defaultdict(list)
    for (user_id, signal), (value, confidence) in current_values.items():
        for cohort_type, cohort_key in memberships[user_id]:
            cohorts[(cohort_type, cohort_key, signal)].append((user_id, value, confidence))

    connection.execute(
        "DELETE FROM community_cohort_baselines WHERE community_id=?", (community_id,)
    )
    connection.execute(
        "DELETE FROM community_cohort_anomalies WHERE community_id=?", (community_id,)
    )
    baselines = anomalies = 0
    for (cohort_type, cohort_key, signal), values in cohorts.items():
        numbers = [value for _, value, _ in values]
        mean = statistics.fmean(numbers)
        stddev = statistics.pstdev(numbers) if len(numbers) > 1 else 0.0
        ordered = sorted(numbers)
        p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        connection.execute(
            """INSERT INTO community_cohort_baselines(
                   community_id,cohort_type,cohort_key,signal_key,sample_size,
                   mean_value,stddev_value,median_value,p90_value,calculated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (community_id, cohort_type, cohort_key, signal, len(numbers), mean,
             stddev, statistics.median(numbers), p90, timestamp),
        )
        baselines += 1
        if len(numbers) < COHORT_MIN_SAMPLE_SIZE or stddev == 0:
            continue
        for user_id, value, confidence in values:
            if confidence < COHORT_MIN_CONFIDENCE:
                continue
            peer_numbers = [peer for peer_id, peer, _ in values if peer_id != user_id]
            if len(peer_numbers) < COHORT_MIN_SAMPLE_SIZE - 1:
                continue
            peer_mean = statistics.fmean(peer_numbers)
            peer_stddev = statistics.pstdev(peer_numbers)
            if peer_stddev == 0:
                continue
            z_score = (value - peer_mean) / peer_stddev
            if abs(z_score) < 1.25:
                continue
            connection.execute(
                """INSERT INTO community_cohort_anomalies(
                       community_id,user_id,cohort_type,cohort_key,signal_key,
                       observed_value,baseline_mean,z_score,direction,confidence,calculated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (community_id, user_id, cohort_type, cohort_key, signal, value,
                 peer_mean, z_score, "above" if z_score > 0 else "below", confidence, timestamp),
            )
            anomalies += 1

    histories: dict[tuple[int, str], list[float]] = defaultdict(list)
    for user_id, signal, value in connection.execute(
        """SELECT user_id,signal_key,value_real FROM community_derived_signal_history
           WHERE community_id=? AND window_name='24h' ORDER BY calculated_at""",
        (community_id,),
    ):
        histories[(int(user_id), str(signal))].append(float(value))
    for (user_id, signal), numbers in histories.items():
        if len(numbers) < COHORT_MIN_SAMPLE_SIZE or (user_id, signal) not in current_values:
            continue
        mean = statistics.fmean(numbers)
        stddev = statistics.pstdev(numbers)
        ordered = sorted(numbers)
        p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        connection.execute(
            """INSERT INTO community_cohort_baselines(
                   community_id,cohort_type,cohort_key,signal_key,sample_size,
                   mean_value,stddev_value,median_value,p90_value,calculated_at
               ) VALUES (?,'self',?,?,?,?,?,?,?,?)""",
            (community_id, str(user_id), signal, len(numbers), mean, stddev,
             statistics.median(numbers), p90, timestamp),
        )
        baselines += 1
        value, confidence = current_values[(user_id, signal)]
        if stddev and confidence >= COHORT_MIN_CONFIDENCE:
            z_score = (value - mean) / stddev
            if abs(z_score) >= 1.25:
                connection.execute(
                    """INSERT INTO community_cohort_anomalies(
                           community_id,user_id,cohort_type,cohort_key,signal_key,
                           observed_value,baseline_mean,z_score,direction,confidence,calculated_at
                       ) VALUES (?,?,'self',?,?,?,?,?,?,?,?)""",
                    (community_id, user_id, str(user_id), signal, value, mean, z_score,
                     "above" if z_score > 0 else "below", confidence, timestamp),
                )
                anomalies += 1
    return baselines, anomalies


def record_evaluation_label(connection: sqlite3.Connection, *, label_key: str, label_value: str,
                            observation_id: int | None = None, alert_id: int | None = None,
                            user_id: int | None = None, operator_id: int | None = None,
                            source: str = "operator") -> int:
    score_key = "risk.composite"
    score_row = None
    if alert_id is not None:
        score_row = connection.execute(
            """SELECT h.signal_key, h.value_real, h.analyzer_version
               FROM intelligence_alerts a
               JOIN derived_signal_history h ON h.id=a.signal_history_id
               WHERE a.id=?""",
            (alert_id,),
        ).fetchone()
    if score_row is None and user_id is not None:
        score_row = connection.execute(
            """SELECT signal_key, value_real, analyzer_version
               FROM derived_signal_windows
               WHERE user_id=? AND signal_key=?
               ORDER BY calculated_at DESC LIMIT 1""",
            (user_id, score_key),
        ).fetchone()
    score_value = float(score_row[1]) if score_row is not None else None
    captured_score_key = str(score_row[0]) if score_row is not None else None
    captured_model_version = int(score_row[2]) if score_row is not None else None
    if alert_id is not None:
        previous = connection.execute(
            "SELECT id FROM evaluation_labels WHERE alert_id=? AND label_key=? AND source=? ORDER BY id DESC LIMIT 1",
            (alert_id, label_key.strip(), source),
        ).fetchone()
        if previous is not None:
            connection.execute(
                """UPDATE evaluation_labels SET observation_id=?, user_id=?, label_value=?,
                   score_key=?, score_value=?, model_version=?, operator_id=?, created_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (observation_id, user_id, label_value.strip().casefold(), captured_score_key,
                 score_value, captured_model_version, operator_id, int(previous[0])),
            )
            return int(previous[0])
    cursor = connection.execute(
        """INSERT INTO evaluation_labels(observation_id, alert_id, user_id, label_key, label_value,
           score_key, score_value, model_version, operator_id, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (observation_id, alert_id, user_id, label_key.strip(), label_value.strip().casefold(),
         captured_score_key, score_value, captured_model_version, operator_id, source),
    )
    return int(cursor.lastrowid)


def run_model_evaluation(connection: sqlite3.Connection, *, model_key: str = "risk.composite", model_version: int = 2) -> int:
    rows = connection.execute(
        """SELECT l.label_value, COALESCE(l.score_value, h.value_real, d.value_real) score,
                  a.alert_type FROM evaluation_labels l
           LEFT JOIN intelligence_alerts a ON a.id=l.alert_id
           LEFT JOIN derived_signal_history h ON h.id=a.signal_history_id AND h.signal_key=?
           LEFT JOIN derived_signals d ON d.user_id=COALESCE(l.user_id,a.user_id) AND d.signal_key=?
           WHERE l.label_value IN ('positive','negative')
             AND COALESCE(l.score_key, h.signal_key, d.signal_key)=?
             AND COALESCE(l.model_version, h.analyzer_version, d.analyzer_version, ?)=?""",
        (model_key, model_key, model_key, model_version, model_version),
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
    score_values = [max(0.0, min(100.0, float(row[1]))) for row in rows]
    distribution = {
        "0-24": sum(0 <= value < 25 for value in score_values),
        "25-49": sum(25 <= value < 50 for value in score_values),
        "50-74": sum(50 <= value < 75 for value in score_values),
        "75-100": sum(75 <= value <= 100 for value in score_values),
    }
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
    community_id: int | None = None,
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
        "user_id": "a.user_id", "cohort_key": "a.cohort_key COLLATE NOCASE",
        "signal_key": "a.signal_key COLLATE NOCASE", "z_score": "a.z_score",
        "direction": "a.direction COLLATE NOCASE", "confidence": "a.confidence",
    }, "a.user_id ASC")
    evaluation_order = order_clause("evaluation", ("calculated_at", "desc"), {
        "model_key": "model_key COLLATE NOCASE", "model_version": "model_version",
        "sample_size": "sample_size", "calculated_at": "calculated_at",
    }, "id DESC")

    def community_rows(sql: str) -> list[dict[str, object]]:
        if community_id is None:
            return []
        parameters = tuple(int(community_id) for _ in range(sql.count("?")))
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    return {
        "growth": community_rows(
            """WITH dates AS (
                   SELECT date(joined_at) AS metric_date,1 AS joins,0 AS leaves
                   FROM community_memberships WHERE community_id=? AND joined_at IS NOT NULL
                   UNION ALL
                   SELECT date(left_at),0,1 FROM community_memberships
                   WHERE community_id=? AND left_at IS NOT NULL
               )
               SELECT metric_date,SUM(joins) AS joins,SUM(leaves) AS leaves,
                      SUM(joins)-SUM(leaves) AS net_growth
               FROM dates GROUP BY metric_date ORDER BY metric_date DESC LIMIT 30"""
        ),
        "repeat_offenses": community_rows(
            """SELECT ma.target_platform_account_id,pa.username,COUNT(*) AS action_count,
                      COUNT(DISTINCT ma.action_type) AS sanction_types,
                      MIN(ma.created_at) AS first_action_at,MAX(ma.created_at) AS latest_action_at
               FROM moderation_actions ma
               JOIN platform_accounts pa ON pa.id=ma.target_platform_account_id
               WHERE ma.community_id=? AND ma.status IN ('pending','completed','confirmed')
               GROUP BY ma.target_platform_account_id,pa.username HAVING COUNT(*)>1
               ORDER BY action_count DESC,latest_action_at DESC LIMIT 25"""
        ),
        "report_outcomes": community_rows(
            """SELECT COALESCE(resolution,'open') AS outcome,COUNT(*) AS report_count,
                      ROUND(AVG(CASE WHEN resolved_at IS NOT NULL THEN
                         (julianday(resolved_at)-julianday(created_at))*86400 END)) AS avg_resolution_seconds
               FROM member_reports WHERE community_id=?
               GROUP BY COALESCE(resolution,'open') ORDER BY report_count DESC,outcome"""
        ),
        "appeal_outcomes": community_rows(
            """SELECT COALESCE(disposition,'open') AS outcome,COUNT(*) AS appeal_count,
                      ROUND(AVG(CASE WHEN resolved_at IS NOT NULL THEN
                         (julianday(resolved_at)-julianday(created_at))*86400 END)) AS avg_resolution_seconds
               FROM member_appeals WHERE community_id=?
               GROUP BY COALESCE(disposition,'open') ORDER BY appeal_count DESC,outcome"""
        ),
        "rule_precision": community_rows(
            """SELECT mr.id AS rule_id,mr.name,COUNT(DISTINCT m.id) AS matches,
                      COUNT(DISTINCT rq.id) AS reviewed,
                      COUNT(DISTINCT CASE WHEN rq.resolution='confirmed' THEN rq.id END) AS confirmed,
                      COUNT(DISTINCT CASE WHEN rq.resolution='dismissed' THEN rq.id END) AS false_positives,
                      ROUND(CASE WHEN COUNT(DISTINCT CASE WHEN rq.resolution IN ('confirmed','dismissed') THEN rq.id END)=0
                         THEN 0.0 ELSE 1.0*COUNT(DISTINCT CASE WHEN rq.resolution='confirmed' THEN rq.id END)/
                         COUNT(DISTINCT CASE WHEN rq.resolution IN ('confirmed','dismissed') THEN rq.id END) END,3) AS precision
               FROM moderation_rules mr
               LEFT JOIN rule_matches rm ON rm.moderation_rule_id=mr.id
               LEFT JOIN messages m ON m.id=rm.message_id AND m.community_id=mr.community_id
               LEFT JOIN review_queue rq ON rq.message_id=m.id
               WHERE mr.community_id=? GROUP BY mr.id,mr.name ORDER BY precision ASC,matches DESC"""
        ),
        "topics": [dict(row) for row in connection.execute(
            "SELECT * FROM emerging_topics" + ("" if community_id is None else " WHERE community_id=?")
            + " ORDER BY " + topic_order + " LIMIT 25",
            () if community_id is None else (community_id,),
        )],
        "graph": [dict(row) for row in connection.execute(
            "SELECT * FROM " + ("graph_metrics" if community_id is None else "community_graph_metrics")
            + ("" if community_id is None else " WHERE community_id=?")
            + " ORDER BY " + graph_order + " LIMIT 25",
            () if community_id is None else (community_id,),
        )],
        "graph_temporal_changes": graph_temporal_changes(connection, community_id=community_id),
        "identity_suggestions": [dict(row) for row in connection.execute(
            "SELECT * FROM " + (
                "identity_link_suggestions" if community_id is None
                else "community_identity_link_suggestions"
            ) + ("" if community_id is None else " WHERE community_id=?")
            + " ORDER BY " + identity_order + " LIMIT 25",
            () if community_id is None else (community_id,),
        )],
        "cohort_anomalies": [dict(row) for row in connection.execute(
            (
                """SELECT a.* FROM cohort_anomalies a
                   JOIN cohort_baselines b ON b.cohort_type=a.cohort_type
                    AND b.cohort_key=a.cohort_key AND b.signal_key=a.signal_key
                   WHERE b.sample_size>=? AND a.confidence>=?"""
                if community_id is None else
                """SELECT a.* FROM community_cohort_anomalies a
                   JOIN community_cohort_baselines b ON b.community_id=a.community_id
                    AND b.cohort_type=a.cohort_type AND b.cohort_key=a.cohort_key
                    AND b.signal_key=a.signal_key
                   WHERE a.community_id=? AND b.sample_size>=? AND a.confidence>=?"""
            ) + " ORDER BY " + cohort_order + " LIMIT 25",
            (
                (COHORT_MIN_SAMPLE_SIZE, COHORT_MIN_CONFIDENCE)
                if community_id is None else
                (community_id, COHORT_MIN_SAMPLE_SIZE, COHORT_MIN_CONFIDENCE)
            ),
        )],
        "evaluation": [] if community_id is not None else [
            dict(row) for row in connection.execute(
                "SELECT * FROM model_evaluation_runs ORDER BY " + evaluation_order + " LIMIT 25"
            )
        ],
    }


def graph_temporal_changes(
    connection: sqlite3.Connection, *, limit: int = 25, community_id: int | None = None
) -> list[dict[str, object]]:
    history_table = "graph_metric_history" if community_id is None else "community_graph_metric_history"
    community_scope = "" if community_id is None else " WHERE community_id=?"
    parameters: tuple[object, ...] = () if community_id is None else (community_id,)
    rows = connection.execute(
        f"""WITH ranked AS (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY calculated_at DESC, id DESC) AS rn
             FROM {history_table}{community_scope}
           )
           SELECT current.user_id, current.influence_score,
                  current.influence_score-previous.influence_score AS influence_delta,
                  current.weighted_degree-previous.weighted_degree AS degree_delta,
                  current.cluster_id, previous.cluster_id AS previous_cluster_id,
                  current.is_bridge, previous.is_bridge AS previous_is_bridge
           FROM ranked current JOIN ranked previous ON previous.user_id=current.user_id AND previous.rn=2
           WHERE current.rn=1 ORDER BY ABS(current.influence_score-previous.influence_score) DESC LIMIT ?""",
        (*parameters, max(1, min(limit, 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def _identity_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _label_propagation(nodes: set[int], graph: dict[int, dict[int, float]]) -> dict[int, int]:
    """Deterministic weighted community detection for operationally stable cluster IDs."""
    labels = {node: node for node in nodes}
    for _ in range(25):
        changed = False
        for node in sorted(nodes):
            if not graph[node]:
                continue
            scores: dict[int, float] = defaultdict(float)
            for neighbor, weight in graph[node].items():
                scores[labels[neighbor]] += weight
            next_label = min(scores, key=lambda label: (-scores[label], label))
            if labels[node] != next_label:
                labels[node] = next_label
                changed = True
        if not changed:
            break
    canonical = {label: index + 1 for index, label in enumerate(sorted(set(labels.values())))}
    return {node: canonical[label] for node, label in labels.items()}


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
