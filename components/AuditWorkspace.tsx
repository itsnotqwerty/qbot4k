import type { DatabaseRow } from "@/src/data/database.ts";

export function AuditWorkspace(
  { items, query }: {
    readonly items: readonly DatabaseRow[];
    readonly query: URLSearchParams;
  },
) {
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/audit" aria-current="page">Audit</a>
        </nav>
      </header>
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
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Entity</th>
                <th>ID</th>
                <th>Payload</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={String(item.id)}>
                  <td>{String(item.created_at)}</td>
                  <td>
                    {String(item.actor_type)}:{String(
                      item.actor_id ?? "system",
                    )}
                  </td>
                  <td>{String(item.action_type)}</td>
                  <td>{String(item.entity_type)}</td>
                  <td>{String(item.entity_id ?? "-")}</td>
                  <td>
                    <code>{String(item.payload_json)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length ? <p class="empty-state">No audit events.</p> : null}
      </main>
    </div>
  );
}
