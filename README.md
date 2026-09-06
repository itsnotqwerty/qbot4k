# QBot4K Intelligence Platform

QBot4K is a Discord, Twitch, and external-feed intelligence service. It collects
an immutable observation stream, projects messages and lifecycle state, analyzes
content and temporal behavior, builds entity relationships, generates
explainable alerts, and gives operators searchable investigations and case
workflows.

## Open Beta

There is a free and open beta of QBot4K hosted at [qbot4k.dev](https://qbot4k.dev).
The service will only be free as long as the beta is ongoing. As the service becomes
more mature, a price will be set on monthly usage.

## Current scope

- Live Discord messages, edits, deletes, membership, role, reaction, and
  moderation events
- Twitch EventSub webhook ingestion (chat, moderation, stream,
  follow/sub/raid/reward and safety events), with IRC and polling fallback
- Organization, workspace, community, installation, membership, and
  community-scoped operator roles
- Authenticated external observations and feeds
- Content entities, language, intent, sentiment, threats, temporal signals,
  coordination campaigns, and community-scoped multi-axis intelligence profiles
- Moderation rules with `enforce`, `review`, `shadow`, and `disabled` modes
- Emerging topics, time-decayed graph metrics, chronological propagation,
  identity suggestions, cohort anomalies, and model evaluation
- Alert triage, assignments, suppression, dispositions, editable cases,
  evidence, notes, and reports
- Moderation-review adjudication, admin-managed policy rules, case/search
  exports, and an admin audit viewer
- Authenticated operational readiness counters, queue state, worker metrics, and
  database integrity status
- Append-only raw-event archive, dead-letter queue and replay; PostgreSQL-native
  backups with integrity checks and bounded retention
- A real-time live operations command center at `/live-ops`, Server-Sent Events
  at `/api/live-ops/stream`, and a JSON surface at `/api/live-ops`
- Stream-session timelines, current chat velocity, full finding context, grouped
  campaign incidents, keyboard moderation, and Twitch-confirmed action state
- Incident assignment/escalation, shift handoff, raid playbooks, Shield
  Mode/chat controls, mobile emergency controls, and webhook destinations
- Post-stream briefings, viewer cohorts, raid/shared-audience graphs, and
  moderator workload/enforcement-consistency reporting
- Legal holds, sharing agreements, model registry/approval state, data-subject
  requests, API-client quotas, and platform-readiness audit

## Install and run

Deno 2.9.4 and PostgreSQL are required.

To run:

```bash
cp .env.example .env
deno install --frozen
deno task check-config --env-file=.env
deno task migrate --env-file=.env
deno task platform-audit --env-file=.env
deno task role:web --env-file=.env
```

To install:

```bash
sudo ./install.sh
```

See `deploy/README.md` for the systemd, nginx, cutover, and rollback artifacts
installed by `install.sh`. All application and installation paths use Deno and
POSIX shell only.

To run tests:

```bash
deno task check
deno task browser
```

Run additive, idempotent PostgreSQL migrations before starting a new release and
back up the database before deployment. See `docs/live-operations.md` for the
command-center workflow, `docs/design.md` for architecture, and
`docs/operations.md` for deployment, support, and recovery procedures. Deno
systemd templates, a narrowly scoped restart authorization, and reverse-proxy
templates are in `deploy/`.

## API authentication

Dashboard APIs accept the signed operator session. Machine ingestion at
`POST /api/events` and `POST /api/external/observations` can instead use
`Authorization: Bearer <QBOT_INGEST_API_TOKEN>`.

Liveness is public at `/health/live`; readiness is public at `/health/ready`.
Detailed queues, counters, metrics, and database state are available to
signed-in operators at `/api/health`.

## Important boundary

Heuristic findings are decision support, not proof. Identity links require
explicit analyst approval. Multi-axis profiles, campaigns, and alerts retain
evidence and model-version metadata; models ship in shadow mode and require
measured approval before automated enforcement.
