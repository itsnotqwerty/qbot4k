# Operations and recovery

## System installation

On a Linux systemd host, run `sudo ./install.sh` from an extracted release. The
installer creates a non-login `qbot4k` system user, installs pinned Deno 2.9.4,
uses versioned release directories beneath `/opt/qbot4k/releases`, atomically
updates `/opt/qbot4k/current`, installs the unit in `/etc/systemd/system`, and
keeps mutable data in `/var/lib/qbot4k` and `/var/backups/qbot4k`.

The first install creates `/etc/qbot4k/qbot4k.env` with mode `0640`, generated
session and ingestion secrets, and a valid `jobs,analysis` profile. Subsequent
installs preserve that file. The unit passes this path to QBot4K with
`--env-file`, so the application parses it directly; the `EnvironmentFile=`
directive remains for compatibility with components that consume process
environment variables. The installer also writes a final `zz-` systemd drop-in
that resets legacy `ExecStart` and `ReadWritePaths` entries, starts separate
permission-bounded Deno roles, and keeps `/opt/qbot4k/data` writable only for
transition compatibility. To enable the dashboard, populate the Discord OAuth
client ID, client secret, exact HTTPS redirect URI, and operator guild IDs, then
add `web` to `QBOT_ENABLED_SERVICES` and restart the service:

```bash
sudoedit /etc/qbot4k/qbot4k.env
sudo systemctl restart qbot4k
sudo systemctl status qbot4k
```

Pass `--no-start` when staging a release that should be enabled but not yet
started. Old versioned release directories are retained for manual rollback;
point `/opt/qbot4k/current` to a previous release, run
`systemctl daemon-reload`, and restart each enabled `qbot4k-<role>.service`
after confirming database compatibility.

Deploy from a staged release directory with `sudo ./install.sh`. Use
`sudo ./install.sh --no-start` to install and enable the role units without
starting them before a blue/green switch. Direct checkout deployment is not a
supported production path.

## Deployment gate

1. Run `deno install --frozen` and confirm Deno 2.9.4.
2. Set a long random dashboard session secret and an independent ingestion
   token.
3. Keep each pilot community in persisted shadow mode and validate review-queue
   output.
4. Run `deno task check-config --env-file=/etc/qbot4k/qbot4k.env`,
   `deno task migrate --env-file=/etc/qbot4k/qbot4k.env`, and the complete test
   suite.
5. Confirm the Discord application has Guild Members, Moderation, Message
   Content, and Message Reactions intents enabled.
6. Promote individual rules from shadow/review to enforce only after sampling
   false positives.
7. Terminate TLS at a reverse proxy, set the exact HTTPS
   `QBOT_DISCORD_OAUTH_REDIRECT_URI`, and bind QBot4K to loopback.
8. Verify `/health/live`, `/health/ready`, and authenticated `/api/health`
   before routing analyst traffic.

Role unit and reverse-proxy templates are provided in `deploy/`. Their paths
match `install.sh`; adjust the proxy hostname for the target host.

## Database migrations

Schema initialization is additive and idempotent. Applied migration markers are
stored in `schema_migrations`. Always make a backup before deployment, stop the
old process, deploy code, run `deno task migrate`, and then start the new
process.

### PostgreSQL provisioning

Provision a dedicated database and two least-privilege roles: an owner used only
for migrations and an application role used by all runtime processes. Keep both
URLs in the deployment secret store rather than in the release directory.

```sql
CREATE ROLE qbot4k_owner LOGIN PASSWORD '<migration-secret>';
CREATE ROLE qbot4k_app LOGIN PASSWORD '<runtime-secret>';
CREATE DATABASE qbot4k OWNER qbot4k_owner;
\connect qbot4k
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO qbot4k_app;
ALTER DEFAULT PRIVILEGES FOR ROLE qbot4k_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qbot4k_app;
ALTER DEFAULT PRIVILEGES FOR ROLE qbot4k_owner IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO qbot4k_app;
```

Run migrations with the owner URL, then grant access to existing objects and
configure the application URL:

```bash
QBOT_DATABASE_URL="$QBOT_MIGRATION_DATABASE_URL" deno task migrate
psql "$QBOT_MIGRATION_DATABASE_URL" <<'SQL'
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO qbot4k_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO qbot4k_app;
SQL
export QBOT_DATABASE_URL='postgresql://qbot4k_app:...@database/qbot4k'
```

The migration command takes a PostgreSQL advisory lock, requires the executing
role to own the schema, rejects unmanaged tables or incompatible migration
history, and applies migrations transactionally. Apply it from an empty database
and each supported previous version in CI. Do not start a release when
`/health/ready` reports `migration_pending`.

### Analysis job handoff

Job ownership is controlled per `job_type` in PostgreSQL. To shadow the Deno
message analyzer while Python remains the owner, run:

```bash
deno task transfer-job-ownership analyze.message.created python \
    --shadow-runtime=deno --operator-id=<operator-id>
```

The Deno analysis role locks eligible live jobs, executes the deterministic
handler in a transaction that is always rolled back, and commits only an
idempotent `processing_job_shadow_runs` evidence row. After reviewing that
evidence, stop new Python claims, allow active leases to drain, and transfer
only this job type:

```bash
deno task transfer-job-ownership analyze.message.created deno \
    --shadow-runtime=none --operator-id=<operator-id>
```

The transfer refuses to proceed while that job type has an active lease and
writes an audit event. Roll back by draining active Deno leases and repeating
the command with owner `python` and shadow runtime `deno`.

For the one-time SQLite import, follow
[SQLite to PostgreSQL Transfer](sqlite-postgresql-transfer.md). Pause ingress
and consumers before the final export, compare all manifest counts and hashes,
and preserve the read-only SQLite source through the rollback window.

## Backups

The Deno jobs role uses `pg_dump --format=custom`, validates each archive with
`pg_restore --list`, writes a SHA-256 verification manifest beside it, and
retains the newest `QBOT_BACKUP_RETENTION_COUNT` generations. The default
`QBOT_BACKUP_INTERVAL_SECONDS=3600` establishes a one-hour recovery point
objective. The initial recovery time objective is four hours, including
provisioning an isolated target, restoring, validating, and switching traffic.

Never restore over the active database. Create an empty isolated database, then
run the Deno rehearsal command with the source in `QBOT_DATABASE_URL` and the
target in `QBOT_RESTORE_DATABASE_URL`:

```bash
createdb qbot4k_restore_check
QBOT_RESTORE_DATABASE_URL=postgres:///qbot4k_restore_check \
   deno task backup-restore -- \
   /var/backups/qbot4k/qbot4k-YYYYMMDDTHHMMSSZ.dump \
  >qbot4k-restore-rehearsal.json
```

The command verifies the archive checksum and list, refuses the source database
or a non-empty target, restores with ownership and privilege replay disabled,
and records archive size, SHA-256, schema version, exact table/row totals,
constraint status, RTO milliseconds, and RPO seconds. Only after validation
should operators pause writes, update `QBOT_DATABASE_URL`, start the Deno roles,
verify readiness and queue age, and then switch traffic. Keep the original
database read-only until the rollback window closes.

## Credential rotation

Rotate one credential class at a time and retain the previous value only for the
shortest overlap supported by its provider. Record the operator, timestamp,
affected installations, and verification result without recording either secret.

1. Rotate `QBOT_DASHBOARD_SESSION_SECRET` only during a declared session reset;
   all existing dashboard sessions become invalid.
2. Rotate `QBOT_INGEST_API_TOKEN` by updating senders and the service during a
   paused-ingress window, then verify an accepted request and a rejection using
   the retired token.
3. Rotate Discord and Twitch OAuth client secrets in the provider console,
   update the secret store, restart only the affected role, and complete a
   non-production authorization flow.
4. Rotate bot access and refresh tokens through the provider flow, persist any
   replacement refresh token, and verify reconnect, one read, and one permitted
   non-production action.
5. Rotate `QBOT_TWITCH_EVENTSUB_SECRET` by reconciling subscriptions against the
   new callback secret, then verify challenge, notification, replay rejection,
   and revocation handling.
6. Rotate `QBOT_CREDENTIAL_ENCRYPTION_KEY` with the audited staged re-encryption
   operation before removing the old key. Verify every encrypted installation
   can be decrypted before and after restart.
7. Rotate PostgreSQL credentials by creating or changing the inactive role,
   updating the secret store, restarting one Deno role at a time, and revoking
   the old credential only after all roles report ready.

## Ownership handoff

Never allow Python and Deno to own the same mutating job type or provider
installation concurrently. Ownership must be represented by the PostgreSQL lease
records delivered by `DF4-07` and `DF5-06`; do not simulate handoff with process
timing alone.

For each job type or installation:

1. Disable new claims by the current owner and record the ownership change
   request in the audit log.
2. Wait for active leases to finish or expire. Confirm there are no running rows
   still attributed to the old owner.
3. Acquire the Deno lease in PostgreSQL and verify its owner, expiry, and
   fencing token from a separate connection.
4. Start only the corresponding Deno role. Verify queue age, duplicate count,
   provider health, and audit continuity before transferring the next unit.
5. On failure, stop new Deno claims, drain or expire its leases, return
   ownership to Python, and verify the prior owner before restarting it.

Provider roles must additionally verify installation capability guards, webhook
or Gateway/IRC continuity, and one non-production outbound action. Provider and
live job transfer remains blocked until the corresponding non-production smoke
test and production ownership evidence are approved.

## Blue/green cutover

Prepare a release-specific Deno systemd unit and upstream while Python remains
the active nginx target. Both runtimes use the same PostgreSQL schema, but Deno
writes and provider actions remain disabled until their individual ownership
handoffs.

1. Back up PostgreSQL and restore it into an isolated database using the
   procedure above. Record measured recovery time and the newest recoverable
   transaction time.
2. Apply migrations with the schema-owner role. Start the green Deno web role
   and verify live/readiness health, authentication, tenant switching, static
   assets, and representative read-only workflows.
3. Run contract, browser, SLO, security, privacy, and shadow-read comparisons.
   Stop for any unresolved severity-1 difference.
4. Pause ingress that creates processing work. Drain Python jobs, then transfer
   each job type and provider installation using the ownership procedure above.
5. Resume ingestion and verify queue movement, webhook acceptance, moderation,
   announcements, provider actions, tenant isolation, and audit attribution.
6. Switch nginx to the green upstream, reload nginx, and verify public and
   authenticated health from outside the host.
7. Monitor error rate, latency, webhook acceptance, queue age, provider health,
   database saturation, and tenant SLOs throughout the rollback window. Keep the
   Python release installed but disabled.

The side-by-side Python/Deno staging procedure above applies only to the
pre-removal transition rehearsal recorded under `DF6-04`. Final Deno releases
are installed from their release directory with `sudo ./install.sh`; rollback
uses the retained prior release directory. The read-only profile fences all
non-observational HTTP methods and OAuth callbacks before controller execution.

After disabling the green role's read-only profile, run the fail-closed
preflight immediately before the forward nginx switch:

```bash
deno task cutover-preflight -- 15 >qbot4k-cutover-preflight.json
```

Do not switch unless it exits zero. It requires every observed processing job
type and active provider installation to be Deno-owned, every active provider to
have an unexpired lease holder, the web role to be writable, and all
rollback-window monitor signals to pass. Preserve the JSON report with release
evidence.

Execute the five cutover stages with the fail-fast sequence runner. Each path is
an executable, reviewed stage script containing that deployment's concrete
commands and arguments:

```bash
sudo /opt/qbot4k/current/deploy/execute-cutover.sh \
   --drain-command ./cutover/drain-python \
   --ownership-command ./cutover/acquire-deno-ownership \
   --preflight-command ./cutover/run-preflight \
   --switch-command ./cutover/switch-nginx \
   --verify-command ./cutover/verify-production
```

The runner stops at the first failure, so nginx cannot switch after failed
drain, ownership, or preflight stages. The verification stage must cover public
and authenticated health, login, ingestion, moderation, jobs, and both provider
paths. Archive every stage's output and the runner's JSON duration record.

Use the atomic switch helper for step 6, substituting the inactive role port and
deployment hostname:

```bash
sudo /opt/qbot4k/current/deploy/switch-nginx-upstream.sh \
   --config /etc/nginx/conf.d/qbot4k.conf \
   --target-port 8081 \
   --public-health-url https://intelligence.example.com/health/ready \
   >qbot4k-nginx-switch.json 2>&1
```

The helper's JSON output records `duration_ms`. On an automatic rollback the
same measurement record is written to standard error with
`"result":"rolled_back"`; capture both streams in rehearsal evidence.

## Release rollback

Rollback keeps PostgreSQL as the source of truth. Never import post-cutover
writes back into SQLite.

1. Stop nginx from sending new requests to Deno if the web surface is affected.
2. Disable Deno claims and outbound provider actions. Let active work complete
   or expire; do not terminate a process while it still owns a renewable lease.
3. Return job and provider ownership to Python one unit at a time and verify the
   database owner, expiry, and fencing token before starting Python consumers.
4. Use `switch-nginx-upstream.sh` with the prior upstream port. It validates the
   target, nginx configuration, reload, and public readiness; then verify
   authentication, ingestion, queue movement, tenant isolation, and provider
   continuity.
5. Preserve Deno logs, audit rows, failed payloads, ownership history, and SLO
   measurements for incident review. Continue monitoring until queues and
   provider sessions are stable.

If a schema change is not readable by the previous Python release, rollback is
blocked: stop the cutover before writes begin and deploy a forward-compatible
fix. Restore PostgreSQL only for database loss or corruption, never as a normal
application rollback mechanism.

## Monitoring

Monitor public readiness plus authenticated `/api/health`, processing jobs that
exhaust retries, worker outcome/latency metrics, pending moderation actions,
open reviews, connector reconnects, backup age, and model-evaluation sample
size/false-positive rate. Treat a failed database integrity result, missing
heartbeat acknowledgement, or authentication close as an incident.

Capture a fail-closed PostgreSQL snapshot every 15 minutes throughout cutover
and the rollback window. This example records 25 samples spanning six hours:

```bash
deno task cutover-monitor -- 15 25 900 >>qbot4k-cutover-monitor.jsonl
```

The arguments are evidence window minutes, sample count, and interval seconds.
The command exits immediately with a nonzero status and names blockers when job
errors exceed 1%, runnable queue age exceeds 15 minutes, any active provider is
unhealthy, any active community lacks a fresh complete SLO set or has a breach,
webhook acceptance exceeds one second, or database connections exceed 80% of
capacity. Preserve the JSONL with release evidence.

The `open_alerts` counter represents untriaged `open` alerts only. Acknowledged,
suppressed, in-case, and resolved alerts remain queryable but do not inflate
this counter. See `alert-policy.md` for analytical warm-up, thresholds, stable
keys, and automatic expiry behavior.

## Incident response

- Set `community_policy_settings.moderation_shadow_mode=1` for the affected
  community to stop automatic enforcement without disabling analysis elsewhere.
- Rotate bot, OAuth, session, and ingestion credentials if exposure is
  suspected.
- Preserve the database and logs before replaying failed observations.
- Use dedupe keys and immutable observation IDs when correlating alerts, cases,
  and source evidence.
