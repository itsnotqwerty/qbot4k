# Professional Twitch Community Intelligence Release

This release implements the seven delivery tracks that move QBot4K beyond its single-community MVP. It remains deployable as one process and SQLite database for a pilot, while defining clean seams for PostgreSQL, an event broker, and horizontally scaled workers.

## 1. Stabilized single-community deployment

- Refreshed Twitch credentials are atomically persisted to the selected `--env-file`, not an implicit repository file.
- Invalid grants stop the connector with `auth_failed`; temporary identity-provider failures remain retryable.
- Schema initialization is serialized and cached per database rather than replayed in every hot path.
- Twitch moderation uses Helix ban/timeout/warning endpoints and only completes an action after a successful provider response.
- Announcement jobs iterate configured Twitch channels instead of embedding a channel name.
- The installer deploys an exact-unit PolicyKit rule for the dashboard restart action.

Release verification: `python -m src platform-audit`, then the unit/integration suite. Run provider smoke tests with a non-production Twitch channel before enabling enforcement.

## 2. Organization and community model

The hierarchy is `organization → workspace → community → installation`. Messages, observations, policies, alerts, cases, campaigns, profiles, legal holds, and recovery records carry community scope. `operator_community_roles` provides viewer, analyst, moderator, admin, and owner roles. Installations map Twitch broadcaster IDs and Discord guild IDs to a community.

The default tenant at ID 1 preserves backward compatibility during an additive migration. New integrations should register an installation and place its community ID on every normalized observation.

## 3. Twitch EventSub control plane

`POST /webhooks/twitch/eventsub` validates Twitch's HMAC signature and a ten-minute replay window before parsing a body. It handles webhook verification, notification, and revocation messages, records subscription state, resolves the broadcaster installation, and submits normalized observations to the same idempotent pipeline used by connectors.

Configure:

```text
QBOT_TWITCH_EVENTSUB_SECRET=<at least 16 random characters>
QBOT_TWITCH_EVENTSUB_CALLBACK_URL=https://example.com/webhooks/twitch/eventsub
```

Create or reconcile subscriptions from deployment automation using Twitch's EventSub API and record returned subscriptions in `twitch_eventsub_subscriptions`. The stored state is the control-plane inventory; Twitch remains the authority.

## 4. Durable event and worker runtime

Each accepted observation creates an append-only `raw_event_archive` record. Maintenance atomically writes pending records to `QBOT_RAW_ARCHIVE_DIR/community-<id>/YYYY/MM/DD/`. Exhausted jobs are copied into `dead_letter_events`, and replay creates a new job with a fresh idempotency key without modifying the original record.

For larger deployments, move query tables to PostgreSQL, payload files to object storage, and processing jobs to a broker. Preserve the current observation ID, event type, schema version, community ID, idempotency key, dead-letter, and replay contracts.

## 5. Validated intelligence

The global reputation score is retained only as a compatibility projection. The professional profile stores separate 0–100 trust, risk, engagement, identity-confidence, and maturity axes with confidence, evidence count, explanation JSON, community scope, and version.

Near-duplicate messages and shared domains are clustered into coordination campaigns when at least three messages and two canonical actors match within the window. `model_registry` keeps content, profile, and coordination models in shadow mode by default. Promotion requires labelled evaluation data, an approved precision threshold, a named approver, and a rollback version.

## 6. Live operations console

`/live-ops` is now an authenticated incident-command surface. `/api/live-ops/stream` delivers snapshots over Server-Sent Events so findings, incident state, velocity, provider confirmations, cohorts, and controls update without a page reload. Operators can inspect full message context, navigate findings and moderate from the keyboard, assign/escalate incidents, hand off a shift, activate attack playbooks, and operate Shield Mode or chat settings from desktop or mobile.

Twitch remains authoritative: UI actions first enter a pending state and are shown as confirmed only after a successful Helix response or matching EventSub moderation event. Provision the exact scopes in `docs/live-operations.md` and complete a non-production control smoke test before relying on the emergency surface.

## 7. Commercial governance

- Community policy defaults to shadow moderation and isolated sharing.
- Cross-community findings require an explicit, time-bounded sharing agreement.
- Legal holds protect case evidence from retention work; data-subject requests track access/deletion workflows.
- API clients store only a key hash, scopes, status, and per-minute quota/usage.
- Model versions and approval status are queryable; operator changes remain in the audit log.
- Raw payload retention, backup retention, and application retention are separate controls.

Before a paid launch, add an external secrets manager, managed PostgreSQL/object storage, restore drills, on-call alerts, a DPA/privacy notice, subprocessor inventory, customer export/deletion runbooks, and measured SLOs. Recommended initial targets are 99.9% webhook acceptance, 99.5% dashboard availability, p95 event-to-alert under 10 seconds, and zero unreviewed automatic enforcement models.

## Pilot release gate

1. `platform-audit` has no failures; warnings have named owners.
2. EventSub signature, challenge, duplicate, and revocation fixtures pass.
3. A backup restore and raw-event replay succeed in a clean environment.
4. Tenant-isolation tests prove community A cannot query or mutate community B through every exposed route.
5. Helix moderation smoke tests confirm provider response storage and retry behavior.
6. Model evaluation meets the community-approved precision floor in shadow mode.
7. Incident, privacy, and operator-access runbooks have completed tabletop tests.
