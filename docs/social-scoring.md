# Social scoring model v2

The social score is now a materialized intelligence product, not an event-by-event counter.

## Data flow

1. Connectors persist immutable observations and normalized messages.
2. Message analysis records behavioral and moderation evidence.
3. Signal analyzer v2 calculates persistent profile signals and temporal windows.
4. Intelligence workflows create evidence-backed alerts and relationships.
5. Social score model v2 recalculates the score from current signals, temporal risk, and adjudicated intelligence.
6. The current value is cached on `users`; every calculation and component is retained for audit and explanation.

## Guardrails

- The score never reads its previous value as an input.
- Composite risk never reads the social score as an input.
- Commands and empty messages do not count as score-eligible participation.
- Activity rewards are capped.
- Negative-content and moderation penalties are capped independently.
- Risk blends 24-hour, 7-day, 30-day, and lifetime windows.
- Open alerts do not change the score. Only confirmed or escalated dispositions do.
- Ordinary relationships and co-activity never create guilt-by-association penalties.
- Power-user status requires both a score of at least 700 and at least 50% evidence confidence.
- Usernames do not receive privileged scores or exemptions.

## Persistence

- `users.score_confidence`, `users.score_model_version`, and `users.score_calculated_at` describe the materialized score.
- `social_score_runs` retains each model output.
- `social_score_components` retains raw values, normalized values, weights, contributions, confidence, evidence counts, and source metadata.
- `derived_signals` stores the current versioned profile measurements.
- `derived_signal_windows`, `derived_signal_history`, and `derived_signal_evidence` retain temporal and evidentiary context.

Existing users with no model-v2 score are backfilled once during database initialization. Subsequent observation analysis, account-link changes, server-boost evidence, Twitch moderation evidence, and alert dispositions recalculate affected profiles.
