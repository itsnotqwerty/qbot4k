# QBot4K Intelligence Platform

QBot4K is a Discord, Twitch, and external-feed intelligence service. It collects an immutable observation stream, projects messages and lifecycle state, analyzes content and temporal behavior, builds entity relationships, generates explainable alerts, and gives operators searchable investigations and case workflows.

## Current scope

- Live Discord messages, edits, deletes, membership, role, reaction, and moderation events
- Twitch EventSub webhook ingestion (chat, moderation, stream, follow/sub/raid/reward and safety events), with IRC and polling fallback
- Organization, workspace, community, installation, membership, and community-scoped operator roles
- Authenticated external observations and feeds
- Content entities, language, intent, sentiment, threats, temporal signals, coordination campaigns, and community-scoped multi-axis intelligence profiles
- Moderation rules with `enforce`, `review`, `shadow`, and `disabled` modes
- Emerging topics, time-decayed graph metrics, chronological propagation, identity suggestions, cohort anomalies, and model evaluation
- Alert triage, assignments, suppression, dispositions, editable cases, evidence, notes, and reports
- Moderation-review adjudication, admin-managed policy rules, case/search exports, and an admin audit viewer
- Authenticated operational readiness counters, queue state, worker metrics, and database integrity status
- Append-only raw-event archive, dead-letter queue and replay; SQLite online backups with integrity checks and bounded retention
- A real-time live operations command center at `/live-ops`, Server-Sent Events at `/api/live-ops/stream`, and a JSON surface at `/api/live-ops`
- Stream-session timelines, current chat velocity, full finding context, grouped campaign incidents, keyboard moderation, and Twitch-confirmed action state
- Incident assignment/escalation, shift handoff, raid playbooks, Shield Mode/chat controls, mobile emergency controls, and webhook destinations
- Post-stream briefings, viewer cohorts, raid/shared-audience graphs, and moderator workload/enforcement-consistency reporting
- Legal holds, sharing agreements, model registry/approval state, data-subject requests, API-client quotas, and platform-readiness audit

## Install and run

Python 3.11 or newer is recommended.

To run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m src check-config
python -m src init-db
python -m src platform-audit
python -m src run
```

To install:

```bash
sudo ./install.sh
```

To run tests:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -v
python -m pytest -q
```

The process applies additive, idempotent SQLite migrations at startup. Back up the database before deploying a new build. See `docs/live-operations.md` for the command-center workflow and Twitch permissions, `docs/professional-platform.md` for the platform architecture, `docs/analyst-mvp-readiness.md` for the bounded release gate, and `docs/operations.md` for deployment and recovery guidance. Templates for systemd, a narrowly scoped restart authorization, and a TLS reverse proxy are in `deploy/`.

## API authentication

Dashboard APIs accept the signed operator session. Machine ingestion at `POST /api/events` and `POST /api/external/observations` can instead use `Authorization: Bearer <QBOT_INGEST_API_TOKEN>`.

Liveness is public at `/health/live`; readiness is public at `/health/ready`. Detailed queues, counters, metrics, and database state are available to signed-in operators at `/api/health`.

## Important boundary

Heuristic findings are decision support, not proof. Identity links require explicit analyst approval. Multi-axis profiles, campaigns, and alerts retain evidence and model-version metadata; models ship in shadow mode and require measured approval before automated enforcement.
