# Deno and Fresh v1.0.0 Transition Plan

## Goal

Port QBot4K from Python to Deno 2 and Fresh while preserving HTTP and API
behavior, operator workflows, tenant isolation, provider behavior, and
operational contracts. PostgreSQL is the transition bridge and final datastore.
Version `1.0.0` ships without a Python runtime dependency.

## Plan Conventions

- `[x]` means implemented and validated.
- `[ ]` means incomplete or not yet validated.
- Work IDs use `DF<phase>-<item>` and exit gates use `DF<phase>-G<gate>`.
- A phase is complete only when every item and its exit gate are checked.
- Compatibility evidence must be automated unless a gate explicitly requires a
  provider, deployment, accessibility, security, or recovery rehearsal.

## Decisions

- [x] `DFD-01` Version `1.0.0` completely replaces Python with Deno and Fresh.
- [x] `DFD-02` PostgreSQL is implemented before Fresh and remains the production
      datastore. Deno KV is not part of the transition.
- [x] `DFD-03` Existing routes, APIs, sessions where feasible, tenant behavior,
      integrations, and operator workflows retain behavioral compatibility.
- [x] `DFD-04` TypeScript replaces Python incrementally in this repository.
      TypeScript domain modules may coexist with matching Python modules under
      `src/`; Fresh framework directories live at the repository root.
- [x] `DFD-05` Production cutover targets near-zero downtime through blue/green
      web switching and database-backed single ownership for workers and
      providers.
- [x] `DFD-06` PostgreSQL changes follow expand-and-contract rules and remain
      readable by the previous Python release throughout the rollback window.
- [x] `DFD-07` Fresh runs as a stateful systemd-hosted Deno service rather than
      an edge deployment. Connectors, workers, IRC, and recovery tasks require a
      controlled host runtime.

## Scope

Included:

- HTTP, HTML, and API routes.
- Authentication, authorization, sessions, and credential storage.
- Domain and intelligence logic.
- PostgreSQL persistence, migrations, and SQLite data import.
- Jobs, workers, Discord, Twitch, deployment, operations, and recovery.
- Compatibility tests, rollout, rollback, and eventual Python removal.

Excluded:

- Product or visual redesign.
- Breaking API changes.
- New providers or unrelated features.
- Model, scoring, or moderation threshold changes.
- Unrelated schema redesign.

## Phase Index

| ID    | Phase                 | Primary outcome                                   |
| ----- | --------------------- | ------------------------------------------------- |
| `DF0` | Contract Freeze       | Executable cross-runtime compatibility baseline   |
| `DF1` | PostgreSQL Boundary   | Python operates correctly on the shared datastore |
| `DF2` | Deno Foundation       | Secure, typed Deno runtime and repository layer   |
| `DF3` | Fresh Web Port        | Compatible Fresh HTTP, API, and dashboard surface |
| `DF4` | Domain and Workers    | Deno owns domain processing and scheduled work    |
| `DF5` | Provider Port         | Deno owns Discord and Twitch integrations         |
| `DF6` | Packaging and Cutover | Rehearsed production transition and rollback      |
| `DF7` | Stabilization         | Python removal and v1.0.0 release                 |

## DF0: Contract Freeze and Migration Harness

- [x] `DF0-01` Generate a machine-readable inventory of every HTTP route,
      method, status, content type, form field, JSON shape, redirect, and
      externally visible error from `DashboardApp.dispatch` and its handlers.
- [x] `DF0-02` Inventory session cookie fields and signatures, OAuth state,
      webhook signatures, configuration variables, CLI commands, job types,
      provider actions, schema objects, and authorization policies.
- [x] `DF0-03` Add language-neutral golden fixtures under
      `tests/fixtures/contracts/` for two-community isolation, sessions,
      observations, moderation, jobs, commands, provider normalization,
      EventSub, exports, backup metadata, API JSON, and representative HTML.
- [x] `DF0-04` Scrub credentials and personal data from every fixture and add a
      fixture provenance and redaction check.
- [x] `DF0-05` Add a parity harness that runs one fixture against Python and
      Deno and compares normalized output. Normalization may ignore declared
      IDs, timestamps, or nonces, but never tenant IDs, authorization results,
      provider payloads, status codes, or audit events.
- [x] `DF0-06` Add CI jobs for Python, Deno formatting/lint/type checking/tests,
      PostgreSQL migrations, contract parity, and browser tests. Keep the Python
      suite mandatory until Python is removed.

### DF0 Exit Gate

- [x] `DF0-G1` Route, policy, job, provider, schema, and configuration manifests
      are generated and checked for drift.
- [x] `DF0-G2` Golden fixtures cover every high-risk compatibility boundary and
      at least one authorized and denied tenant case per boundary.
- [x] `DF0-G3` CI can execute a minimal Python-versus-Deno fixture comparison.

## DF1: PostgreSQL Compatibility Boundary

- [x] `DF1-01` Introduce a Python database protocol and backend selection behind
      `connect_database`. Domain code must not branch on the backend.
- [x] `DF1-02` Translate ordered SQLite migrations into versioned PostgreSQL
      migrations with an advisory migration lock and idempotent startup
      behavior.
- [x] `DF1-03` Replace SQLite-specific behavior deliberately, including WAL and
      `PRAGMA`, `BEGIN IMMEDIATE`, conflict syntax, placeholders, `lastrowid`,
      timestamp comparisons, JSON text access, and SQLite backup APIs.
- [x] `DF1-04` Replace FTS5 and its triggers with PostgreSQL `tsvector` and GIN
      indexes while preserving search and ranking behavior through fixtures.
- [x] `DF1-05` Implement processing-job claims with PostgreSQL row locking while
      preserving leases, retries, priority, idempotency, and tenant fairness.
- [x] `DF1-06` Build a rehearsable SQLite-to-PostgreSQL exporter/importer. Its
      manifest must include schema version, deterministic per-table checksums,
      row counts, orphan checks, tenant ownership checks, and source/target
      totals.
- [x] `DF1-07` Import in foreign-key order, reset sequences, validate all
      constraints, and keep the final source SQLite database read-only.
- [x] `DF1-08` Run domain and HTTP suites against SQLite and PostgreSQL. Add
      concurrency tests for jobs, idempotency, quotas, moderation completion,
      and migration locking.
- [ ] `DF1-09` Move the Python deployment to PostgreSQL before the runtime port.
      Pause ingress, checkpoint SQLite, export/import, verify the manifest,
      switch configuration, resume Python, and monitor.

### DF1 Exit Gate

- [x] `DF1-G1` Fresh and upgrade-path PostgreSQL migrations are repeatable and
      reject incompatible ownership or schema state.
- [x] `DF1-G2` Python domain and HTTP suites pass against both backends.
- [ ] `DF1-G3` A production-size sanitized export/import drill produces matching
      row counts, checksums, search results, sequences, and zero orphans.
- [ ] `DF1-G4` The Python PostgreSQL deployment passes SLO and recovery checks.
- [x] `DF1-G5` Existing gameplan items `P1-06` and `P1-G2` are complete.

## DF2: Deno Runtime Foundation

- [x] `DF2-01` Add pinned Deno and Fresh dependencies, `deno.json`, a lockfile,
      and tasks for formatting, linting, type checking, testing, development,
      and production roles.
- [x] `DF2-02` Add Fresh framework directories at the repository root and allow
      TypeScript and Python domain modules to coexist during transition.
- [x] `DF2-03` Implement strict TypeScript equivalents of `TenantContext`,
      `ActorAttribution`, shared models, surface policies, validated
      configuration, structured logging, and typed application errors.
- [x] `DF2-04` Implement a PostgreSQL repository and transaction layer with
      request-scoped clients, typed row decoders, compound tenant lookups, and
      test transaction helpers. Routes and provider adapters may not access
      unscoped raw clients.
- [x] `DF2-05` Preserve HMAC session, OAuth state, EventSub signature,
      constant-time comparison, CSRF/origin, credential encryption, token
      storage, and rotation semantics.
- [x] `DF2-06` Prove Deno can read Python-created sessions and installation
      credentials. If encryption compatibility is impractical, implement an
      audited staged re-encryption migration before provider cutover.
- [x] `DF2-07` Add role-selectable entry points for `web`, `jobs`, `analysis`,
      `discord`, and `twitch`, preserving `QBOT_ENABLED_SERVICES` behavior.
- [x] `DF2-08` Add live and ready health checks for PostgreSQL, migration state,
      and role-specific dependencies. Run Deno with explicit least-privilege
      permissions.

### DF2 Exit Gate

- [x] `DF2-G1` Deno formatting, linting, type checking, and unit tests pass.
- [x] `DF2-G2` Tenant, actor, policy, configuration, and cryptographic fixtures
      pass in both runtimes.
- [x] `DF2-G3` Each Deno process role starts, reports readiness, and shuts down
      gracefully under systemd-compatible signals.

## DF3: Fresh HTTP and Dashboard Port

- [x] `DF3-01` Extract the application shell into Fresh layouts and components,
      CSS into `static/`, and progressive behavior into narrowly scoped islands.
      Forms and core navigation must work without JavaScript.
- [x] `DF3-02` Port public, legal, health, Discord operator authentication, and
      community-switching routes.
- [x] `DF3-03` Port overview, users, search, signals, and analytics as complete
      vertical slices with route, query service, policy guard, component, and
      tests.
- [x] `DF3-04` Port moderation queues, review workspaces, rules, bulk actions,
      sanctions, and provider confirmation states.
- [x] `DF3-05` Port commands, announcements, onboarding, integrations, settings,
      audit, and live operations.
- [x] `DF3-06` Preserve current paths, methods, redirects, statuses, content
      types, cookies, forms, JSON shapes, pagination, filters, and permission
      outcomes. Any compatibility redirect requires an explicit fixture.
- [x] `DF3-07` Port machine ingestion and Twitch EventSub separately from
      operator routes. Verify raw-body signatures before parsing and preserve
      size limits, replay protection, tenant resolution, capability checks, and
      response behavior.
- [x] `DF3-08` Run Fresh in shadow-read mode against PostgreSQL. Mirror safe GET
      traffic and recorded or synthetic mutations, but never live provider
      actions. Evidence: an isolated PostgreSQL-backed Fresh run mirrored
      `GET /privacy` to a controlled loopback upstream; synthetic `POST /logout`
      remained on the primary and produced no shadow comparison or provider
      action.

### DF3 Exit Gate

- [x] `DF3-G1` Every route in the frozen manifest has a Fresh implementation and
      a parity result.
- [x] `DF3-G2` HTTP, auth, tenant-isolation, no-JavaScript, and browser suites
      pass on desktop and mobile.
- [x] `DF3-G3` Shadow-read comparisons meet agreed correctness and latency
      thresholds with no unresolved severity-1 defects. Evidence: the live
      comparison matched status, media type, and normalized body with 3.41 ms
      primary and 6.26 ms upstream latency. The PostgreSQL integration gate
      enforces matched output and a 1,000 ms ceiling for both measured paths;
      all five focused tests pass.
- [x] `DF3-G4` Existing dashboard decomposition item `P4-01` is complete.

## DF4: Domain Services, Queues, and Scheduled Work

- [x] `DF4-01` Port pure domain logic first: normalization, moderation rules,
      commands and templates, permissions, scoring, signals, analytics,
      intelligence, quotas, and SLO calculations.
- [x] `DF4-02` Preserve model versions, thresholds, rounding, evidence,
      confidence, explanations, and deterministic outputs through golden
      fixtures.
- [x] `DF4-03` Port observation ingestion and processing repositories while
      preserving unique keys, archive hashes, tenant ownership, idempotency,
      retries, leases, dead letters, and audit attribution.
- [x] `DF4-04` Implement concurrent Deno workers with PostgreSQL transactional
      claims. Test crashes, recovery, starvation, duplicate execution, lease
      expiry, and tenant fairness before enabling live consumption.
- [x] `DF4-05` Port maintenance, analytics, announcements, onboarding,
      notifications, retention, raw archive/replay, and backup orchestration.
- [x] `DF4-06` Replace SQLite file backups with PostgreSQL-native backups or
      managed snapshots while retaining application-level verification manifests
      and documented recovery point and recovery time objectives.
- [x] `DF4-07` Shadow deterministic analysis jobs without committing results.
      Transfer one live job type at a time through a PostgreSQL single-owner
      flag.

### DF4 Exit Gate

- [x] `DF4-G1` Domain and job parity fixtures pass with no unexplained output
      differences.
- [x] `DF4-G2` Fault and concurrency tests prove fair, idempotent processing and
      recovery after process termination.
- [ ] `DF4-G3` Deno owns all production job types and scheduled work while
      Python consumers remain disabled but available for rollback. Local
      evidence: the PostgreSQL ownership gate proves Python-owned jobs are
      unclaimable, audited transfer enables Deno claims, and `platform-audit`
      fails for observed job types that are missing ownership or are not
      Deno-owned. Production ownership and disabled Python consumers remain.

## DF5: Discord and Twitch Provider Port

- [x] `DF5-01` Run compatibility spikes for maintained Deno-compatible Discord
      and Twitch libraries. Pin selected versions and retain recorded protocol
      fixtures. Use native WebSocket, `fetch`, or TCP/TLS behind provider
      interfaces where libraries cannot preserve required behavior.
- [x] `DF5-02` Port Discord OAuth and installation flows, Gateway lifecycle,
      intents, normalization, commands, channels, members, announcements,
      moderation, rate limits, reconnect/resume, and installation health.
- [x] `DF5-03` Port Twitch broadcaster OAuth and token refresh, IRC lifecycle,
      EventSub reconciliation and verification, stream polling, live controls,
      announcements, moderation, rate limits, and installation health.
- [x] `DF5-04` Preserve tenant and installation capability guards before every
      outbound action.
- [x] `DF5-05` Add recorded protocol, fake-server, disconnect/resume,
      expired-token, signature/replay, revocation, and retry tests.
- [x] `DF5-06` Transfer each live installation through a PostgreSQL ownership
      lease so only one runtime can receive events or perform actions for it.

### DF5 Exit Gate

- [x] `DF5-G1` Recorded and fake-provider suites pass for both providers.
- [ ] `DF5-G2` Real non-production Discord and Twitch smoke tests pass for
      OAuth, installation, reconnect/resume, ingestion, commands, moderation,
      announcements, refresh, revocation, and denied capabilities.
- [ ] `DF5-G3` Deno owns all provider installations without duplicate events or
      actions. Python connectors remain disabled but rollback-ready. Local
      evidence: the PostgreSQL ownership gate proves exclusive concurrent
      acquisition, wrong-holder denial, independent installation leases, release
      handoff, and expired-lease recovery. Live installation ownership and
      disabled Python connectors remain.

## DF6: Packaging, Rollout, and Cutover

- [x] `DF6-01` Update `install.sh`, `deploy/install.py`, systemd templates, and
      nginx templates for pinned Deno, explicit permission flags, separate
      process roles, Fresh static assets, graceful shutdown, migration gating,
      health checks, logs, and release-directory rollback.
- [x] `DF6-02` Add Deno operational commands equivalent to `check-config`,
      `init-db` or migrate, `platform-audit`, `issue-pilot-invite`, `run`, and
      development watch.
- [x] `DF6-03` Document PostgreSQL provisioning, migrations, import, backups,
      restore, credential rotation, provider ownership, cutover, and rollback.
- [x] `DF6-04` Deploy Deno blue/green beside Python with Deno writes and
      provider actions disabled. Run parity, browser, SLO, migration, restore,
      security, and privacy checks. Evidence: an isolated live rehearsal ran
      Python blue and Fresh green concurrently on separate loopback ports
      against the same PostgreSQL schema. It exposed and resolved migration-28
      manifest/readiness incompatibility and missing Python legal routes. Both
      roles then returned ready, Fresh `POST /logout` returned the read-only
      `503`, and live `/privacy` shadow comparison matched with 0.44 ms primary
      and 1.17 ms upstream latency. `deno task test:blue-green` now guards
      shared migration and legal-route compatibility and runs parity, SLO,
      migration, write-fence, security/privacy, and desktop/mobile browser
      gates. Restore rehearsal evidence is recorded under `DF6-06`;
      production-like runbook execution remains tracked by `DF6-G1`.
- [ ] `DF6-05` Roll out through one internal community, a second internal
      community, and design partners using database-backed ownership flags.
- [x] `DF6-06` Rehearse a successful cutover, an aborted cutover, nginx
      switchback, worker/provider lease handoff, and PostgreSQL restore. Record
      measured recovery time and data-loss bounds. Evidence: the fail-fast
      cutover runner proves the successful sequence and abort before nginx on a
      failed preflight. The nginx helper validates the inactive target,
      atomically switches generated loopback upstreams, verifies public
      readiness, restores the previous upstream on failure, and records elapsed
      milliseconds. Real-PostgreSQL ownership gates prove aborted job and
      provider handoffs return ownership to Python with Deno fenced out and
      queued work untouched. The restore gate verifies a real custom archive,
      refuses unsafe targets, restores a disposable PostgreSQL database,
      validates schema, row totals, and constraints, and records RTO/RPO.
      Production-like runbook execution remains tracked by `DF6-G1`.
- [x] `DF6-07` Execute cutover in dependency order: drain Python jobs and
      providers, acquire Deno ownership, verify queues, switch nginx, and verify
      health, auth, ingestion, moderation, jobs, and providers. Evidence:
      `deploy/execute-cutover.sh` executes drain, ownership, preflight, nginx
      switch, and post-switch verification hooks in strict order and stops at
      the first failure. Its automated gate proves the complete order and that a
      failed preflight prevents switching. `deno task cutover-preflight` fails
      closed unless every observed job type and active installation is
      Deno-owned, every active provider lease has a live holder, the green web
      role is writable, and the DF6-08 monitor passes; a disposable PostgreSQL
      transition proves the ownership sequence. Production-like execution
      remains tracked by `DF6-G1`.
- [x] `DF6-08` Monitor error rate, webhook acceptance, queue age, provider
      health, and tenant SLOs throughout the rollback window. Evidence:
      `deno task cutover-monitor` emits timestamped JSON snapshots at a bounded
      sample count and interval and exits immediately on a rollback blocker. It
      fails closed on job error rate, queue age, provider health, missing,
      stale, or breached tenant SLOs, webhook latency, and PostgreSQL connection
      saturation. Automated tests prove complete-window sampling and early
      blocker exit; a disposable PostgreSQL gate proves healthy evidence passes
      and a webhook/SLO breach names both blockers. Production stabilization
      evidence remains tracked by `DF6-G4`.

### DF6 Exit Gate

- [ ] `DF6-G1` Deployment, backup, restore, cutover, and rollback runbooks have
      been executed successfully in a production-like environment.
- [ ] `DF6-G2` Two-community and design-partner rollout evidence is approved.
- [x] `DF6-G3` Security and privacy review has no unresolved release blocker.
      Evidence: `docs/security-privacy-review.md` records the reviewed scope and
      disposition. The review resolved central structured-log credential leakage
      and missing Fresh browser security headers. The repeatable
      `deno task test:security-review` gate covers cryptographic integrity,
      authorization, read-only fencing, security headers, retention and legal
      holds, credential isolation and redaction, and deployment hardening.
- [ ] `DF6-G4` Deno meets the current SLOs throughout the stabilization window.

## DF7: Stabilization and v1.0.0 Release

- [x] `DF7-01` Remove Python source, Python tests, requirements files,
      virtualenv deployment paths, and SQLite production code after port
      completion. Evidence: the legacy Python application, tests, requirements,
      and host installer are removed. CI and release installation now use Deno
      and POSIX shell exclusively; `deno task test:release-boundary` prevents
      those dependencies from returning and confines SQLite to the supported
      offline importer.
- [x] `DF7-02` Retain the SQLite importer as a supported offline migration tool.
      Evidence: `deno task database-transfer` exports SQLite to the frozen
      manifest/JSONL format and transactionally imports PostgreSQL only with
      `--replace-target`; unit and disposable-database integration gates cover
      checksums, row totals, dependencies, ownership, constraints, and sequence
      reset without a Python runtime.
- [ ] `DF7-03` Contract obsolete PostgreSQL columns only in a later migration
      after rollback compatibility is no longer required.
- [x] `DF7-04` Update README, operations documentation, release notes, installer
      instructions, architecture diagrams, and support procedures for
      Deno/Fresh. Evidence: production guidance now uses Deno 2.9.4, Fresh,
      PostgreSQL, role-specific systemd services, PostgreSQL backup/restore, and
      the Deno-only offline importer. Documentation formatting, local links,
      referenced task names, installer syntax, and release packaging pass.
- [ ] `DF7-05` Record the final compatibility, accessibility, provider, rollout,
      restore, and rollback evidence in the release report.

### DF7 Exit Gate

- [x] `DF7-G1` No production process or package depends on Python or SQLite.
      Evidence: `deno task test:release-boundary` inventories source, tests,
      deployment files, task configuration, and CI, and rejects SQLite APIs
      outside `src/ops/database_transfer.ts`.
- [ ] `DF7-G2` All Deno checks, contract suites, browser suites, provider smoke
      tests, and platform audits pass.
- [ ] `DF7-G3` Backup restoration and release rollback are rehearsed against the
      final release candidate.
- [ ] `DF7-G4` Version `1.0.0` is tagged only after every preceding gate passes.

## Cutover and Rollback Model

Before cutover, Python and Deno share an expand-and-contract PostgreSQL schema.
Only one runtime may own a mutating job type or provider installation at a time.
Ownership is stored in PostgreSQL, leased, audited, and changed independently
from nginx routing.

Cutover order:

1. Disable new Python scheduler claims and allow active leases to drain.
2. Acquire and verify Deno job ownership one job type at a time.
3. Transfer Discord and Twitch installation ownership and verify health.
4. Switch nginx from the Python web service to Fresh.
5. Verify health, login, community switching, ingestion, moderation, jobs,
   announcements, and provider actions.
6. Continue automated parity and SLO monitoring through stabilization.

Rollback order:

1. Stop Deno from acquiring new mutating work.
2. Drain or expire Deno leases and return provider ownership to Python.
3. Switch nginx to the installed Python release.
4. Verify queues, providers, tenant isolation, and audit continuity.
5. Keep PostgreSQL as the source of truth. Do not reverse-migrate writes to
   SQLite after PostgreSQL production writes begin.

## Required Verification

1. Run the Python suite against SQLite and PostgreSQL until Python removal;
   after `DF7-01`, enforce the Deno-only release boundary in CI.
2. Run `deno fmt --check`, `deno lint`, `deno check`, and permission-bounded
   `deno test` suites.
3. Apply PostgreSQL migrations from empty and every supported prior version,
   including concurrent startup tests.
4. Verify SQLite import against synthetic two-community data and a sanitized
   production-size copy using counts, checksums, sequences, constraints, tenant
   inventory, search parity, and zero orphans.
5. Compare Python and Deno behavior for auth, permissions, ingestion, commands,
   moderation, scoring, jobs, HTTP responses, provider payloads, audits, and
   errors.
6. Run PostgreSQL fault and concurrency tests for job fairness, leases, retries,
   quotas, duplicate webhooks, provider ownership, rollback, and termination.
7. Run Fresh browser tests at desktop and mobile widths for workflows,
   no-JavaScript forms, islands, keyboard navigation, focus, landmarks, names,
   screen-reader semantics, overflow, CSP, and static assets.
8. Use fake Discord and Twitch services in CI, then execute real non-production
   smoke tests before provider cutover.
9. Rehearse PostgreSQL backup/restore, release rollback, nginx switchback,
   ownership handoff, and an aborted cutover.
10. Require zero unresolved severity-1 compatibility defects before tagging
    version `1.0.0`.

## Critical Path

1. Contract manifests, fixtures, parity harness, and CI.
2. Python PostgreSQL boundary and verified SQLite import.
3. Deno repository, tenant, security, and process foundations.
4. Fresh HTTP and dashboard parity.
5. Domain, queue, and scheduled-work correctness.
6. Provider ownership, compatibility, and smoke testing.
7. Packaging, staged rollout, cutover, rollback, and Python removal.

## Principal Source Files

- `runtime.ts`, `cli.ts`: role lifecycle and operational commands.
- `src/core/config.ts`: environment, service selection, secrets, and safe
  summaries.
- `src/data/database.ts`, `src/data/repository.ts`, `src/data/operations.ts`:
  migrations, scoped persistence, ownership, and operational controls.
- `src/core/contexts.ts`, `src/core/models.ts`: canonical TypeScript contract
  sources.
- `src/data/schema_scope.ts`, `src/security/surface_policy.ts`: tenancy and
  authorization inventories.
- `src/ops/database_transfer.ts`: deterministic SQLite export and verified
  PostgreSQL import rehearsal.
- `routes/`, `components/`, `src/web/`: routes, sessions, query services,
  mutations, and rendering.
- `src/jobs/`: job states, workers, retries, scheduling, and ingestion.
- `src/providers/discord/`, `src/domain/command_domain.ts`: Discord and command
  behavior.
- `src/providers/twitch/`: Twitch behavior and security contracts.
- `src/domain/analytics.ts`, `src/domain/signals.ts`, `src/domain/scoring.ts`:
  analytical domain behavior.
- `tests/`: source scenarios for fixtures and compatibility tests.
- `install.sh`, `deploy/`: packaging, systemd, nginx, and rollback behavior.

External non-production provider credentials, design partners, and production
infrastructure are required for the final provider and rollout gates. Mocks and
local tests alone cannot satisfy those gates.
