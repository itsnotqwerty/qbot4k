import { collectCutoverPreflight } from "./src/ops/cutover_preflight.ts";

export async function main(args = Deno.args): Promise<number> {
  const databaseUrl = Deno.env.get("QBOT_DATABASE_URL");
  if (!databaseUrl) throw new TypeError("QBOT_DATABASE_URL is required");
  const windowMinutes = args.length ? Number(args[0]) : 15;
  if (args.length > 1 || !Number.isSafeInteger(windowMinutes)) {
    throw new TypeError("usage: cutover-preflight [window_minutes]");
  }
  const readOnly = (Deno.env.get("QBOT_WEB_READ_ONLY") ?? "false")
    .toLocaleLowerCase();
  if (!new Set(["true", "false"]).has(readOnly)) {
    throw new TypeError("QBOT_WEB_READ_ONLY must be true or false");
  }
  const report = await collectCutoverPreflight(
    databaseUrl,
    readOnly === "true",
    windowMinutes,
  );
  console.log(JSON.stringify(report));
  return report.status === "pass" ? 0 : 1;
}

if (import.meta.main) Deno.exit(await main());
