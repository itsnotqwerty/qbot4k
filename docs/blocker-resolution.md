# Immediate Blocker Resolution

This revision resolves the operational blockers found during the intelligence-platform review.

## Changes

- Rule-generated moderation actions are queued as durable, retryable action jobs and executed by the active Discord or Twitch connector.
- Exhausted moderation jobs mark their associated moderation actions as failed instead of leaving them pending indefinitely.
- The action registry and worker initialize safely with Discord only, Twitch only, both connectors, or neither connector.
- Twitch reconnects after transient failures with exponential backoff up to 60 seconds. Invalid authorization remains a terminal state requiring reauthorization.
- Temporal signal history references only the observation that triggered each calculation. This bounds evidence-link creation to 25 rows per analyzed message instead of copying up to 2,500 rows.
- Maintenance now removes expired messages by event time, orphaned observations, temporal signal history, all but the latest expired score run per user, and old terminal processing jobs.
- `requirements.txt` declares the runtime WebSocket dependency.
- The analytical breadth extension is documented in `docs/analytical-breadth.md`.

## Verification

Install the runtime dependency and run the complete test suite:

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest -q
```

The regression suite includes connector-independent runtime shutdown, Twitch-only worker initialization, supervised reconnects, Discord and Twitch moderation dispatch, bounded evidence growth, and intelligence-data retention.
