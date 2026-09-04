# QBot4K v1.0.0 Deno/Fresh Transition

QBot4K v1.0.0 moves the production application to Deno 2.9.4, Fresh 2.3.3,
TypeScript, and PostgreSQL. Separate permission-bounded web, jobs, analysis,
Discord, and Twitch roles replace the Python runtime while preserving frozen
HTTP, domain, tenant, provider, and operational contracts.

The release retains a Deno-only offline SQLite importer for migration archives.
The Python application, tests, requirements, and installer have been removed;
v1.0.0 has no Python runtime or package dependency.

## Professional Stream Operations

This archive extends the seven-track professional platform foundation with the
requested 15 live-community capabilities:

1. Real-time alert delivery through authenticated Server-Sent Events, without
   page reloads.
2. Stream sessions, a chronological event timeline, and a rolling
   30-minute/current chat-velocity graph.
3. Before-and-after conversation context for each message-backed finding.
4. One operations incident per coordinated campaign, with constituent message
   alerts grouped underneath it.
5. J/K finding navigation and W/T/B/C moderation/context shortcuts.
6. Pending, failed, completed, and Twitch/EventSub-confirmed moderation and
   control state.
7. Incident ownership, three-level escalation, operator shifts, and handoff
   notes that transfer open work.
8. Seeded Raid Lockdown, Spam Containment, and Recovery playbooks with run-state
   records.
9. One-click Twitch Shield Mode and follower/slow/normal chat-setting controls.
10. Automatically generated post-stream incident and community-health briefings.
11. Unique, new, returning, subscriber, VIP, and moderator stream cohorts.
12. Weighted raid/shared-audience edges and an operator-facing graph.
13. Seven-day moderator workload, action mix, workload balance, and
    enforcement-consistency reporting.
14. Discord, Slack, and generic HTTPS webhook escalation destinations with
    retryable deliveries.
15. Responsive, touch-sized emergency controls for mobile incident response.

Verification completed for this release:

- Deno formatting, linting, type checking, contract, browser, provider-fixture,
  PostgreSQL ownership, and SQLite-transfer gates are part of the release check.
- Fresh schema/migration, campaign grouping, context, cohorts, briefing,
  audience graph, handoff, playbook, raw archive/replay, and
  provider-confirmation fixtures are retained.
- Final production rollout, provider smoke tests, recovery rehearsal, and the
  stabilization window remain release gates in the transition plan.

See `docs/live-operations.md` for operator controls and deployment requirements,
and `docs/professional-platform.md` for the remaining managed-infrastructure
steps required before a paid multi-customer launch.
