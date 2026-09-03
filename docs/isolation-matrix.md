# Authorization and Isolation Matrix

The executable surface inventory is `src/surface_policy.py`. Every dashboard
handler routed by `DashboardApp.dispatch` has a capability, guard, scope, and
surface kind. The same inventory classifies bot commands, scheduled jobs,
direct lookups, and provider actions.

| Surface | Guard | Tenant source | Capability family |
|---|---|---|---|
| HTML and JSON routes | Validated dashboard session | Active session community | Dashboard capability catalog |
| Exports and SSE | Validated dashboard session | Active session community | Export or live-operations capability |
| External event ingestion | Community-bound API client or admin session | Required payload `community_id` | `events.write` |
| Twitch EventSub | HMAC and replay validation | Active broadcaster installation | `events.ingest` |
| Bot command reads | Active installation context | Command context community | Read capability |
| Bot command mutations | Tenant operator permission | Command context community | `settings.manage` |
| Scheduled jobs | System runner | Per-community or per-installation query | Job capability |
| Direct object lookups | Compound tenant lookup | Explicit community or installation | Domain capability |
| Provider actions | Installation capability check | Explicit installation | Moderation, announcement, or live control |

`tests/test_surface_policy.py` parses the dashboard dispatcher and fails if a
new routed handler is absent from the inventory or does not reach its declared
guard. It also requires coverage for all non-HTTP surface categories.

Bearer ingestion keys are bound to one community. Requests must include the same
`community_id`; missing or cross-community ownership fails closed. Legacy
unbound API clients cannot ingest tenant data. Bot command mutation contexts
must also carry an explicit community and the operator must hold
`settings.manage` there.
