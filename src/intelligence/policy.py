from __future__ import annotations


TOPIC_ALERT_POLICIES: dict[str, tuple[int, int, float]] = {
    # topic kind: (minimum observations, minimum contexts, minimum unusualness)
    "term": (8, 3, 12.0),
    "phrase": (5, 2, 10.0),
    "domain": (3, 2, 8.0),
}
TOPIC_ALERT_LIMIT = 10
TOPIC_BASELINE_MIN_ACTIVE_DAYS = 3

COHORT_MIN_SAMPLE_SIZE = 6
COHORT_MIN_CONFIDENCE = 0.70

COORDINATION_ALERT_MIN_EVIDENCE = 6
COORDINATION_ALERT_MEDIUM_EVIDENCE = 10

AUTO_EXPIRED_DISPOSITION = "expired"

