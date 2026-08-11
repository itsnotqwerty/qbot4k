# Analyst MVP readiness review

Review date: 2026-08-11

## Release decision

The codebase meets the defined analyst MVP gate for a single-node deployment. This means one organization can collect supported events, search and pivot evidence, triage findings, adjudicate moderation reviews, manage cases, export work product, and operate the service with authenticated health data, backups, migrations, audit history, and recovery guidance.

It does not claim enterprise-scale completion. Multi-tenant isolation, horizontally distributed workers, external identity providers beyond Discord OAuth, a warehouse/lakehouse, and formal compliance certification remain post-MVP platform work.

## Gate score

| Vertical | Gate | Result | Evidence |
|---|---|---:|---|
| P0 — trustworthy collection | Immutable, deduplicated Discord/Twitch/external observations; lifecycle fidelity; evidence links | 100% | Collector, event-fidelity, idempotency, content-analysis, and evidence tests |
| P1 — analyst operations | Search, pivots, saved queries, alert triage, review adjudication, editable cases, notes, exports, audit viewer | 100% | Dashboard/API workflow integration tests and service-level lifecycle tests |
| P2 — intelligence depth | Topics, graph decay, propagation, identity suggestions with human approval, cohort anomalies, evaluation/backtests | 100% | Analytical-breadth and P0–P3 tests |
| P3 — safe operation | Additive migrations, retries, bounded input, role revalidation, origin checks, metrics, integrity-aware health, online backups, recovery docs | 100% | Migration, retry, auth/security, operational snapshot, backup/restore, and full-suite tests |

“100%” is the completion of these bounded gates, not a claim that no future feature, model, connector, or scale improvement exists.

## Blockers found and resolved

| Blocker | Resolution |
|---|---|
| Moderation review queue was read-only | Added dismiss/confirm/escalate decisions, optional bounded enforcement actions, idempotent action jobs, resolution metadata, and audit events |
| Moderation policy could not be managed by an operator | Added admin-only rule create/update surfaces and validation for rule types, severity, mode, action, and duration |
| Cases were editable only through low-level APIs | Added case controls for metadata, ownership, status, notes, entities, and evidence plus an activity timeline |
| Alerts lacked practical triage controls | Added severity/status/assignee/text filtering, acknowledgement/assignment, case creation, and disposition controls |
| Analysts could not export search or case work product | Added bounded CSV search export and complete JSON case export; both are audited |
| Audit data existed without a review surface | Added an admin-only filtered audit page and API |
| Operational metrics table was unused | Added worker outcome/latency and job/backup metrics plus queue/counter snapshots in authenticated health output |
| Database readiness did not verify integrity | Added SQLite `quick_check` and degraded health behavior on database errors or failed integrity |
| Request size parsing could terminate a handler | Added bounded bodies and invalid `Content-Length` handling |
| Deployment guidance was not executable | Added systemd and Caddy templates plus explicit release, TLS, backup, and restore gates |
| Analytical findings overwhelmed the triage queue | Added baseline warm-up, per-kind evidence gates, top-10 topic limiting, stable alert keys, cohort minimums, automatic expiry, and open-only queue counts |

## Release acceptance criteria

Before traffic is enabled:

1. Install production and development dependencies in a clean virtual environment.
2. Run `python -m src --env-file /etc/qbot4k/qbot4k.env check-config`,
   `python -m src --env-file /etc/qbot4k/qbot4k.env init-db`,
   `python -m unittest discover -v`, and `python -m pytest -q`.
3. Start with global moderation shadow mode enabled; promote individual rules only after analyst sampling.
4. Put the dashboard behind TLS and configure the exact HTTPS Discord OAuth redirect URI.
5. Verify `/health/live`, `/health/ready`, and authenticated `/api/health`.
6. Create a backup, restore it to a new path, and confirm `PRAGMA integrity_check` returns `ok`.
7. Confirm the Discord privileged intents and Twitch credentials in the target environment.

## Post-MVP roadmap

The next platform phase should prioritize multi-tenant authorization and data partitioning, PostgreSQL/object-storage separation, independently scalable workers, OpenTelemetry/SIEM export, enterprise SSO, connector replay controls, and formal model-governance sign-off. Those items improve scale and governance but do not block this bounded analyst MVP.
