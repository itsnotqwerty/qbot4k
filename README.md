# QBot4K

QBot4K is a Python moderation and chat operations system for Twitch and Discord with a built-in operator dashboard and SQLite persistence.

It provides:

- Message ingestion from Discord and Twitch
- A normalized event pipeline backed by SQLite
- Rule-driven moderation findings (review queue and auto-actions)
- Canonical user profiles with cross-platform account linking
- Reputation scoring and power-user flagging
- Maintenance jobs for retention, rollups, and backups
- Twitch live announcement delivery into Discord
- A server-rendered dashboard with Discord OAuth login

## Current Project Status

The current codebase is a working foundation and integration slice:

- Core CLI, config validation, database schema, connectors, dashboard routes, and background jobs are implemented.
- Unit and integration tests exist across foundation, ingestion, identity, jobs, commands, and dashboard auth/UI flows.
- The repository currently includes 100 test methods under tests/.

## Repository Layout

- src/__main__.py: CLI bootstrap and runtime orchestration
- src/config.py: Environment loading and validation
- src/db.py: SQLite schema, ingestion persistence, moderation recording, command storage, helper queries
- src/commands.py: Command parsing, reply rendering, template expansion, command management
- src/discord.py: Discord gateway connector, message normalization, boost reward flow, moderation execution
- src/twitch.py: Twitch IRC connector, parsing, join workflow, spam auto-timeout
- src/moderation.py: Moderation rule evaluation
- src/health.py: Health server and route dispatch to dashboard
- src/jobs.py: Retention cleanup, metrics rollups, backups, and Twitch live announcement jobs
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
# Required when jobs+twitch+discord are all enabled
QBOT_DISCORD_GUILD_IDS=
# Required only when discord service is enabled
QBOT_DISCORD_BOT_TOKEN=
# Optional: allow bot-authored Discord messages to be ingested
QBOT_DISCORD_ALLOW_BOT_MESSAGES=false

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
- jobs runs maintenance once at startup and then every 300 seconds
- jobs also runs Twitch live announcement checks when both discord and twitch services are enabled
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
- /auth/discord/callback

POST routes:

- /logout
- /dashboard/go-live
- /users/link
- /users/{user_id}/moderation
- /commands

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

## Commands and Templating

QBot4K supports two command families:

- Structured command definitions (for example, credit) stored in command_definitions
- Simple command definitions stored in simple_command_definitions

Built-in management commands:

- !addcom !name response
- !editcom !name response
- !delcom !name
- !alias !newcommand !oldcommand

Notes:

- Command editing is restricted to dashboard operators.
- Discord renders structured command output as embeds and simple command output as plaintext content.
- Twitch renders command output as plaintext.

Template capabilities:

- Standard placeholders such as {display_name}, {author_username}, {platform}, {query}, {score}, {power_user}
- Random number ranges with {min..max} and query-driven bounds via {query}
- HTTP calls inside templates with syntax {GET}(url) or {POST}(url)
- Optional JSON selectors in brackets, including alias mapping for extracted values
- Per-render request caching for repeated identical HTTP calls

## Ingestion and Moderation

### Ingestion

- Discord and Twitch messages are normalized and persisted into messages.
- Platform accounts are upserted in platform_accounts.
- Each platform account is auto-linked to a canonical user on first message.
- Discord message attachments are persisted in message_attachments.

### Moderation Rules

Supported rule types:

- exact_term
- banned_phrase
- link_restriction
- duplicate_message (same_user_same_content pattern)
- builtin egregious content rule

Result behavior:

- A matching rule creates a row in rule_matches.
- Rules with auto_enforce_action create moderation_actions rows with pending status.
- Rules without auto action create review_queue rows with open status.

### Reputation

- Score updates are event-based and recorded in reputation_events.
- Message content can trigger positive or negative score deltas.
- Moderation findings apply additional penalties.
- Candidate flag is updated when score reaches threshold.

Current behavior note:

- The implementation enforces fixed high scores for specific handles through power-user logic.

## Twitch Integration

### Join Workflow

When a message equal to !join is posted in QBOT_TWITCH_JOIN_COMMAND_CHANNEL:

- The sender username is stored as a requested Twitch channel in twitch_channels.
- On active IRC runtime join, channel status can transition to active.

Bootstrap channels from QBOT_TWITCH_CHANNELS are seeded as active entries.

### Streamboo Viewer Spam Auto-Moderation

For persisted non-moderator Twitch messages, if content contains both:

- streamboo
- viewers

QBot4K sends a 600-second timeout command and records a completed moderation action with reason streamboo_viewer_spam.

## Discord Integration

### Server Boost Rewards

QBot4K tracks server bump requests and fulfillment:

- A user bump command request is recorded as pending in server_boost_requests.
- A matching bump success bot message rewards the pending requester.
- Reward completion updates status and contributes reputation via the standard event pipeline.

## Maintenance and Background Jobs

### Maintenance Run

Each maintenance run performs:

- Message retention purge using QBOT_MESSAGE_RETENTION_DAYS
- Audit-log retention purge using QBOT_AUDIT_RETENTION_DAYS
- Metrics rollup refresh for:
  - messages_total
  - open_reviews
  - pending_actions
- SQLite backup creation in QBOT_BACKUP_DIR with JSON metadata and SHA-256

### Twitch Live Announcements

The live announcement job:

- Requires both Discord and Twitch bot tokens.
- Resolves target Discord guild IDs from configured IDs plus bot-discovered guilds.
- Fetches active stream state for its_not_qwerty via Twitch Helix.
- Picks the best Discord channel using stream-title and game-name token overlap with fallback channel names.
- Sends @here live notifications into Discord.
- Deduplicates automatic announcements per stream and guild using twitch_live_announcements.
- Supports manual dashboard-triggered announcements via POST /dashboard/go-live.
- Attempts Twitch token refresh when credentials are configured.

## Database

SQLite is initialized with WAL mode and includes tables for:

- users
- platform_accounts
- operator_accounts
- messages
- message_attachments
- welcome_events
- twitch_channels
- moderation_rules
- rule_matches
- moderation_actions
- reputation_events
- user_notes
- review_queue
- server_boost_requests
- command_definitions
- simple_command_definitions
- discord_channels
- twitch_live_announcements
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
- When jobs+twitch+discord are enabled together, QBOT_DISCORD_GUILD_IDS is required.
- Twitch OAuth refresh support requires QBOT_TWITCH_REFRESH_TOKEN, QBOT_TWITCH_CLIENT_ID, and QBOT_TWITCH_CLIENT_SECRET.
- For local dashboard exposure (for OAuth callback testing), you can tunnel port 8080 with tools like ngrok.

## Documentation

For project planning and architecture docs, see:

- docs/spec.md
- docs/design.md
- docs/roadmap.md
