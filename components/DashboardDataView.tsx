import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

interface DashboardDataViewProps {
  readonly title: string;
  readonly eyebrow: string;
  readonly description: string;
  readonly items?: readonly DashboardItem[];
  readonly metrics?: DashboardItem;
  readonly query?: string;
  readonly activePath?: string;
}

const display = (value: unknown): string => {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") {
    // Some metric fields arrive as JSON-encoded arrays of rows.
    const trimmed = value.trim();
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
      try {
        return formatRowArray(JSON.parse(trimmed));
      } catch {
        return value;
      }
    }
    return value;
  }
  if (Array.isArray(value)) return formatRowArray(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

function formatRowArray(rows: unknown[]): string {
  if (!rows.length) return "None";
  const parts = rows.map((row) => {
    if (row === null || typeof row !== "object") return String(row);
    const record = row as Record<string, unknown>;
    const label = record.channel ?? record.platform ?? record.name ??
      record.channel_id ?? "";
    const count = record.count;
    return count === undefined
      ? String(label)
      : `${String(label)} (${String(count)})`;
  });
  return parts.join(",  ");
}

const COLUMN_LABELS: Readonly<Record<string, string>> = {
  user_id: "User",
  primary_display_name: "Display name",
  current_reputation_score: "Social score",
  candidate_flag: "Power user",
  account_count: "Accounts",
  message_count: "Messages",
  platform_user_id: "Account ID",
  guild_or_channel_context: "Server / channel",
  signal_key: "Signal",
  value_real: "Value",
  evidence_count: "Evidence",
  calculated_at: "Calculated",
  created_at: "Created",
  occurred_at: "Occurred",
  event_type: "Event",
  health_status: "Health",
  display_name: "Name",
};

const columnLabel = (column: string): string =>
  COLUMN_LABELS[column] ??
    column.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());

export function DashboardDataView(
  {
    title,
    eyebrow,
    description,
    items = [],
    metrics,
    query = "",
    activePath = "/dashboard",
  }: DashboardDataViewProps,
) {
  const rows = metrics ? Object.entries(metrics) : [];
  const columns = items.length ? Object.keys(items[0]) : [];
  return (
    <div class="app-shell">
      <DashboardHeader active={activePath} />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">{eyebrow}</p>
            <h1>{title}</h1>
            <p class="lede">{description}</p>
          </div>
          {query !== undefined && title !== "Overview"
            ? (
              <form method="get" action={`/${title.toLocaleLowerCase()}`}>
                <label for="q">
                  Filter<input id="q" name="q" value={query} />
                </label>
                <button type="submit">Apply</button>
              </form>
            )
            : null}
        </section>
        {metrics
          ? (
            <dl class="metric-grid">
              {rows.map(([name, value]) => (
                <div key={name}>
                  <dt>{columnLabel(name)}</dt>
                  <dd>{display(value)}</dd>
                </div>
              ))}
            </dl>
          )
          : (
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={column} scope="col">
                        {columnLabel(column)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, rowIndex) => {
                    const userId = item.user_id ?? item.id;
                    const isUserRow = activePath === "/users" &&
                      userId !== undefined;
                    return (
                      <tr key={String(item.id ?? item.user_id ?? rowIndex)}>
                        {columns.map((column) => {
                          const value = display(item[column]);
                          if (isUserRow && column === "primary_display_name") {
                            return (
                              <td key={column}>
                                <a
                                  href={`/users/${String(userId)}`}
                                  class="text-link"
                                >
                                  {value}
                                </a>
                              </td>
                            );
                          }
                          return <td key={column}>{value}</td>;
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!items.length ? <p class="empty-state">No results.</p> : null}
            </div>
          )}
      </main>
    </div>
  );
}
