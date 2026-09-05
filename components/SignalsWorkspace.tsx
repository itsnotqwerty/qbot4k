import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState, Meter, WindowLabel } from "./ui.tsx";

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

const cell = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  const ts = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?/u);
  return ts ? `${ts[1]} ${ts[2]}` : text;
};

const signalLabel = (key: unknown): string =>
  String(key ?? "").replaceAll("_", " ").replaceAll(".", " / ") || "—";

export function SignalsWorkspace(
  { items, query }: {
    readonly items: readonly DashboardItem[];
    readonly query: string;
  },
) {
  // Group rows by signal so an analyst scans per-signal cohorts.
  const bySignal = new Map<string, DashboardItem[]>();
  for (const item of items) {
    const key = String(item.signal_key ?? "unknown");
    const list = bySignal.get(key) ?? [];
    list.push(item);
    bySignal.set(key, list);
  }
  // Normalization ceiling per signal for meter scaling.
  const maxima = new Map<string, number>();
  for (const [key, rows] of bySignal) {
    maxima.set(
      key,
      Math.max(0.0001, ...rows.map((row) => Math.abs(num(row.value)))),
    );
  }

  return (
    <div class="app-shell">
      <DashboardHeader active="/signals" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community data</p>
            <h1>Signals</h1>
            <p class="lede">
              Derived behavioral signals per member over the active window.
            </p>
          </div>
          <form method="get" action="/signals" class="search-bar">
            <input
              id="q"
              name="q"
              value={query}
              placeholder="Filter signals"
              aria-label="Filter signals"
            />
            <button type="submit">Apply</button>
          </form>
        </section>

        {bySignal.size === 0
          ? (
            <EmptyState
              title="No signals computed yet"
              hint="Derived behavioral signals appear after the analysis window accumulates enough message volume. Each signal groups members by their measured value, confidence, and supporting evidence."
              columns={[
                "Member",
                "Value",
                "Confidence",
                "Evidence",
                "Calculated",
              ]}
            />
          )
          : (
            [...bySignal.entries()].map(([signal, rows]) => (
              <section class="panel analytics-section" key={signal}>
                <h2>
                  {signalLabel(signal)} <WindowLabel text="last 24h" />
                </h2>
                <div class="table-wrap">
                  <table class="command-table">
                    <thead>
                      <tr>
                        <th scope="col">Member</th>
                        <th scope="col">Value</th>
                        <th scope="col">Confidence</th>
                        <th scope="col">Evidence</th>
                        <th scope="col">Calculated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, index) => (
                        <tr key={`${signal}-${String(row.user_id ?? index)}`}>
                          <td>
                            {row.user_id != null
                              ? (
                                <a
                                  href={`/users/${String(row.user_id)}`}
                                  class="text-link"
                                >
                                  {cell(row.display_name)}
                                </a>
                              )
                              : cell(row.display_name)}
                          </td>
                          <td>
                            <Meter
                              value={Math.abs(num(row.value))}
                              max={maxima.get(signal) ?? 1}
                              tone="info"
                              format={(v) =>
                                num(row.value).toFixed(2)}
                            />
                          </td>
                          <td>
                            <Meter
                              value={num(row.confidence)}
                              tone={num(row.confidence) >= 0.7
                                ? "ok"
                                : num(row.confidence) >= 0.4
                                ? "warn"
                                : "neutral"}
                              format={(v) =>
                                `${Math.round(v * 100)}%`}
                            />
                          </td>
                          <td class="num">{num(row.evidence_count)}</td>
                          <td class="num">{cell(row.calculated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            ))
          )}
      </main>
    </div>
  );
}
