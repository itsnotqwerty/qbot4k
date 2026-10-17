import type { AdminView } from "../src/data/billing_repository.ts";
import type { DatabaseRow } from "../src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

const columns = {
  organizations: [
    ["id", "ID"],
    ["name", "Organization"],
    ["status", "Status"],
    ["communities", "Communities"],
    ["complimentary", "Staff access"],
  ],
  accounts: [["id", "ID"], ["discord_username", "Account"], [
    "status",
    "Status",
  ], ["platform_role", "Platform role"]],
  audit: [
    ["id", "ID"],
    ["organization_id", "Organization"],
    ["operator_id", "Actor"],
    ["action", "Action"],
    ["reason", "Reason"],
    ["created_at", "Time"],
  ],
} as const;

export function AdminWorkspace(
  { view, page, rows }: {
    view: AdminView;
    page: number;
    rows: readonly DatabaseRow[];
  },
) {
  return (
    <div class="app-shell">
      <DashboardHeader active="/admin" platformAdmin />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">QBot4K</p>
            <h1>Platform admin</h1>
          </div>
        </section>
        <nav class="admin-navigation" aria-label="Administration views">
          {([["organizations", "Organizations"], ["accounts", "Accounts"], [
            "audit",
            "Billing audit",
          ]] as const).map(([key, label]) => (
            <a
              href={`/admin?view=${key}`}
              aria-current={view === key ? "page" : undefined}
            >
              {label}
            </a>
          ))}
        </nav>
        <div class="table-wrap">
          <table class="command-table">
            <thead>
              <tr>
                {columns[view].map(([key, label]) => (
                  <th key={key} scope="col">{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 50).map((row) => (
                <tr key={String(row.id)}>
                  {columns[view].map(([key]) => (
                    <td style={{ overflowWrap: "anywhere", maxWidth: "28rem" }}>
                      {key === "complimentary"
                        ? (row[key] === true ? "Complimentary" : "Standard")
                        : String(row[key] ?? "None")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length === 0 && <p>No records.</p>}
        <nav class="admin-navigation" aria-label="Pagination">
          {page > 1 && (
            <a href={`/admin?view=${view}&page=${page - 1}`} rel="prev">
              Previous
            </a>
          )}
          <span>Page {page}</span>
          {rows.length > 50 && (
            <a href={`/admin?view=${view}&page=${page + 1}`} rel="next">Next</a>
          )}
        </nav>
        <nav class="admin-navigation" aria-label="Administration tools">
          <a href="/settings">Settings</a>
          <a href="/commands">Commands</a>
          <a href="/system-health">System health</a>
          <a href="/billing">Billing</a>
          <a href="/admin/rewards">Backer fulfillment</a>
        </nav>
      </main>
    </div>
  );
}
