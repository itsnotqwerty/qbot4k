# P0–P3 delivery record

## P0 — release safety and correctness

- Expanded Discord gateway event coverage, role/permission resolution, heartbeat acknowledgement, resume state, and fatal close handling.
- Added message edit/delete and stream lifecycle projections.
- Linked high-severity moderation/content alerts to source observations.
- Added moderation shadow/review/disabled modes, bounded timeout duration, action error/completion state, and global shadow mode.
- Corrected evaluation score semantics and captured scores at adjudication time.
- Replaced file-copy backups with SQLite online backups, integrity checks, hashes, and retention.

## P1 — analyst workflows

- Added editable case ownership, priority, status/closure, entity roles, evidence, notes, and immutable case activity.
- Added alert acknowledgement, assignment, suppression, and audited lifecycle updates.
- Preserved complete saved-query filters per operator and added structured observation pivots.
- Added observation-level relationship evidence.
- Added review adjudication, bounded moderation action queueing, policy-rule management, alert filters, case/search exports, and an admin audit viewer.

## P2 — analytical validity

- Added evidence-backed analytical alerts for threats, emerging topics, cohort anomalies, and network bridges.
- Added 30-day half-life edge decay, weighted label-propagation communities, and time-respecting propagation paths.
- Added identity candidate blocking and explicit manual approval.
- Changed peer anomaly scoring to leave-one-out baselines.
- Versioned content understanding and model evaluation inputs.

## P3 — operational hardening

- Added Discord rate-limit/transient retry behavior and server-requested heartbeats.
- Added bearer authentication for machine ingestion, origin checks, and browser security headers.
- Added configurable moderation, maintenance, analytics, backup, and retention settings.
- Added install manifest, environment template, deployment gate, migration guidance, restore procedure, and incident runbook.
- Added worker/job/backup metrics, authenticated readiness counters, database `quick_check`, bounded request bodies, and systemd/TLS proxy templates.

## Remaining product-scale work

The codebase is a credible single-node intelligence platform, not yet a horizontally scalable multi-tenant product. The next architectural step is a durable broker/worker topology plus Postgres/object storage, formal RBAC and tenancy, schema migration tooling, calibrated ML/NLP services, and production telemetry with SLO alerting.
