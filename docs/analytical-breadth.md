# Analytical Breadth

This revision extends QBot4K from message-centric scoring into a broader intelligence workflow. Every capability remains evidence-linked and operator-reviewable.

## Event coverage

The generic observation pipeline now analyzes messages, edits, deletions, joins, leaves, reactions, role changes, moderation events, stream start/end/update events, account updates, and external feed items. Discord gateway events and Twitch IRC membership/moderation events are normalized automatically. Administrators can submit platform events to `POST /api/events` and feed items to `POST /api/external/observations`.

All event families use the same durable collection and analysis jobs. Event-specific relationships preserve direction, context, time, and the latest supporting observation.

## Investigation search

The authenticated `/search` surface and `GET /api/search` provide SQLite FTS5 search with:

- quoted phrases and full-text terms;
- start and end timestamps;
- platform, event type, user, container, and context filters;
- extracted-entity type and value filters;
- bounded pagination;
- observation pivots at `GET /api/observations/{id}/pivots`;
- saved queries through `POST /api/search/saved`.

Existing observations are backfilled into the FTS index during database initialization. Triggers keep inserts, edits, and deletions synchronized.

## Content understanding

Each observation records analyzer version, language and confidence, contextual sentiment, intent, threat level and indicators, conversation structure, and extracted entities. Entities include URLs, domains, mentions, hashtags, email addresses, IP addresses, and named entities. The implementation is deterministic and explainable. It is a foundation for later model-backed analyzers, not a claim of human-equivalent language understanding.

## Emerging topics

Maintenance compares the latest 24 hours with a seven-day baseline. It calculates velocity and unusualness for terms, adjacent phrases, and domains, retains evidence, records timelines, groups related phrases, and reports cross-community diffusion. Domains seen across multiple contexts surface unusual-link propagation.

## Graph analytics

Directed relationship edges produce in-degree, out-degree, weighted degree, PageRank, betweenness, connected clusters, articulation-point bridge flags, and a combined influence score. Metric history supports temporal deltas. `propagation_path` provides directed shortest-path pivots between known users.

## Identity inference

Cross-platform account suggestions use normalized username and identifier similarity plus context overlap. Every suggestion stores confidence and evidence with `manual_approval_required`. No inferred identity is linked automatically. Administrators must approve or reject a pending suggestion through the review API.

## Cohort baselines

The baseline job compares current 24-hour signals against platform peers, communities inferred from observation context, and each user's own historical signal distribution. Baselines retain sample size, mean, standard deviation, median, and 90th percentile. Deviations store direction, z-score, and source confidence.

## Model evaluation

Alert dispositions become positive, negative, or uncertain evaluation labels. Evaluation runs report precision, recall, false-positive rate, false-positive alert types, current score distribution, and threshold backtests at 25, 50, and 75. Runs and distributions are retained for monitoring rather than replacing earlier results.

## Analyst surface

`/analytics` and `GET /api/analytics` expose emerging topics, graph leaders and changes, identity suggestions, cohort anomalies, and recent evaluation runs. Every visible table supports independent ascending and descending sorting while preserving the sort state of the other tables. Identity decisions and event submission remain admin-only.

The `/intelligence` workspace provides the same independent sorting behavior for Alerts, Cases, and Relationships. Alerts sort by severity, subject, finding, confidence, or status. Cases sort by case, priority, status, entity count, evidence count, or update time. Relationships sort by source, relationship type, target, strength, evidence count, or last-observed time. The `/api/intelligence` response applies the same allowlisted sort parameters and reports the effective sort state.

## Verification

The complete suite contains 135 tests, including focused coverage for non-message events, external feeds, content analysis, FTS and pivots, topic diffusion, graph paths and bridges, review-gated identity inference, cohort anomalies, threshold evaluation, and all dashboard sorting paths.

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
```
