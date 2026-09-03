# Operations and recovery

## System installation

On a Linux systemd host with Python 3.11 or newer, run `sudo ./install.sh` from
an extracted release. The installer creates a non-login `qbot4k` system user,
uses versioned release directories beneath `/opt/qbot4k/releases`, atomically
updates `/opt/qbot4k/current`, installs the unit in `/etc/systemd/system`, and
keeps mutable data in `/var/lib/qbot4k` and `/var/backups/qbot4k`.

The first install creates `/etc/qbot4k/qbot4k.env` with mode `0640`, generated
session and ingestion secrets, and a valid `jobs,analysis` profile. Subsequent
installs preserve that file. The unit passes this path to QBot4K with
`--env-file`, so the application parses it directly; the `EnvironmentFile=`
directive remains for compatibility with components that consume process
environment variables. The installer also writes a final `zz-` systemd
drop-in that resets legacy `ExecStart` and `ReadWritePaths` entries, uses an
absolute Python entrypoint, and keeps `/opt/qbot4k/data` writable for existing
installations that still store their database there. To enable the dashboard, populate the Discord
OAuth client ID, client secret, exact HTTPS redirect URI, and operator guild
IDs, then add `web` to `QBOT_ENABLED_SERVICES` and restart the service:

```bash
sudoedit /etc/qbot4k/qbot4k.env
sudo systemctl restart qbot4k
sudo systemctl status qbot4k
```

Pass `--no-start` when staging a release that should be enabled but not yet
started. Set `QBOT_PYTHON_BIN=/path/to/python3.11` when the desired interpreter
is not the first supported `python3` on `PATH`. Old versioned release
directories are retained for manual rollback; point `/opt/qbot4k/current` to a
previous release, run `systemctl daemon-reload`, and restart after confirming
database compatibility.

For deployments that run directly from a checked-out project directory, use
the Python deploy kit instead:

```bash
python deploy/install.py --env .env.production --domain intelligence.example.com --http-only --dry-run
sudo python3 deploy/install.py --env .env.production --domain intelligence.example.com --http-only
```

It creates `.venv`, installs `requirements.txt`, securely copies the required
environment file, and renders project-owned systemd and nginx configuration.
Rerun without `--http-only` after certificates exist. nginx is validated before
the service is restarted, and a failed validation restores the prior proxy
configuration. See `deploy/README.md` for all options.

## Deployment gate

1. Install `requirements.txt` in a clean virtual environment.
2. Set a long random dashboard session secret and an independent ingestion token.
3. Keep each pilot community in persisted shadow mode and validate review-queue output.
4. Run `python -m src --env-file /etc/qbot4k/qbot4k.env check-config`,
   `python -m src --env-file /etc/qbot4k/qbot4k.env init-db`, and the complete
   test suite.
5. Confirm the Discord application has Guild Members, Moderation, Message Content, and Message Reactions intents enabled.
6. Promote individual rules from shadow/review to enforce only after sampling false positives.
7. Terminate TLS at a reverse proxy, set the exact HTTPS `QBOT_DISCORD_OAUTH_REDIRECT_URI`, and bind QBot4K to loopback.
8. Verify `/health/live`, `/health/ready`, and authenticated `/api/health` before routing analyst traffic.

Example unit and reverse-proxy configurations are provided in `deploy/qbot4k.service` and `deploy/Caddyfile.example`. The unit paths match `install.sh`; adjust the proxy hostname for the target host.

## Database migrations

Schema initialization is additive and idempotent. Applied migration markers are stored in `schema_migrations`. Always make a backup before deployment, stop the old process, deploy code, run `python -m src init-db`, and then start the new process.

## Backups

Backups use SQLite's online backup API, run `PRAGMA integrity_check`, write SHA-256 metadata, and retain the newest `QBOT_BACKUP_RETENTION_COUNT` generations. Restore into a new path first:

```bash
cp data/backups/qbot4k-YYYYMMDDTHHMMSSZ.sqlite3 data/restore-check.sqlite3
sqlite3 data/restore-check.sqlite3 'PRAGMA integrity_check;'
```

Stop the service before switching `QBOT_DATABASE_PATH` to the verified restore.

## Monitoring

Monitor public readiness plus authenticated `/api/health`, processing jobs that exhaust retries, worker outcome/latency metrics, pending moderation actions, open reviews, connector reconnects, backup age, and model-evaluation sample size/false-positive rate. Treat a failed database integrity result, missing heartbeat acknowledgement, or authentication close as an incident.

The `open_alerts` counter represents untriaged `open` alerts only. Acknowledged, suppressed, in-case, and resolved alerts remain queryable but do not inflate this counter. See `alert-policy.md` for analytical warm-up, thresholds, stable keys, and automatic expiry behavior.

## Incident response

- Set `community_policy_settings.moderation_shadow_mode=1` for the affected
    community to stop automatic enforcement without disabling analysis elsewhere.
- Rotate bot, OAuth, session, and ingestion credentials if exposure is suspected.
- Preserve the database and logs before replaying failed observations.
- Use dedupe keys and immutable observation IDs when correlating alerts, cases, and source evidence.
