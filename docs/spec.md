# QBot4K Specification

## 1. Purpose
QBot4K is a chat management and moderation system for Twitch and Discord. It combines platform-specific bot integrations with a shared intelligence layer and an operator dashboard.

This document defines the functional requirements, implementation boundaries, technical architecture, data model, operational requirements, and open decisions required to build a production-capable first release.

## 2. Product Goals
The system must:

- Provide consistent moderation tooling across Twitch and Discord.
- Maintain a shared user profile that operators can use across both platforms.
- Support semi-automated moderation, where clear violations can be actioned automatically and ambiguous cases are escalated to operators.
- Expose operational visibility through a web dashboard.
- Run on SQLite for the initial implementation while keeping the data layer portable enough for a future database migration.

The initial release does not need:

- Heuristic or automatic account linking.
- Fully autonomous moderation policy decisions for all content.
- A distributed, multi-tenant architecture.

## 3. Scope
### In Scope
- Twitch bot integration.
- Discord bot integration.
- Shared moderation rules and event ingestion.
- Shared user profiles and manually managed cross-platform account links.
- Reputation scoring based on moderation history and positive contribution signals.
- Dashboard pages for overview, users, and moderation.
- Dashboard APIs, authentication, and deployment requirements.
- Audit logging, observability, and backup procedures appropriate for a standard production deployment.

### Out of Scope
- Automatic account inference between Twitch and Discord.
- Machine learning model training or hosted LLM integration in the first release.
- Native mobile applications.
- Multi-workspace support.

## 4. Definitions
- Operator: An authenticated admin or moderator using the dashboard.
- Platform account: A user identity on Twitch or Discord.
- Linked user: A canonical user profile connected to one or more platform accounts.
- Reputation score: A bounded numeric score used for prioritization and moderation review.
- Moderation event: An observed message, rule violation, or operator action stored by the system.
- Enforcement action: A bot-issued or operator-issued delete, warning, timeout, mute, kick, or ban.

## 5. System Overview
QBot4K consists of five logical subsystems:

1. Platform connectors
	 Twitch and Discord adapters receive messages, convert platform events into a common format, and execute moderation actions.
2. Intelligence services
	 Shared services classify content, maintain reputation scores, and build user summaries.
3. Persistence layer
	 SQLite stores users, links, messages, moderation actions, rule definitions, scores, and audit records.
4. Dashboard API
	 A backend service exposes authenticated endpoints for metrics, user search, moderation review, and admin actions.
5. Dashboard UI
	 Pages for overview, users, and moderation consume the API.

The codebase layout should map to these responsibilities:

- src/twitch.py: Twitch bot connector.
- src/discord.py: Discord bot connector.
- src/intelligence/userprofiles.py: Canonical user profile services.
- src/intelligence/powerusers.py: Reputation and moderator-candidate logic.
- src/dashboard/overview.py: Overview page handlers and metrics queries.
- src/dashboard/users.py: User search, profile, and linking workflows.
- src/dashboard/moderation.py: Moderation queue and action workflows.
- src/__main__.py: Process bootstrap, config loading, and service startup.

## 6. Functional Requirements
### 6.1 Platform Connectors
Both Twitch and Discord integrations must:

- Connect using bot credentials loaded from configuration.
- Receive chat messages and normalize them into a shared event schema.
- Emit join, leave, and moderation-related events when the platform API provides them.
- Support outbound moderation commands issued by the dashboard or rules engine.
- Apply per-platform rate limiting and retry logic for outbound API calls.
- Persist inbound events before attempting asynchronous enrichment or scoring.

### 6.2 Twitch Requirements
The Twitch connector must:

- Join configured channels at startup.
- Ingest message text, username, user identifier, badges or roles if available, timestamp, and channel identifier.
- Support deletion, timeout, and ban actions where permitted by the Twitch API or chat command surface.
- Detect moderator or VIP status when available and include it in the normalized event.

### 6.3 Discord Requirements
The Discord connector must:

- Connect to configured guilds and channels.
- Ingest message text, author identifier, guild, channel, role information, and timestamp.
- Support delete, warn, mute or timeout, kick, and ban actions depending on configured permissions.
- Ignore bot-authored messages unless explicitly configured for loopback testing.

### 6.4 Rule Evaluation and Moderation
The system must support a shared moderation pipeline:

1. Receive normalized message event.
2. Run static rule checks such as blacklist matching, rate limits, spam detection, and repeated-message detection.
3. Produce a rule evaluation result containing severity, matched rules, and a recommended action.
4. For clear violations, perform the configured automatic action.
5. For non-clear or high-impact cases, create a moderation review item for an operator.
6. Record all decisions and actions in the audit log.

The first release must include these rule types:

- Exact-match blacklist terms.
- Pattern-based banned phrases.
- Message frequency threshold per user.
- Duplicate or near-duplicate message detection within a rolling window.
- Link posting restrictions for untrusted users.

Moderation policy behavior:

- Automatic actions are allowed only for rule matches marked as auto-enforceable.
- Ban-level actions require either a high-confidence rule or operator confirmation.
- Every automatic action must be reviewable and reversible by an operator.
- The rules engine must expose reason codes, not only freeform text.

### 6.5 Shared User Profiles
The system must maintain a canonical user record independent of platform.

Each user profile must include:

- Internal user ID.
- Display names and aliases.
- Linked platform accounts.
- Reputation score and score history.
- Moderation history summary.
- Contribution summary metrics.
- Notes visible to operators.

Manual linking requirements:

- Operators can create a canonical user from either platform account.
- Operators can link an existing Twitch account and Discord account through the dashboard.
- Linking must require explicit operator confirmation and produce an audit record.
- Unlinking must be allowed only to authorized admins and must preserve historical events.

### 6.6 Reputation and Candidate Promotion
The reputation system replaces the loose concept of a social credit score with an implementation-defined reputation score.

Requirements:

- Score range must be bounded, for example 0 to 100.
- Score changes must be event-driven and explainable.
- Negative adjustments apply for rule violations, repeated moderation actions, or ban history.
- Positive adjustments apply for sustained participation without violations and approved positive contribution signals.
- The dashboard must display current score, trend, and recent score-change reasons.
- Users above a configurable threshold may be flagged as moderator candidates.

Candidate promotion behavior:

- The system may surface candidate lists to operators.
- The system may notify operators about candidates.
- The system must not auto-promote users to moderator.

### 6.7 Dashboard
The dashboard must require authenticated access and provide three primary views.

#### Overview
The overview page must show:

- Messages processed over time.
- Rule violations over time.
- Automatic actions versus manual actions.
- Top channels or guilds by activity.
- Recent system errors and connector health.
- Count of open moderation review items.

#### Users
The users page must show:

- Search and filter by username, platform, reputation band, moderation status, and link status.
- Sort by activity, score, recent violations, or creation date.
- A user detail view with linked accounts, score history, moderation history, and notes.
- Manual account linking and unlinking actions.

#### Moderation
The moderation page must show:

- Recent moderation actions.
- Open review items requiring operator input.
- Current bans, mutes, or limitations known to the system.
- Action controls for warning, deleting, timing out, muting, or banning where supported.
- Filters by platform, severity, rule type, operator, and time range.

## 7. Nonfunctional Requirements
### 7.1 Reliability
- Connector failures must not crash the dashboard API process.
- Message ingestion should be at-least-once within process limits.
- Temporary platform API failures must be retried with backoff.
- Background scoring jobs must be idempotent.

### 7.2 Performance
- Dashboard list pages should respond within 500 ms for typical queries on a modest dataset.
- Message ingestion should persist events fast enough to avoid connector backpressure during normal channel traffic.
- Expensive aggregations for overview metrics should be precomputed or cached when necessary.

### 7.3 Security
- The dashboard must require authenticated sessions.
- Operator roles must distinguish moderators from admins.
- All operator actions must be audit logged.
- Secrets must be loaded from environment variables or a secure configuration source and must never be committed.

### 7.4 Privacy and Retention
- Message retention periods must be configurable.
- Deleted or moderated content should remain visible only to authorized operators.
- Manual notes about users must be access-controlled.
- Account linking and moderation decisions must retain attribution to the acting operator.

## 8. Technical Architecture
### 8.1 Runtime Model
The first release should run as a single deployable application with separate internal services:

- Connector worker for Twitch.
- Connector worker for Discord.
- API server for dashboard requests.
- Background worker for scoring, cleanup, and rollups.

These may run in a single process for local development and as separate processes in production if needed.

### 8.2 Internal Modules
- Config module: Loads environment, validates required settings, and exposes typed settings.
- Connector modules: Translate platform-specific events to shared domain objects.
- Rules engine: Evaluates messages against configured rules.
- Profile service: Resolves canonical users and linked accounts.
- Reputation service: Applies score deltas and computes candidate flags.
- Moderation service: Issues actions and records audit entries.
- API service: Serves dashboard data and action endpoints.
- Scheduler or job runner: Executes periodic cleanup, rollups, and score recalculation tasks.

### 8.3 Data Flow
1. Connector receives platform event.
2. Event is normalized and written to storage.
3. Rules engine evaluates the event.
4. Moderation service performs any automatic action.
5. Reputation service records score changes.
6. Dashboard queries stored and aggregated data.

## 9. Data Model
SQLite is the system of record for the first release.

Minimum tables:

- users
	Canonical user records.
- platform_accounts
	Twitch and Discord identities mapped to a canonical user when linked.
- messages
	Normalized inbound chat events.
- moderation_rules
	Configured moderation rules and enforcement behavior.
- rule_matches
	Rule evaluation results for a message.
- moderation_actions
	Automatic and manual enforcement records.
- reputation_events
	Score deltas with reason codes.
- user_notes
	Operator-authored notes.
- review_queue
	Cases awaiting operator action.
- audit_log
	Auth, linking, configuration, and moderation audit records.
- metrics_rollups
	Precomputed aggregates for dashboard views.

Key relationships:

- One user may have many platform accounts.
- One platform account belongs to zero or one canonical user.
- One message may produce many rule matches.
- One rule match may trigger zero or one automatic moderation actions.
- One moderation action may be associated with an operator or system actor.

Implementation notes:

- Use integer primary keys unless a strong reason exists to use UUIDs.
- Add created_at and updated_at timestamps to mutable tables.
- Index platform-specific account identifiers, message timestamps, moderation action timestamps, and reputation score lookup fields.

## 10. API Requirements
The dashboard backend must expose authenticated endpoints. Example route groups:

- GET /api/overview
	Returns KPIs, timeseries data, open review counts, and health summaries.
- GET /api/users
	Returns paginated user search results with filters.
- GET /api/users/{user_id}
	Returns full user profile details.
- POST /api/users/link
	Manually links two platform accounts.
- POST /api/users/{user_id}/notes
	Adds an operator note.
- GET /api/moderation/actions
	Returns recent moderation actions.
- GET /api/moderation/reviews
	Returns open review queue items.
- POST /api/moderation/actions
	Issues an operator moderation action.
- POST /api/moderation/reviews/{review_id}/resolve
	Resolves a queued moderation case.
- GET /api/health
	Returns API and connector health state.

API requirements:

- JSON request and response bodies.
- Pagination for list endpoints.
- Structured error responses with machine-readable codes.
- Authorization checks for every mutating endpoint.

## 11. Authentication and Authorization
The dashboard must implement:

- Login for operators.
- Session-based authentication backed by Discord OAuth for the first release.
- Role-based access control with at least moderator and admin roles.
- Admin-only permissions for account unlinking, rule changes, and other high-impact actions.

Authentication requirements:

- Only members of an approved Discord server or allowlist may authenticate.
- The application must map authenticated Discord identities to operator roles stored locally.
- Sessions must expire and be revocable by admins.
- External identity providers other than Discord are out of scope for the first release.

## 12. Background Jobs
The system must support scheduled or queued jobs for:

- Reputation rollup recalculation.
- Metrics aggregation for dashboard charts.
- Cleanup of expired review items or cache entries.
- Backup execution or backup verification hooks.
- Connector health checks.

## 13. Configuration
The application must load configuration for:

- Twitch credentials and channel list.
- Discord credentials and guild or channel scope.
- Discord OAuth client credentials and callback settings for dashboard login.
- SQLite database path.
- Dashboard bind address and port.
- Authentication secrets.
- Rule thresholds and default enforcement behavior.
- Retention periods.
- Logging level.

Startup must fail fast if required configuration is missing.

## 14. Observability and Operations
Standard production operations for the first release must include:

- Structured application logs.
- Error logging for failed connector actions.
- Health endpoints for API and connectors.
- Periodic database backups.
- Startup and runtime configuration validation.
- Metrics for ingestion rate, moderation actions, queue depth, and error counts.

Recommended deployment shape:

- One application service.
- One persistent volume for SQLite and backups.
- A reverse proxy for dashboard access.
- TLS termination at the proxy or hosting layer.

Dashboard implementation choice:

- The first release dashboard should be rendered by the Python backend using server-side templates.
- Client-side JavaScript may be used for small interactive enhancements, but a separate frontend application is out of scope.

## 15. Testing Requirements
The implementation must include:

- Unit tests for rules engine logic.
- Unit tests for reputation score calculations.
- Integration tests for manual account linking.
- Integration tests for moderation action recording.
- API tests for auth and authorization boundaries.
- Smoke tests for connector startup and configuration validation.

## 16. Delivery Milestones
### Milestone 1: Foundation
- Config loading.
- SQLite schema creation.
- Process bootstrap.
- Basic logging and health checks.

### Milestone 2: Connectors
- Twitch message ingestion.
- Discord message ingestion.
- Normalized message persistence.

### Milestone 3: Moderation Core
- Rules engine.
- Automatic action execution for low-risk cases.
- Moderation audit trail.

### Milestone 4: Shared Intelligence
- Canonical users.
- Manual account linking.
- Reputation scoring.

### Milestone 5: Dashboard
- Auth.
- Overview page and API.
- Users page and API.
- Moderation page and API.

### Milestone 6: Production Readiness
- Backups.
- Rollups and background jobs.
- Test coverage and deployment documentation.

## 17. Open Questions
These items still need explicit decisions before implementation should be considered locked:

- Which Python web framework will serve the dashboard API and server-rendered UI?
- Which reputation signals count as positive contribution, and who is allowed to configure them?
- Which moderation rules are approved for automatic enforcement on Twitch versus Discord?
- What are the expected scale targets in terms of channels, guilds, messages per minute, and operators?

Retention defaults for the first release:

- Raw message records: 90 days.
- Audit records: 1 year.
- Aggregated metrics rollups may be retained longer because they contain summary data rather than raw chat content.

## 18. Acceptance Criteria
The first release is complete when:

- Both Twitch and Discord connectors can ingest messages into SQLite.
- Operators can review users, link accounts manually, and view moderation history.
- Clear rule violations can trigger configured automatic actions.
- Ambiguous cases appear in a moderation review queue.
- The dashboard is authenticated and role-aware.
- Backups, health checks, logging, and audit trails are operational.
