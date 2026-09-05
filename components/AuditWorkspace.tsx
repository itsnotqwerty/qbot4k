import type { DatabaseRow } from "@/src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

export function AuditWorkspace(
  { items, query }: {
    readonly items: readonly DatabaseRow[];
    readonly query: URLSearchParams;
  },
) {
  return (
    <div class="app-shell">
      <DashboardHeader active="/audit" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Governance</p>
            <h1>Audit trail</h1>
            <p class="lede">
              Review authentication, analyst decisions, policy changes, exports,
              and administrative actions.
            </p>
          </div>
        </section>
        <form method="get" action="/audit">
          <input
            name="action_type"
            value={query.get("action_type") ?? ""}
            placeholder="Action type"
          />
          <input
            name="actor_id"
            value={query.get("actor_id") ?? ""}
            placeholder="Operator ID"
          />
          <input
            name="entity_type"
            value={query.get("entity_type") ?? ""}
            placeholder="Entity type"
          />
          <input
            name="start_at"
            value={query.get("start_at") ?? ""}
            placeholder="Start ISO time"
          />
          <button type="submit">Filter</button>
          <a href="/audit">Clear</a>
        </form>
        <div class="table-wrap">
          <table class="command-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Subject</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const actorName = item.actor_id == null
                  ? String(item.actor_type)
                  : `${String(item.actor_type)} #${String(item.actor_id)}`;
                const subject = [
                  String(item.entity_type ?? ""),
                  item.entity_id == null ? "" : `#${String(item.entity_id)}`,
                ].filter(Boolean).join(" ");
                return (
                  <tr key={String(item.id)}>
                    <td class="command-name">
                      {new Date(String(item.created_at)).toLocaleString()}
                    </td>
                    <td>{actorName}</td>
                    <td>
                      {String(item.action_type).replaceAll(".", " / ")
                        .replaceAll("_", " ")}
                    </td>
                    <td>{subject || "—"}</td>
                    <td>
                      <details class="audit-payload">
                        <summary>View</summary>
                        <code>{String(item.payload_json)}</code>
                      </details>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!items.length ? <p class="empty-state">No audit events.</p> : null}
      </main>
    </div>
  );
}
