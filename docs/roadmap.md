# QBot4K Roadmap

## 1. Purpose
This roadmap converts the specification and architecture documents into a phased delivery plan. It is structured to reduce implementation risk by building the shared core before the dashboard and production hardening.

Principles:

- Deliver working slices, not isolated scaffolding.
- Validate shared domain behavior before broad UI work.
- Keep SQLite viable by addressing rollups, backup, and transaction shape early.
- Defer policy-heavy automation until auditability and operator workflows exist.

## 2. Delivery Strategy
The first release should be delivered in six phases:

1. Foundation
2. Ingestion
3. Moderation Core
4. Shared Identity and Reputation
5. Dashboard
6. Production Readiness

Each phase should end with a demonstrable outcome and explicit exit criteria.

## 3. Phase Plan
### Phase 1: Foundation
Goal: Establish the runtime skeleton, persistent storage, and operational baseline.

Deliverables:

- Typed configuration loader.
- Application bootstrap in src/__main__.py.
- SQLite connection management and WAL configuration.
- Initial schema and migration mechanism.
- Structured logging.
- Basic health endpoint and startup diagnostics.

Suggested work items:

- Create config module and environment contract.
- Create db module and connection lifecycle.
- Define schema for users, platform_accounts, messages, moderation_rules, rule_matches, moderation_actions, reputation_events, user_notes, review_queue, audit_log, and metrics_rollups.
- Add application entrypoint that can start web, connectors, and jobs.
- Add smoke tests for startup success and failure.

Exit criteria:

- The app starts with valid config and fails fast with invalid config.
- SQLite schema can be created in a clean environment.
- Health endpoint reports process readiness.

### Phase 2: Ingestion
Goal: Receive platform messages and persist them in a common format.

Deliverables:

- Twitch connector with channel join and message ingestion.
- Discord connector with guild and channel message ingestion.
- Shared normalized event schema.
- Durable message persistence.
- Basic connector health reporting.

Suggested work items:

- Implement connector adapters in src/twitch.py and src/discord.py.
- Define normalized message object and persistence mapping.
- Persist platform account identities on first sight.
- Add duplicate protection using platform message identifiers where available.
- Add integration tests for message ingestion on both connectors.

Exit criteria:

- Twitch messages are stored in SQLite with normalized fields.
- Discord messages are stored in SQLite with normalized fields.
- Connector failures are logged and surfaced in health status.

### Phase 3: Moderation Core
Goal: Turn ingested messages into rule evaluations, auto-actions, and review items.

Deliverables:

- Deterministic rules engine.
- Initial rule types from the specification.
- Moderation service for automatic and manual action recording.
- Review queue creation for non-auto-enforceable cases.
- Audit log coverage for system-issued actions.

Suggested work items:

- Implement exact term, phrase pattern, rate limit, duplicate message, and link restriction rules.
- Define reason codes and severity model.
- Implement outbound moderation adapter methods per platform.
- Persist rule_matches, moderation_actions, and review_queue rows.
- Apply idempotency checks for retryable moderation execution.
- Add unit tests for rules and integration tests for queue creation.

Exit criteria:

- Clear low-risk violations can trigger automatic action.
- Higher-risk or ambiguous cases are queued for operator review.
- All moderation outcomes are auditable.

### Phase 4: Shared Identity and Reputation
Goal: Build the shared user layer that makes cross-platform moderation useful.

Deliverables:

- Canonical user records.
- Manual Twitch and Discord account linking.
- Unlinking with admin-only authorization.
- Reputation score event model and score calculation.
- Candidate flagging for high-reputation users.

Suggested work items:

- Implement profile service in src/intelligence/userprofiles.py.
- Implement reputation service in src/intelligence/powerusers.py.
- Define score delta reasons and thresholds.
- Add operator notes support.
- Add integration tests for link conflict detection, linking, unlinking, and reputation updates.

Exit criteria:

- Operators can associate platform accounts to one canonical user.
- Score history is explainable through reputation events.
- Candidate flags update when thresholds are crossed.

### Phase 5: Dashboard
Goal: Expose system state and actions through an authenticated operator interface.

Deliverables:

- Server-rendered dashboard shell.
- Discord OAuth login flow.
- Session management and role-based authorization.
- Overview page and API.
- Users page and API.
- Moderation page and API.

Suggested work items:

- Implement auth middleware and session handling.
- Build overview metrics queries using rollups where available.
- Build user search, detail, notes, and account linking views.
- Build moderation queue and recent action views.
- Wire dashboard forms to shared domain services.
- Add API and integration tests for auth boundaries.

Exit criteria:

- An authorized operator can log in through Discord OAuth.
- Overview, users, and moderation pages render from live data.
- Moderators and admins see correct permission boundaries.

### Phase 6: Production Readiness
Goal: Harden the system for sustained operation.

Deliverables:

- Metrics rollup jobs.
- Retention and cleanup jobs.
- Backup automation and freshness reporting.
- Operational documentation.
- Improved error handling and observability.
- Release checklist and deployment procedure.

Suggested work items:

- Implement background job scheduling.
- Add retention enforcement for raw messages and audit logs.
- Add backup job with verification metadata.
- Add monitoring counters for ingestion, moderation, queue depth, and errors.
- Document deployment, configuration, and restore process.
- Run end-to-end manual verification in a staging-like environment.

Exit criteria:

- Backup freshness and health are visible.
- Retention jobs operate without manual intervention.
- The release can be deployed repeatably from documented steps.

## 4. Cross-Phase Dependencies
- Phase 2 depends on Phase 1 schema, config, and bootstrap.
- Phase 3 depends on Phase 2 normalized message persistence.
- Phase 4 depends on Phase 2 platform account persistence and Phase 3 moderation history.
- Phase 5 depends on Phase 4 profile data and Phase 3 moderation services.
- Phase 6 depends on all prior phases.

## 5. Critical Path
The delivery critical path is:

1. Config and database foundation.
2. Message ingestion.
3. Rules and moderation orchestration.
4. Canonical users and reputation.
5. Authenticated dashboard.
6. Jobs, backups, and hardening.

This path should not be delayed by optional UI polish or advanced analytics.

## 6. Risk Register
### Risk: SQLite write contention
Impact:
Connector ingestion and dashboard mutations may compete for locks.

Mitigation:

- Enable WAL mode.
- Keep transactions short.
- Precompute overview metrics.
- Avoid synchronous long-running jobs during message spikes.

### Risk: Platform API rate limits
Impact:
Moderation actions or reconnect behavior may fail under load.

Mitigation:

- Centralize rate limit handling inside connectors.
- Use backoff and retry.
- Surface failures in health and audit logs.

### Risk: Policy ambiguity for automatic enforcement
Impact:
Unsafe automation or inconsistent moderation decisions.

Mitigation:

- Start with a small allowlist of auto-enforceable rules.
- Require explicit reason codes and audit logs.
- Route uncertain cases to review queue.

### Risk: OAuth and local role mapping complexity
Impact:
Login works but operator authorization is unreliable.

Mitigation:

- Separate authentication from authorization clearly.
- Store local operator roles in the application database.
- Add tests for unauthorized-but-authenticated cases.

## 7. Suggested Release Gates
Before calling the first release complete, verify:

- Ingestion from both platforms works in a realistic environment.
- Automatic moderation is limited to approved low-risk rules.
- Operators can review users, link accounts, and resolve queue items.
- Dashboard auth and admin boundaries work correctly.
- Backup, retention, and health checks are active.
- Audit trails exist for system and operator actions.

## 8. Open Planning Questions
- Which Python web framework will the team adopt?
- Which migration tool will manage SQLite schema changes?
- What are the approved positive contribution signals for reputation gains?
- What scale target should Phase 6 validate against?
- Is there a preferred hosting environment for deployment documentation?
