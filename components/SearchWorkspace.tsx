import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState } from "./ui.tsx";

const shortTime = (value: unknown): string => {
  const text = String(value ?? "").trim();
  if (!text) return "—";
  // Timestamps arrive pre-formatted in the community timezone
  // (YYYY-MM-DD HH24:MI:SS); render as-is rather than re-localizing.
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/u.test(text)) return text;
  const date = new Date(text);
  return Number.isNaN(date.valueOf()) ? text : date.toLocaleString();
};

const threatTone = (value: unknown): string =>
  ["high", "critical"].includes(String(value)) ? "danger" : "";

const userCell = (item: DashboardItem) => {
  const actorId = item.actor_user_id;
  const actor = String(item.actor_name ?? "").trim();
  const target = String(item.target_name ?? "").trim();
  const actorNode = actor
    ? (actorId != null
      ? <a href={`/users/${String(actorId)}`} class="text-link">{actor}</a>
      : actor)
    : "—";
  return (
    <span>
      {actorNode}
      {target ? <span class="search-target">→ {target}</span> : null}
    </span>
  );
};

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
            <EmptyState
              title={query
                ? `No results for “${query}”`
                : "Search community activity"}
              hint={query
                ? "Try a broader term, or clear filters. Results span messages and events across every linked platform."
                : "Search messages and events across every linked platform. Results show the time, user, content, and any sentiment or threat classification."}
              columns={[
                "Time",
                "User",
                "Platform",
                "Event",
                "Content",
                "Sentiment",
                "Threat",
              ]}
            />
          )
          : (
            <div class="table-wrap">
              <table class="command-table">
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">User</th>
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
                      <td>{userCell(item)}</td>
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
