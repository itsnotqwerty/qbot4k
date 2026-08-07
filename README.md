# QBot4K

QBot4K is a Python moderation and chat operations system for Twitch and Discord with a built-in operator dashboard and SQLite persistence.

It provides:

- Message ingestion from Discord and Twitch
- A normalized event pipeline backed by SQLite
- Rule-driven moderation findings (review queue and auto-actions)
- Canonical user profiles with cross-platform account linking
- Reputation scoring and power-user flagging
- Maintenance jobs for retention, rollups, and backups
- A server-rendered dashboard with Discord OAuth login

## Current Project Status

The current codebase is a working foundation and integration slice:

- Core CLI, config validation, database schema, connectors, dashboard routes, and maintenance jobs are implemented.
- Unit and integration tests exist across foundation, ingestion, identity, jobs, and dashboard auth/UI flows.
- The test suite currently passes:

	Ran 42 tests in about 5 seconds, result: OK

## Repository Layout

- src/__main__.py: CLI bootstrap and runtime orchestration
- src/config.py: Environment loading and validation
- src/db.py: SQLite schema, ingestion persistence, moderation recording, helper queries
- src/discord.py: Discord gateway connector and message normalization
- src/twitch.py: Twitch IRC connector, parsing, and join workflow
- src/moderation.py: Moderation rule evaluation
- src/health.py: Health server and route dispatch to dashboard
- src/jobs.py: Retention cleanup, metrics rollups, and backup generation
- src/intelligence/userprofiles.py: Canonical user linking and notes
- src/intelligence/powerusers.py: Reputation and score update logic
- src/dashboard/: Server-rendered dashboard and JSON APIs
- tests/: Unit and integration tests

## Requirements

- Python 3.11+ recommended
- SQLite (via Python stdlib)
- websocket-client Python package for Discord gateway connectivity

Install dependency:

```bash
python -m pip install websocket-client
```

## Quickstart

1. Create a local environment file in the repository root:

```env
QBOT_DATABASE_PATH=./var/qbot4k.sqlite3
QBOT_BACKUP_DIR=./var/backups
QBOT_LOG_LEVEL=INFO

# Enable only what you want to run locally.
# Examples:
# QBOT_ENABLED_SERVICES=jobs
# QBOT_ENABLED_SERVICES=web,jobs
# QBOT_ENABLED_SERVICES=web,jobs,discord,twitch
QBOT_ENABLED_SERVICES=web,jobs

# Dashboard bind settings
QBOT_DASHBOARD_HOST=127.0.0.1
QBOT_DASHBOARD_PORT=8080

# Required when web is enabled
QBOT_DASHBOARD_SESSION_SECRET=replace-me
QBOT_DISCORD_OAUTH_CLIENT_ID=replace-me
QBOT_DISCORD_OAUTH_CLIENT_SECRET=replace-me
# Optional; if omitted the app infers callback URL from request host
QBOT_DISCORD_OAUTH_REDIRECT_URI=http://127.0.0.1:8080/oauth/discord/callback

# Required when web is enabled: restrict dashboard login to approved Discord guilds
QBOT_OPERATOR_GUILD_IDS=replace-with-guild-id

# Twitch options
QBOT_TWITCH_CHANNELS=its_not_qwerty
QBOT_TWITCH_JOIN_COMMAND_CHANNEL=its_not_qwerty
# Required only when twitch service is enabled
QBOT_TWITCH_BOT_TOKEN=
# Optional: token refresh support for long-running Twitch service/jobs
QBOT_TWITCH_REFRESH_TOKEN=
QBOT_TWITCH_CLIENT_ID=
QBOT_TWITCH_CLIENT_SECRET=

# Discord options
QBOT_DISCORD_GUILD_IDS=
# Required only when discord service is enabled
QBOT_DISCORD_BOT_TOKEN=

# Retention settings (days)
QBOT_MESSAGE_RETENTION_DAYS=90
QBOT_AUDIT_RETENTION_DAYS=365
```

2. Validate configuration:

```bash
python src/__main__.py check-config
```

3. Initialize database schema:

```bash
python src/__main__.py init-db
```

4. Start the app:

```bash
python src/__main__.py run
```

5. Open:

- Dashboard and entry route: http://127.0.0.1:8080/dashboard
- Health routes: http://127.0.0.1:8080/health, /health/live, /health/ready

## CLI Commands

Run all commands through src/__main__.py.

```bash
python src/__main__.py check-config
python src/__main__.py init-db
python src/__main__.py run
python src/__main__.py run --once
python src/__main__.py watch --interval 1.0
python src/__main__.py watch --path src --path .env
```

Command summary:

- check-config: Loads config and prints a safe JSON summary
- init-db: Creates or updates schema and prints known table names
- run: Initializes components and starts enabled services
- run --once: Prints a health snapshot and exits
- watch: Restarts the app when source or watched files change

## Services and Runtime Behavior

QBot4K supports these service flags in QBOT_ENABLED_SERVICES:

- web
- jobs
- twitch
- discord

Behavior details:

- web starts the health server and dashboard routes
- jobs runs maintenance once at startup and then every 5 minutes
- twitch launches Twitch IRC connector loop
- discord launches Discord gateway connector loop

If no long-running worker is enabled, run prints snapshot output and exits.

## Dashboard and APIs

HTML routes:

- /
- /dashboard
- /users
- /users/{user_id}
- /moderation
- /commands
- /login
- /oauth/discord/callback

JSON routes:

- /api/overview
- /api/users
- /api/users/{user_id}
- /api/users/link
- /api/users/{user_id}/notes
- /api/moderation/actions
- /api/moderation/reviews
- /api/health

Auth model:

- Dashboard uses Discord OAuth and a signed session cookie.
- Role assignment is based on configured operator guild IDs and guild permissions.
- QBOT_OPERATOR_GUILD_IDS must be configured when the web service is enabled; users outside those guilds are denied access.

Command templates:

- /commands lets admins edit shared command templates used by both Discord and Twitch.
- The built-in `credit` command stores its title and message templates in SQLite.
- Discord renders the response as an embed, while Twitch renders the same command as plaintext.

## Ingestion and Moderation

### Ingestion

- Discord and Twitch messages are normalized and persisted into messages.
- Platform accounts are upserted in platform_accounts.
- Each platform account is auto-linked to a canonical user on first message.

### Moderation Rules

Supported rule types:

- exact_term
- banned_phrase
- link_restriction
- duplicate_message (same_user_same_content pattern)

Result behavior:

- A matching rule creates a row in rule_matches.
- Rules with auto_enforce_action create moderation_actions rows with pending status.
- Rules without auto action create review_queue rows with open status.

### Reputation

- Score updates are event-based and recorded in reputation_events.
- Message content can trigger positive or negative score deltas.
- Moderation findings apply additional penalties.
- Candidate flag is updated when score reaches threshold.

Note:

- The current implementation pins specific handles (apollyon, its_not_qwerty) to max score.

## Twitch Join Workflow

When a message equal to !join is posted in QBOT_TWITCH_JOIN_COMMAND_CHANNEL:

- The sender username is stored as a requested twitch channel in twitch_channels.
- On active IRC runtime join, channel status can transition to active.

Bootstrap channels from QBOT_TWITCH_CHANNELS are seeded as active entries.

## Maintenance Jobs

Each maintenance run performs:

- Message retention purge using QBOT_MESSAGE_RETENTION_DAYS
- Audit-log retention purge using QBOT_AUDIT_RETENTION_DAYS
- Metrics rollup refresh for:
	- messages_total
	- open_reviews
	- pending_actions
- SQLite backup creation in QBOT_BACKUP_DIR with JSON metadata and SHA-256

## Database

SQLite is initialized with WAL mode and includes tables for:

- users
- platform_accounts
- operator_accounts
- messages
- twitch_channels
- moderation_rules
- rule_matches
- moderation_actions
- reputation_events
- user_notes
- review_queue
- audit_log
- metrics_rollups

Schema is created automatically by init-db, run, and other entry points that open the database.

## Running Tests

Run the full test suite:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Operational Notes

- Keep secrets in local environment variables or a local .env file that is not committed.
- Enable only the services needed for local workflows.
- For local dashboard exposure (for OAuth callback testing), you can tunnel port 8080 with tools like ngrok.

## Documentation

For project planning and architecture docs, see:

- docs/spec.md
- docs/design.md
- docs/roadmap.md
