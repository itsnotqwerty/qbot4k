import type { DatabaseRow } from "@/src/data/database.ts";

export function AnnouncementsWorkspace({
  community,
  installations,
  items,
  canManage,
  status,
}: {
  readonly community: DatabaseRow;
  readonly installations: readonly DatabaseRow[];
  readonly items: readonly DatabaseRow[];
  readonly canManage: boolean;
  readonly status: string;
}) {
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/announcements" aria-current="page">Announcements</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community management</p>
            <h1>Announcements</h1>
            <p class="lede">
              Draft and schedule messages for{" "}
              {String(community.name)}. Times use {String(community.timezone)}.
            </p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}
        {canManage
          ? (
            <form method="post" action="/announcements">
              <input type="hidden" name="platform" value="discord" />
              <select name="target_installation_id">
                <option value="">Auto-select sole installation</option>
                {installations.map((item) => (
                  <option value={String(item.id)}>
                    {String(item.display_name)}
                  </option>
                ))}
              </select>
              <input
                name="target_external_id"
                required
                placeholder="Channel or target ID"
              />
              <textarea
                name="body"
                required
                maxLength={2000}
                placeholder="Message"
              />
              <button type="submit">Save draft</button>
            </form>
          )
          : null}
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Target</th>
                <th>Message</th>
                <th>Status</th>
                <th>Scheduled</th>
                <th>Attempts</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={String(item.id)}>
                  <td>{String(item.id)}</td>
                  <td>{String(item.target_external_id)}</td>
                  <td>{String(item.body)}</td>
                  <td>{String(item.status)}</td>
                  <td>{String(item.scheduled_at ?? "")}</td>
                  <td>{String(item.attempt_count)}</td>
                  <td>
                    {canManage && item.status === "draft"
                      ? (
                        <form
                          method="post"
                          action={`/announcements/${item.id}/approve`}
                        >
                          <input
                            type="datetime-local"
                            name="scheduled_at"
                            required
                          />
                          <button type="submit">Approve</button>
                        </form>
                      )
                      : null}
                    {canManage && item.status === "failed"
                      ? (
                        <form
                          method="post"
                          action={`/announcements/${item.id}/retry`}
                        >
                          <button type="submit">Retry</button>
                        </form>
                      )
                      : null}
                    {canManage &&
                        ["draft", "scheduled", "failed"].includes(
                          String(item.status),
                        )
                      ? (
                        <form
                          method="post"
                          action={`/announcements/${item.id}/cancel`}
                        >
                          <button type="submit">Cancel</button>
                        </form>
                      )
                      : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
