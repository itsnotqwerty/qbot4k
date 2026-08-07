# QBot4K Project Review

This review is based on the current implementation, tests, and project docs. The goal is to separate likely improvements into four quadrants by impact and effort, while putting the highest-impact, lowest-effort work first.

## Priority Order

These are the best first moves because they reduce production risk quickly without requiring broad rewrites.

1. Fail closed when operator allowlists are missing instead of granting admin access by default.
2. Add graceful shutdown handling for `SIGTERM` and coordinated service teardown.
3. Tighten cross-field config validation so incomplete runtime setups fail at startup.
4. Stop returning raw Discord OAuth and API error details to the browser.

## Quadrant 1: High Impact, Low Effort

### 1. Fail closed for dashboard authorization
- Impact: High
- Effort: Low
- Why: The dashboard currently grants `admin` when `QBOT_OPERATOR_GUILD_IDS` is empty, which makes a misconfiguration a security boundary failure instead of a safe startup failure.
- Evidence: `determine_operator_role()` in `src/dashboard/auth.py` returns `admin` when no allowlist is configured. `README.md` also documents this as a development fallback.
- Improvement: Return no role when the allowlist is empty, and require an explicit operator allowlist whenever the web service is enabled.

### 2. Add graceful shutdown for long-running services
- Impact: High
- Effort: Low
- Why: The main runtime joins worker threads and only handles `KeyboardInterrupt`, which is fine for local runs but weak for process managers that send `SIGTERM`.
- Evidence: `run_application()` in `src/__main__.py` handles `KeyboardInterrupt` and shuts down the web server in `finally`, but there is no signal-driven coordinated shutdown path for connectors or jobs.
- Improvement: Register `SIGTERM` and `SIGINT` handlers, introduce a shared shutdown flag, and make connectors stop cleanly.

### 3. Strengthen cross-field configuration validation
- Impact: High
- Effort: Low
- Why: Several runtime behaviors degrade silently instead of failing fast when a service is enabled without the supporting configuration.
- Evidence: `src/config.py` validates individual fields, but jobs such as `run_twitch_live_announcement_job()` in `src/jobs.py` still need to discover or infer missing Discord state at runtime.
- Improvement: Add service-specific dependency checks in `AppSettings.validate()`, especially around Discord guild targeting, operator allowlists, and feature combinations that require tokens.

### 4. Hide raw upstream error details from end users
- Impact: Medium
- Effort: Low
- Why: OAuth and Discord API failures currently bubble detailed error text into browser responses, which is not necessary for operators and increases information exposure.
- Evidence: `src/dashboard/auth.py` builds exception messages with upstream bodies, and `src/dashboard/server.py` returns `Discord OAuth failed: {exc}` directly to the client.
- Improvement: Log detailed failures server-side and return a generic operator-facing error message.

## Quadrant 2: High Impact, High Effort

### 1. Remove hardcoded single-channel Twitch assumptions
- Impact: High
- Effort: High
- Why: Live announcement behavior is still tied to one specific Twitch channel, which blocks clean multi-channel operation and leaks environment-specific assumptions into the product.
- Evidence: `run_twitch_live_announcement_job()` and `send_manual_twitch_live_announcements()` in `src/jobs.py` call `_fetch_twitch_live_stream("its_not_qwerty", ...)` and store announcements under the same hardcoded channel name.
- Improvement: Make live-announcement sources configuration-driven and support one or more managed channels explicitly.

### 2. Introduce schema versioning and real migrations
- Impact: High
- Effort: High
- Why: The database schema is created from one large embedded SQL script, which works for bootstrap but becomes risky as soon as table shapes need to change across deployments.
- Evidence: `SCHEMA_SQL` in `src/db.py` defines the full schema, and `initialize_database()` applies it directly with no schema version table or migration history.
- Improvement: Add a migration table, discrete migration steps, and migration tests against real persisted databases.

### 3. Add retry and backoff for external APIs and connectors
- Impact: High
- Effort: High
- Why: Discord OAuth, guild discovery, channel fetches, and Twitch API calls rely on one-shot requests with fixed timeouts. That makes transient failures look like feature failures.
- Evidence: `src/dashboard/auth.py` and `src/jobs.py` use `urlopen(..., timeout=15)` without retry loops or backoff.
- Improvement: Centralize HTTP request helpers with retry, jitter, and rate-limit-aware behavior.

### 4. Implement Discord heartbeat and reconnect logic
- Impact: High
- Effort: High
- Why: Long-lived gateway sessions need lifecycle management beyond a simple socket loop. Without it, the Discord connector can silently stop ingesting events.
- Evidence: The current architecture expects robust connector workers, but the codebase has no dedicated reconnect and heartbeat hardening called out in tests or docs, and there are no operational tests covering this failure mode.
- Improvement: Add heartbeat tracking, resume or reconnect behavior, and connector health transitions.

## Quadrant 3: Low Impact, Low Effort

### 1. Reduce environment-specific wording in docs and defaults
- Impact: Low
- Effort: Low
- Why: The repository still exposes channel-specific defaults and development fallbacks in user-facing docs, which makes the project feel less reusable than it is.
- Evidence: `README.md` and `src/config.py` use `its_not_qwerty` as the default Twitch channel and join-command channel.
- Improvement: Replace environment-specific defaults with neutral examples and clearer configuration notes.

### 2. Consolidate operator-facing status messages
- Impact: Low
- Effort: Low
- Why: Redirect-based status strings are assembled ad hoc across dashboard actions, which makes UX and future localization or templating harder.
- Evidence: `src/dashboard/server.py` builds status query strings inline for go-live, linking, moderation, and other actions.
- Improvement: Add a small helper for normalized flash messages and consistent success or failure phrasing.

### 3. Add explicit notes about current operational test gaps
- Impact: Low
- Effort: Low
- Why: The README states the current suite passes, but it does not say what failure modes are not yet covered.
- Evidence: The test suite is focused on foundation, ingestion, identity, jobs, dashboard auth, commands, and dashboard behavior, with no dedicated ops-failure test module under `tests/`.
- Improvement: Document current test boundaries so deployment expectations stay realistic.

## Quadrant 4: Low Impact, High Effort

### 1. Break up the monolithic database module
- Impact: Low
- Effort: High
- Why: `src/db.py` owns schema, ingestion, moderation persistence, commands, and several product behaviors. That is a maintainability issue, but it is not the most urgent product or operational risk right now.
- Evidence: `src/db.py` is both the schema bootstrap location and a broad grab bag of domain behaviors.
- Improvement: Split it into schema, repositories, and focused domain data-access modules once migration support exists.

### 2. Build a deeper operational test matrix
- Impact: Low
- Effort: High
- Why: More failure-mode coverage would help reliability, but the codebase will benefit more if shutdown, retries, migrations, and auth defaults are fixed before investing in a larger scenario suite.
- Evidence: The current tests directory covers core functional slices, but not signal handling, retry exhaustion, or lock-contention scenarios.
- Improvement: After the runtime hardening work lands, add integration tests for shutdown, transient API failures, and SQLite contention.

## Recommended Sequence

1. Lock down dashboard authorization defaults.
2. Add graceful shutdown and fail-fast config validation.
3. Sanitize upstream error handling.
4. Remove hardcoded Twitch channel assumptions.
5. Add retry and reconnect hardening for Discord and Twitch integrations.
6. Introduce schema migrations before the next substantial data-model change.

## Notes

- This review intentionally prioritizes shipped risks over feature expansion.
- The codebase already has a good baseline of tests and documentation for a working integration slice; the main opportunity is hardening runtime behavior and removing environment-specific assumptions.