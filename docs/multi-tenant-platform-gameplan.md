# Multi-Tenant Community Platform Checklist

## Goal

Turn QBot4K from a capable single-community deployment into an invite-only,
multi-tenant community operations platform. Discord and Twitch remain
first-class integrations, while a capability-based adapter contract leaves a
clear path for future platforms. SQLite remains available for development and
constrained pilots; PostgreSQL is required before broad production launch.

## Checklist Conventions

- `[x]` means implemented and validated.
- `[ ]` means incomplete or not yet validated.
- Stable ID patterns such as `P<phase>-<item>`, `P<phase>-G<gate>`, and
   `V-<check>` are used for search, issue titles, commits, and status updates.
- A checked sub-item does not imply that its parent phase or exit gate is
   complete.

## Phase Index

| ID | Phase | Focus |
|---|---|---|
| `P0` | [Architecture Contracts](#p0-architecture-contracts) | Tenant and platform boundaries |
| `P1` | [Tenant-Safe Persistence](#p1-tenant-safe-persistence) | Migrations, ownership, PostgreSQL, credentials |
| `P2` | [Tenant Sessions and Permissions](#p2-tenant-sessions-and-permissions) | Sessions, capabilities, operators |
| `P3` | [Homepage and Installation Onboarding](#p3-homepage-and-installation-onboarding) | Public entry, Discord/Twitch installation |
| `P4` | [Moderation Dashboard Rework](#p4-moderation-dashboard-rework) | Queues, reviews, sanctions, policy safety |
| `P5` | [Community Management](#p5-community-management) | Lifecycle, onboarding, announcements, operations |
| `P6` | [Operations and Rollout](#p6-operations-and-rollout) | Quotas, SLOs, staged launch |
| `V` | [Verification](#verification-checklist) | Isolation, security, provider and UI checks |

## Product Decisions

- [x] `D-01` Initial onboarding is invite-only with an admin-assisted fallback.
- [x] `D-02` The tenant hierarchy is organization, workspace, community, and
   installation.
- [x] `D-03` Default roles are viewer, analyst, moderator, admin, and owner,
   with community-scoped grants and denials. Arbitrary custom roles are deferred.
- [x] `D-04` Intelligence-assisted enforcement starts in shadow or review mode
   and remains explainable, reviewable, and reversible.
- [x] `D-05` Cross-community data is isolated by default. Sharing requires a
   separate, explicit, time-bounded agreement.
- [x] `D-06` Billing, enterprise SSO/SCIM, mobile applications, and a third
   platform are outside the initial release.

## P0: Architecture Contracts

- [x] `P0-01` Make community context mandatory for every tenant-owned record,
   external event, operator request, query, mutation, job, and provider action.
- [x] `P0-02` Reject unresolved or unauthorized tenant context instead of
   falling back to community 1 on implemented tenant-sensitive paths.
- [x] `P0-03` Inventory every table and application surface as global,
   organization, community, or installation scoped. Extend `platform-audit` to
   enforce the inventory.
- [x] `P0-04` Introduce tenant-context and actor-attribution contracts at
   repository and service boundaries.
- [x] `P0-05` Define platform capabilities for events, moderation actions,
   member lifecycle, announcements, and live controls. Discord and Twitch
   adapters must advertise the capabilities they implement.

### P0 Exit Gate

- [x] `P0-G1` Unknown and inactive installations fail closed.
- [x] `P0-G2` An automated inventory identifies every remaining unscoped surface.
- [x] `P0-G3` Two-community tests prove basic ingestion and query isolation.

## P1: Tenant-Safe Persistence

- [x] `P1-01` Replace additive startup column checks with ordered, idempotent
   migrations.
- [x] `P1-02` Backfill legacy rows into the default community, identify orphans,
   add missing installation references, and then make tenant ownership non-null.
- [x] `P1-03` Remove `DEFAULT 1` and fallback-to-1 behavior from messages,
   observations, moderation rules, intelligence data, and installation resolution.
- [x] `P1-04` Require compound tenant lookups such as
   `(community_id, entity_id)` for implemented sensitive records to prevent
   cross-tenant object enumeration.
- [x] `P1-05` Move community configuration out of process-wide environment
   variables. Keep only deployment bootstrap and secrets settings in application
   config.
- [ ] `P1-06` Add PostgreSQL behind the database boundary and provide a
   rehearsable SQLite export/import path with row-count and checksum verification.
- [x] `P1-07` Encrypt installation credentials at rest and track credential
   rotation.

### P1 Exit Gate

- [x] `P1-G1` Legacy migration is repeatable and rejects unresolved ownership.
- [ ] `P1-G2` Domain and HTTP suites run against SQLite and PostgreSQL.
- [x] `P1-G3` Backup restore and raw-event replay succeed in a clean environment.

## P2: Tenant Sessions and Permissions

- [x] `P2-01` Extend dashboard identity with available communities and one
   validated active community. Never trust a raw `community_id` query parameter.
- [x] `P2-02` Keep the five role bundles and add per-operator grants and denials.
   A denial wins, and the last owner cannot be removed.
- [x] `P2-03` Define capabilities for dashboard access, members, moderation
   queues, moderation actions, bulk actions, rules, appeals, sensitive evidence,
   cases, analytics, exports, announcements, integrations, settings, operators,
   and audit history.
- [x] `P2-04` Map every route, API action, bot command, job, and live control to
   a capability enforced by a shared authorization guard.
- [x] `P2-05` Add operator invitations, expiry and revocation, ownership
   transfer, emergency access removal, and session invalidation after permission
   changes.
- [x] `P2-06` Require confirmation for destructive bulk actions, ownership
   transfer, integration removal, and permanent bans.

### P2 Exit Gate

- [x] `P2-G1` A table-driven authorization suite covers every role and override.
- [x] `P2-G2` Tenant switching cannot disclose or mutate another community on
   implemented surfaces.
- [x] `P2-G3` Every access mutation has immutable actor attribution and audit
   history.

## P3: Homepage and Installation Onboarding

- [x] `P3-01` Split the public `/` homepage from authenticated `/dashboard` and
   present the Discord/Twitch product, privacy and security posture, integration
   support, and clear login and invite actions.
- [x] `P3-02` Add a prominent **Link Discord** button that starts gated pilot
   onboarding instead of opening an unbound bot installation URL.
- [x] `P3-03` Authenticate the operator, validate the pilot invite, create or
   select the community, and verify authority to manage the selected Discord guild.
- [x] `P3-04` Generate Discord authorization with the configured client ID,
   minimal `bot` and `applications.commands` scopes, reviewed permission bits, a
   fixed callback URI, and a signed, expiring, single-use state nonce bound to the
   operator and community.
- [x] `P3-05` Reject tampered, expired, or replayed callback state, validate the
   selected guild, and create a `pending` installation. Mark it `active` only
   after bot or gateway confirmation.
- [x] `P3-06` Continue onboarding through Twitch broadcaster OAuth/EventSub,
   channel selection, scope review, shadow-mode defaults, and connection health
   checks.
- [x] `P3-07` Persist installation states (`pending`, `active`, `degraded`,
   `revoked`), scopes, capability flags, external metadata, health, and reconnect
   history.
- [x] `P3-08` Discover active connector installations from storage instead of
   static guild and channel lists on implemented connector paths.
- [x] `P3-09` Add a community switcher and settings for profile, locale,
   timezone, integrations, notifications, retention, guidelines, policy, and
   operators.

### P3 Exit Gate

- [x] `P3-G1` Discord install callbacks are tenant bound, replay safe, and audited.
- [x] `P3-G2` Revoked installations stop ingestion and outbound actions on
   implemented provider paths.
- [x] `P3-G3` Onboarding can resume after interruption and reports actionable
   health.

## P4: Moderation Dashboard Rework

- [ ] `P4-01` Decompose the monolithic dashboard server into controllers, query
   services, reusable server-rendered templates, static CSS, and progressively
   enhanced JavaScript while retaining the current runtime initially.
- [x] `P4-02` Build a permission-aware application shell with community
   switching, responsive navigation, breadcrumbs, and accessible controls.
- [x] `P4-03` Replace the moderation page with queues for unassigned, mine,
   escalated, appeals, and resolved work. Add saved filters, search, severity,
   rule, platform, time, assignment, SLA age, keyboard navigation, and pagination.
- [x] `P4-04` Build a review workspace with conversation context, member
   history, linked identities, prior sanctions, matched-rule explanation,
   signals, reports, cases, evidence, notes, and provider confirmation state.
- [x] `P4-05` Add warnings, graduated sanctions, action presets, reason
   templates, duration controls, evidence requirements, previews, idempotency,
   retries, and visible pending, confirmed, and failed provider states.
- [x] `P4-06` Add bounded bulk moderation with explicit selection, dry-run
   summaries, rate-limit-aware execution, partial-failure reporting, and full
   auditing.
- [x] `P4-07` Add rule versioning, draft/shadow/enforce lifecycles, sample
   testing, impact previews, high-risk approvals, rollback, exemptions, and
   per-platform scope.
- [x] `P4-08` Make member reports and appeals first-class queues. High-severity
   appeals should use a different reviewer where staffing permits.
- [x] `P4-09` Reuse existing alerts, cases, campaigns, shift handoff, live
   operations, evidence, and legal holds instead of creating parallel workflows.

### P4 Exit Gate

- [x] `P4-G1` Moderators can complete daily queue work without tenant or
   permission leaks.
- [x] `P4-G2` Provider actions are idempotent and their authoritative status is
   visible on implemented paths.
- [x] `P4-G3` Bulk actions and policy publication have explicit safety controls.

## P5: Community Management

### Member Lifecycle

- [x] `P5-01A` Show tenant-scoped joins, leaves, role observations, linked
   identities, notes, warnings, sanctions, verification evidence, and explainable
   risk or reputation signals in the user detail UI and API.
- [x] `P5-01B` Resolve Discord role IDs to role names in lifecycle history.
- [x] `P5-01C` Add lifecycle event filtering and export.

### Onboarding and Guidelines

- [x] `P5-02A` Add tenant-scoped Discord welcomes with preview, disable,
   auditing, deduplication, rate limiting, and installation binding.
- [x] `P5-02B` Add newcomer role routing with bounded provider retries.
- [x] `P5-02C` Add operator verification with optional required evidence.
- [x] `P5-02D` Add checkpoint deadlines, overdue reminders, and departure closure.
- [x] `P5-02E` Add optional post-verification guidelines/resource delivery.
- [x] `P5-02F` Add richer resource catalogs.
- [x] `P5-02G` Add self-service verification gates.
- [x] `P5-02H` Add broader anti-raid and spam policy controls.

### Announcements

- [x] `P5-03A` Add tenant announcement drafts, approvals, explicit installation
   targets, scheduling, cancellation, bounded retries, and delivery attempts.
- [x] `P5-03B` Fold Twitch live notifications into the tenant-scoped
   announcement service.
- [x] `P5-03C` Add complete tenant timezone handling across announcement editing,
   preview, scheduling, and delivery status.

### Community Operations

- [x] `P5-04A` Add shift handoffs, escalation workflows, workload reporting,
   response-time metrics, and incident playbooks.
- [x] `P5-04B` Add shift scheduling and on-call routing.

### Community Health

- [x] `P5-05A` Add explainable health analytics for activity, moderation volume,
   response time, campaigns, and platform health.
- [x] `P5-05B` Complete growth, repeat-offense, report and appeal outcome, and
   rule-precision analytics.

### Governance and Offboarding

- [x] `P5-06A` Add legal holds and account unlinking with preserved history.
- [x] `P5-06B` Add retention administration, export and deletion request
   completion, token revocation, tenant offboarding, and isolated backup drills.

### P5 Exit Gate

- [x] `P5-G1` Implemented automations support preview or dry-run visibility,
   disable controls, auditing, deduplication, and rate limiting.
- [x] `P5-G2` Analytics apply minimum cohort sizes and permission-gated exports.
- [x] `P5-G3` Export, deletion, legal-hold, and tenant-offboarding drills pass.

## P6: Operations and Rollout

- [x] `P6-01` Add per-tenant quotas and backpressure for ingestion, APIs, jobs,
   exports, announcements, and moderation actions. Schedule work fairly so one
   noisy tenant cannot starve others.
- [x] `P6-02` Track tenant-aware SLOs for webhook acceptance, event-to-alert
   latency, moderation confirmation, queue age, connector health, dashboard
   availability, dead letters, and backup freshness.
- [ ] `P6-03` Roll out through an internal two-community environment,
   invite-only design partners in shadow mode, controlled enforcement,
   PostgreSQL migration and restore drills, and security/privacy review before
   broader onboarding.

## Verification Checklist

- [x] `V-01` Build an isolation matrix for every HTML route, API endpoint, bot
   command, job, export, SSE stream, direct lookup, and provider action.
- [x] `V-02` Test role bundles, grants and denials, stale sessions, invitation
   expiry, ownership transfer, and destructive-action confirmation.
- [x] `V-03` Test Discord and Twitch installation resolution, revocation,
   unknown events, outbound credentials, OAuth scopes and permission bits, and
   callback state on implemented flows.
- [x] `V-04` Cover the homepage, Link Discord CTA, onboarding, tenant switching,
   moderation queues, bulk actions, policies, reports, appeals, and auditing with
   end-to-end dashboard tests.
- [x] `V-05` Run `python -m unittest discover -v`, `python -m pytest -q`, and
   `python -m src platform-audit` after implementation slices.
- [ ] `V-06` Perform non-production Discord and Twitch smoke tests for
   install/revoke, ingestion, sanctions, announcements, reconnects, rate limits,
   and provider confirmations.
- [x] `V-07` Test tenant-ID tampering, IDOR, CSRF, OAuth state, session fixation
   and revocation, stored XSS, SQL injection, secret exposure, webhook replay,
   export authorization, and bulk-action abuse.
- [x] `V-08` Verify keyboard and screen-reader operation plus mobile and desktop
   layouts for the homepage, onboarding, switcher, moderation workspace, and
   settings.

## Completed Initial Slice

- [x] `S-01` Make installation resolution fail closed and add focused tests.
- [x] `S-02` Inventory fallback-to-community-1 behavior and begin removing it
   from tenant-sensitive paths.
- [x] `S-03` Introduce signed Discord installation state and callback validation.
- [x] `S-04` Add the public homepage and Link Discord entry point.
- [x] `S-05` Add pending installation persistence and gateway confirmation.
