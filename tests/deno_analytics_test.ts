import {
  assertAlmostEquals,
  assertEquals,
  assertThrows,
} from "jsr:@std/assert@1.0.14";
import {
  calculateCohortBaseline,
  calculateGraphMetrics,
  calculatePeerAnomaly,
} from "../src/domain/analytics.ts";

Deno.test("cohort baseline uses population deviation and nearest-rank p90", () => {
  assertEquals(calculateCohortBaseline([1, 1, 1, 3, 3, 3]), {
    sampleSize: 6,
    meanValue: 2,
    stddevValue: 1,
    medianValue: 2,
    p90Value: 3,
  });
});

Deno.test("graph metrics handle empty input and reject invalid reference time", () => {
  assertEquals(calculateGraphMetrics([], "2026-08-11T12:00:00+00:00"), []);
  assertThrows(
    () => calculateGraphMetrics([], "not-a-timestamp"),
    TypeError,
    "invalid calculated timestamp",
  );
});

Deno.test("peer anomalies preserve confidence sample and z-score thresholds", () => {
  const values = [1, 1, 1, 3, 3, 10].map((value, index) => ({
    userId: index + 1,
    value,
    confidence: 0.9,
  }));
  const anomaly = calculatePeerAnomaly(values, 6);
  assertEquals(anomaly?.direction, "above");
  assertEquals(anomaly?.baselineMean, 1.8);
  assertAlmostEquals(anomaly?.zScore ?? 0, 8.369, 0.001);
  assertEquals(
    calculatePeerAnomaly(
      values.map((item) => ({
        ...item,
        confidence: item.userId === 6 ? 0.69 : 0.9,
      })),
      6,
    ),
    null,
  );
});
