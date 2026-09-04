export interface TenantSloSample {
  metricName: string;
  value: number;
  targetValue: number;
  status: "no_data" | "met" | "breached";
  evidenceCount: number;
}

export function calculateLatencySample(
  metricName: string,
  targetValue: number,
  values: readonly number[],
): TenantSloSample {
  const orderedValues = [...values].sort((left, right) => left - right);
  if (orderedValues.length === 0) {
    return sample(metricName, 0, targetValue, "no_data", 0);
  }
  const index = Math.min(
    orderedValues.length - 1,
    Math.max(0, Math.ceil(orderedValues.length * 0.95) - 1),
  );
  const value = orderedValues[index];
  return sample(
    metricName,
    value,
    targetValue,
    value <= targetValue ? "met" : "breached",
    orderedValues.length,
  );
}

export function calculatePercentageSample(
  metricName: string,
  targetValue: number,
  values: readonly number[],
): TenantSloSample {
  if (values.length === 0) {
    return sample(metricName, 0, targetValue, "no_data", 0);
  }
  const value = values.reduce((total, item) => total + Math.trunc(item), 0) *
    100 / values.length;
  return sample(
    metricName,
    value,
    targetValue,
    value >= targetValue ? "met" : "breached",
    values.length,
  );
}

export function calculateCountSample(
  metricName: string,
  targetValue: number,
  value: number,
): TenantSloSample {
  return sample(
    metricName,
    value,
    targetValue,
    value <= targetValue ? "met" : "breached",
    Math.trunc(value),
  );
}

export function calculateFreshnessSample(
  value: number | null,
): TenantSloSample {
  if (value === null) {
    return sample("backup_freshness_seconds", 0, 86_400, "no_data", 0);
  }
  return sample(
    "backup_freshness_seconds",
    value,
    86_400,
    value <= 86_400 ? "met" : "breached",
    1,
  );
}

function sample(
  metricName: string,
  value: number,
  targetValue: number,
  status: TenantSloSample["status"],
  evidenceCount: number,
): TenantSloSample {
  return { metricName, value, targetValue, status, evidenceCount };
}
