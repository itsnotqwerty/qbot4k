import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

const shortTime = (value: unknown): string => {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.valueOf())
    ? String(value ?? "—")
    : date.toLocaleString();
};

const threatTone = (value: unknown): string =>
  ["high", "critical"].includes(String(value)) ? "danger" : "";

export function SearchWorkspace(
  { items, query }: {
    readonly items: readonly DashboardItem[];
    readonly query: string;
  },
) {
  return (
    <div class="app-shell">
      <DashboardHeader active="/search" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community data</p>
            <h1>Search</h1>
            <p class="lede">
              Search observations and messages across the active community.
            </p>
          </div>
          <form method="get" action="/search" class="search-bar">
            <input
              id="q"
              name="q"
              value={query}
              placeholder="Search messages, users, content"
              aria-label="Search"
            />
            <button type="submit">Search</button>
          </form>
        </section>

        {items.length === 0
          ? (
            <p class="empty-state">
              {query
                ? `No results for “${query}”.`
                : "Enter a query to search community activity."}
            </p>
          )
          : (
            <div class="table-wrap">
              <table class="command-table">
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Platform</th>
                    <th scope="col">Event</th>
                    <th scope="col">Content</th>
                    <th scope="col">Sentiment</th>
                    <th scope="col">Threat</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={String(item.id)}>
                      <td class="command-name">
                        {shortTime(item.occurred_at)}
                      </td>
                      <td>{String(item.platform ?? "—")}</td>
                      <td>{String(item.event_type ?? "—")}</td>
                      <td class="search-content">
                        {String(item.text_raw ?? "") || "—"}
                      </td>
                      <td>{String(item.sentiment_label ?? "—")}</td>
                      <td class={threatTone(item.threat_level)}>
                        {String(item.threat_level ?? "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </main>
    </div>
  );
}
