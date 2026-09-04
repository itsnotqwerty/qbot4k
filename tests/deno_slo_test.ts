import {
  calculateCountSample,
  calculateFreshnessSample,
  calculateLatencySample,
  calculatePercentageSample,
} from "../src/domain/slo.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      `expected ${JSON.stringify(expected)}, received ${
        JSON.stringify(actual)
      }`,
    );
  }
}

Deno.test("latency SLO uses nearest-rank p95 and lower-is-better status", () => {
  assertEquals(calculateLatencySample("latency", 19, [20, 1, 18, 2]), {
    metricName: "latency",
    value: 20,
    targetValue: 19,
    status: "breached",
    evidenceCount: 4,
  });
  assertEquals(calculateLatencySample("latency", 10, []), {
    metricName: "latency",
    value: 0,
    targetValue: 10,
    status: "no_data",
    evidenceCount: 0,
  });
});

Deno.test("percentage SLO is inclusive and higher is better", () => {
  assertEquals(calculatePercentageSample("health", 75, [1, 1, 1, 0]), {
    metricName: "health",
    value: 75,
    targetValue: 75,
    status: "met",
    evidenceCount: 4,
  });
});

Deno.test("count and freshness SLOs are inclusive and lower is better", () => {
  assertEquals(calculateCountSample("dead_letters", 0, 1), {
    metricName: "dead_letters",
    value: 1,
    targetValue: 0,
    status: "breached",
    evidenceCount: 1,
  });
  assertEquals(calculateFreshnessSample(null), {
    metricName: "backup_freshness_seconds",
    value: 0,
    targetValue: 86400,
    status: "no_data",
    evidenceCount: 0,
  });
});
