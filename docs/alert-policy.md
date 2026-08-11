# Intelligence alert policy

The primary alert queue is reserved for findings that cross minimum evidence, diffusion, and confidence gates. Broad analytical tables remain available on `/analytics`; a row appearing there does not automatically make it an analyst alert.

## Emerging topics

Topic alerts remain disabled until the database contains text observations on at least three distinct baseline days within the preceding seven-day baseline window. Once warm, the following gates apply:

| Topic kind | Minimum observations | Minimum contexts | Minimum unusualness |
|---|---:|---:|---:|
| Term | 8 | 3 | 12 |
| Phrase | 5 | 2 | 10 |
| Domain | 3 | 2 | 8 |

At most ten emerging-topic alerts are active from each refresh. Topics are ranked by unusualness, observation count, and stable key. Alerts use one stable deduplication key per topic; subsequent refreshes update that record rather than adding daily copies.

If a topic no longer qualifies or falls outside the top ten, an open, acknowledged, or suppressed alert is resolved automatically with disposition `expired`. Alerts attached to cases and alerts manually resolved by an analyst are not overridden. A previously expired alert can reopen if the topic materially recurs.

## Cohort anomalies

Cohort anomalies require at least six members, five comparison peers after leave-one-out exclusion, confidence of at least 0.70, and an absolute z-score of at least 3 before alerting. Self-history baselines also require at least six samples. Cohort alerts use stable keys and automatically expire when the finding stops qualifying.

## Coordination patterns

Coordination alerts require six supporting relationship observations. They remain low severity until ten observations and are updated in place as evidence grows. Older alerts created below the six-observation gate expire during the next analytics refresh.

## Queue semantics

The dashboard's `Untriaged alerts` metric counts only alerts with status `open`. Acknowledged, suppressed, in-case, expired, and manually resolved alerts remain available through the status filter without inflating the primary queue count. The intelligence page opens with the `open` filter selected; choose `All statuses` to inspect history.

## Upgrade behavior

The first analytics refresh after upgrading evaluates existing analytical alerts against this policy. Legacy daily topic, cohort, graph-bridge, and under-threshold coordination alerts are preserved but automatically resolved as `expired`. This cleanup is audited as `alert.auto_expired` and does not delete evidence.

