# Live Operations Command Center

## Operator surface

Open `/live-ops` with a signed operator session. The page opens an authenticated Server-Sent Events connection to `/api/live-ops/stream`; the server emits a complete initial snapshot and then emits changed snapshots with one-second detection latency. The browser reconnects automatically after the bounded 30-second stream rotates.

The console includes:

- current and 30-minute chat velocity;
- stream lifecycle and moderation timeline;
- individual findings with before/after conversation context;
- campaign-level incidents instead of one open alert per matching message;
- incident ownership, escalation, playbooks, and shift handoff state;
- Twitch-confirmed moderation and channel-control results;
- stream cohorts, raid/shared-audience graph, moderator workload, enforcement consistency, and post-stream briefings;
- responsive emergency controls suitable for a phone-sized viewport.

## Keyboard workflow

| Key | Action |
|---|---|
| `J` / `K` | Select next / previous finding |
| `W` | Queue warning for the selected message |
| `T` | Queue a ten-minute timeout |
| `B` | Queue a ban |
| `C` | Load full conversation context |

Actions are queued idempotently through the existing action worker. `pending_provider_confirmation` is not success. A Twitch action becomes confirmed only after the Helix handler returns successfully; a matching EventSub moderation event adds independent provider-event confirmation.

## Emergency controls and scopes

The Shield Mode and chat-settings endpoints use the configured Twitch bot grant and resolve `QBOT_TWITCH_CHANNELS` as the default broadcaster. The moderator account represented by the access token must be permitted on that channel.

Provision only the capabilities you enable:

- `moderator:manage:shield_mode` for Shield Mode;
- `moderator:manage:chat_settings` and `moderator:read:chat_settings` for chat settings;
- `moderator:manage:banned_users` for warnings, timeouts, and bans;
- the EventSub read scopes required by the subscribed chat and moderation event types.

Smoke-test Shield on/off, follower mode, slow mode, timeout, and ban on a non-production channel. Confirm that `twitch_control_actions.confirmed_at` and `moderation_actions.provider_confirmed_at` populate as expected.

## Playbooks

- **Raid Lockdown:** enables Shield Mode, ten-minute follower mode, ten-second slow mode, and forces incident notification delivery.
- **Spam Containment:** records the link-restriction policy step, enables five-second slow mode, and assigns the active operator as incident commander.
- **Recovery:** disables Shield Mode and slow mode, then generates the latest stream briefing.

Every activation creates a `raid_playbook_runs` record. Automated control steps store Twitch provider responses. A failed provider call returns an error to the console and leaves its underlying control action marked failed for review.

## Escalation destinations

Admins can add Discord, Slack, or generic HTTPS webhook destinations from the command center. Incident creation queues deliveries at or above each destination's severity floor; manual escalation forces a new queue decision. The maintenance job dispatches pending deliveries, records provider status, and retries transient failures up to five attempts.

Treat webhook URLs as secrets. For a managed deployment, place destinations behind an external secrets/notification relay and restrict database and backup access.

## Post-stream output

When a `stream.ended` observation closes a session, the platform refreshes audience cohorts and creates a briefing containing duration, messages, unique chatters, average velocity, moderation volume, incidents, cohorts, and recommendations. Briefings remain available in the live console and `post_stream_briefings` table for downstream reporting.
