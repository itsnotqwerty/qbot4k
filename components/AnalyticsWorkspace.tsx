import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState, Meter } from "./ui.tsx";

const SECTION_LABELS: Readonly<Record<string, string>> = {
  growth: "Member growth",
  repeat_offenses: "Repeat offenses",
  report_outcomes: "Report outcomes",
  appeal_outcomes: "Appeal outcomes",
  rule_precision: "Moderation rules",
  topics: "Emerging topics",
  graph: "Influence graph",
  identity_suggestions: "Identity link suggestions",
  cohort_anomalies: "Cohort anomalies",
  evaluation: "Evaluation",
};

const SECTION_ORDER: readonly string[] = [
  "growth",
  "topics",
  "repeat_offenses",
  "report_outcomes",
  "appeal_outcomes",
  "graph",
  "identity_suggestions",
  "cohort_anomalies",
  "rule_precision",
];

// Expected columns + guidance shown when a section has no rows, so the
// structure reads as intentional even before data accumulates.
const SECTION_EMPTY: Readonly<
  Record<string, { readonly hint: string; readonly columns: readonly string[] }>
> = {
  growth: {
    hint:
      "Daily member joins chart here once the community records membership events.",
    columns: ["Date", "Joins"],
  },
  topics: {
    hint:
      "Emerging topics surface after the 24h analysis window accumulates message volume.",
    columns: ["Topic", "Velocity", "Unusualness", "Last seen"],
  },
  repeat_offenses: {
    hint: "Members with more than one moderation action appear here.",
    columns: ["Member", "Actions"],
  },
  report_outcomes: {
    hint:
      "A proportion bar of report resolutions appears once members file reports.",
    columns: ["Outcome", "Reports"],
  },
  appeal_outcomes: {
    hint:
      "A proportion bar of appeal dispositions appears once appeals are filed.",
    columns: ["Outcome", "Appeals"],
  },
  graph: {
    hint: "Influence scores compute as members interact across channels.",
    columns: ["Member", "Influence", "Pagerank"],
  },
  identity_suggestions: {
    hint:
      "Suggested cross-platform identity links appear when accounts show matching behavior.",
    columns: ["Confidence", "Status"],
  },
  cohort_anomalies: {
    hint:
      "Outlier members surface when a cohort's signal deviates from baseline.",
    columns: ["Member", "Signal", "Z-score", "Direction"],
  },
  rule_precision: {
    hint: "Moderation rules configured for this community are listed here.",
    columns: ["Rule", "Name"],
  },
};

// Columns that are noise in a table (raw JSON payloads, redundant IDs, text
// timestamps already covered by friendlier fields).
const HIDDEN_COLUMNS = new Set([
  "details_json",
  "evidence_json",
  "payload_json",
  "source_json",
  "community_id",
  "model_version",
  "reviewed_by_operator_id",
]);

const cellText = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  if (typeof value === "object") return JSON.stringify(value);
  const text = String(value);
  // ISO-ish timestamps -> drop the sub-second/offset noise for readability.
  const match = text.match(
    /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?/u,
  );
  return match ? `${match[1]} ${match[2]}` : text;
};

const columnLabel = (column: string): string =>
  column.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());

// ---- Lightweight inline visualizations (no chart library) ----

// Trend sparkline for time series like member growth.
function Sparkline(
  { points }: { readonly points: readonly number[] },
) {
  if (!points.length) {
    return (
      <svg class="spark" viewBox="0 0 200 56" role="img" aria-label="No data">
        <text class="spark-empty" x="8" y="30">No trend data</text>
      </svg>
    );
  }
  const width = 200;
  const height = 56;
  const pad = 2;
  const max = Math.max(1, ...points);
  const step = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
  const coords = points.map((value, index) => {
    const x = pad + index * step;
    const y = height - pad - (value / max) * (height - pad * 2);
    return [x, y] as const;
  });
  const line = coords.map(([x, y], i) =>
    `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`
  )
    .join(" ");
  const area = `${line} L${(pad + (points.length - 1) * step).toFixed(1)},${
    (height - pad).toFixed(1)
  } L${pad},${(height - pad).toFixed(1)} Z`;
  return (
    <svg
      class="spark"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Trend"
    >
      <path class="area" d={area} />
      <path class="line" d={line} />
    </svg>
  );
}

// Proportion bar for outcome breakdowns (reports / appeals).
const OUTCOME_TONES: readonly string[] = [
  "p-ok",
  "p-info",
  "p-warn",
  "p-danger",
  "p-neutral",
];

function ProportionBar(
  { rows, labelKey, countKey }: {
    readonly rows: readonly DashboardItem[];
    readonly labelKey: string;
    readonly countKey: string;
  },
) {
  const total = rows.reduce((sum, row) => sum + Number(row[countKey] ?? 0), 0);
  if (!total) return <p class="empty-state">No data yet.</p>;
  return (
    <div>
      <div class="prop" role="img" aria-label="Outcome proportions">
        {rows.map((row, index) => {
          const count = Number(row[countKey] ?? 0);
          const pct = (count / total) * 100;
          return (
            <span
              key={String(row[labelKey])}
              class={OUTCOME_TONES[index % OUTCOME_TONES.length]}
              style={{ width: `${pct.toFixed(1)}%` }}
              title={`${String(row[labelKey])}: ${count}`}
            />
          );
        })}
      </div>
      <div class="prop-legend">
        {rows.map((row, index) => (
          <span key={String(row[labelKey])}>
            <b>{String(row[labelKey])}</b> {Number(row[countKey] ?? 0)}{" "}
            ({((Number(row[countKey] ?? 0) / total) * 100).toFixed(0)}%)
          </span>
        ))}
      </div>
    </div>
  );
}

function SectionTable(
  { name, rows }: {
    readonly name: string;
    readonly rows: readonly DashboardItem[];
  },
) {
  if (!rows.length) {
    const empty = SECTION_EMPTY[name];
    return (
      <section class="panel analytics-section">
        <h2>{SECTION_LABELS[name] ?? columnLabel(name)}</h2>
        <EmptyState
          title={`No ${
            (SECTION_LABELS[name] ?? columnLabel(name)).toLocaleLowerCase()
          } yet`}
          hint={empty?.hint ??
            "This section populates as community data accumulates."}
          columns={empty?.columns ?? []}
        />
      </section>
    );
  }
  const columns = Object.keys(rows[0]).filter((column) =>
    !HIDDEN_COLUMNS.has(column)
  );
  // Section-specific visual summaries rendered above the table.
  let visual = null;
  if (name === "growth") {
    // Rows arrive newest-first; reverse so the trend reads left-to-right.
    const points = [...rows]
      .map((row) => Number(row.joins ?? 0))
      .reverse();
    visual = <Sparkline points={points} />;
  } else if (name === "report_outcomes") {
    visual = (
      <ProportionBar rows={rows} labelKey="outcome" countKey="report_count" />
    );
  } else if (name === "appeal_outcomes") {
    visual = (
      <ProportionBar rows={rows} labelKey="outcome" countKey="appeal_count" />
    );
  } else if (name === "topics") {
    // Ranked velocity bars for emerging topics.
    const max = Math.max(1, ...rows.map((row) => Number(row.velocity ?? 0)));
    visual = (
      <div class="prop-legend" style={{ flexDirection: "column", gap: "6px" }}>
        {rows.slice(0, 6).map((row) => (
          <span key={String(row.id)} style={{ width: "100%" }}>
            <Meter
              value={Number(row.velocity ?? 0)}
              max={max}
              tone="info"
              format={() =>
                String(row.label ?? row.topic_key ?? "")}
            />
          </span>
        ))}
      </div>
    );
  }
  return (
    <section class="panel analytics-section">
      <h2>{SECTION_LABELS[name] ?? columnLabel(name)}</h2>
      {visual}
      <div class="table-wrap">
        <table class="command-table">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column} scope="col">{columnLabel(column)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={String(row.id ?? row.user_id ?? index)}>
                {columns.map((column) => (
                  <td key={column}>{cellText(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function AnalyticsWorkspace(
  { metrics }: { readonly metrics: DashboardItem },
) {
  const ordered = [
    ...SECTION_ORDER,
    ...Object.keys(metrics).filter((key) => !SECTION_ORDER.includes(key)),
  ];
  return (
    <div class="app-shell">
      <DashboardHeader active="/analytics" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community data</p>
            <h1>Analytics</h1>
            <p class="lede">
              Community trends, moderation outcomes, and emerging signals.
            </p>
          </div>
        </section>
        <div class="analytics-grid">
          {ordered.map((name) => {
            const value = metrics[name];
            const rows = Array.isArray(value)
              ? (value as readonly DashboardItem[])
              : [];
            return <SectionTable key={name} name={name} rows={rows} />;
          })}
        </div>
      </main>
    </div>
  );
}
