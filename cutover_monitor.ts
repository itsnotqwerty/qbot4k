import {
  collectCutoverMonitor,
  type CutoverMonitorReport,
} from "./src/ops/cutover_monitor.ts";

export interface CutoverMonitorWindowOptions {
  readonly sampleCount: number;
  readonly intervalMilliseconds: number;
  readonly collect: () => Promise<CutoverMonitorReport>;
  readonly emit?: (report: CutoverMonitorReport) => void;
  readonly wait?: (milliseconds: number) => Promise<void>;
}

export async function monitorCutoverWindow(
  options: CutoverMonitorWindowOptions,
): Promise<number> {
  const emit = options.emit ??
    ((report) => console.log(JSON.stringify(report)));
  const wait = options.wait ??
    ((milliseconds) =>
      new Promise((resolve) => setTimeout(resolve, milliseconds)));
  for (let sample = 0; sample < options.sampleCount; sample += 1) {
    const report = await options.collect();
    emit(report);
    if (report.status === "fail") return 1;
    if (sample + 1 < options.sampleCount) {
      await wait(options.intervalMilliseconds);
    }
  }
  return 0;
}

export async function main(args = Deno.args): Promise<number> {
  const databaseUrl = Deno.env.get("QBOT_DATABASE_URL");
  if (!databaseUrl) throw new TypeError("QBOT_DATABASE_URL is required");
  const windowMinutes = args.length ? Number(args[0]) : 15;
  const sampleCount = args.length > 1 ? Number(args[1]) : 1;
  const intervalSeconds = args.length > 2 ? Number(args[2]) : 900;
  if (
    args.length > 3 || !Number.isSafeInteger(windowMinutes) ||
    !Number.isSafeInteger(sampleCount) || sampleCount < 1 ||
    !Number.isSafeInteger(intervalSeconds) || intervalSeconds < 1
  ) {
    throw new TypeError(
      "usage: cutover-monitor [window_minutes] [sample_count] [interval_seconds]",
    );
  }
  return await monitorCutoverWindow({
    sampleCount,
    intervalMilliseconds: intervalSeconds * 1000,
    collect: () => collectCutoverMonitor(databaseUrl, windowMinutes),
  });
}

if (import.meta.main) Deno.exit(await main());
