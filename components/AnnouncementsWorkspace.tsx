import type { DatabaseRow } from "@/src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { Chip, EmptyState, severityTone } from "./ui.tsx";

const time = (value: unknown): string => {
  const text = String(value ?? "").trim();
  if (!text) return "—";
  const ts = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/u);
  return ts ? `${ts[1]} ${ts[2]}` : text;
};

export function AnnouncementsWorkspace({
  community,
  installations,
  items,
  channels = [],
  canManage,
  status,
}: {
  readonly community: DatabaseRow;
  readonly installations: readonly DatabaseRow[];
  readonly items: readonly DatabaseRow[];
  readonly channels?: readonly DatabaseRow[];
  readonly canManage: boolean;
  readonly status: string;
}) {
  return (
    <div class="app-shell">
      <DashboardHeader active="/announcements" />
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
              <select name="target_external_id" required>
                <option value="">Select a channel</option>
                {channels.map((channel) => (
                  <option
                    value={String(channel.channel_id)}
                    key={String(channel.channel_id)}
                  >
                    #{String(channel.channel_name)}
                  </option>
                ))}
              </select>
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
        {items.length === 0
          ? (
            <EmptyState
              title="No announcements yet"
              hint="Draft a message above, then approve and schedule it. Delivery status, attempts, and any errors are tracked per announcement."
              columns={[
                "ID",
                "Target",
                "Message",
                "Status",
                "Scheduled",
                "Attempts",
                "Actions",
              ]}
            />
          )
          : (
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
                      <td class="num">{String(item.id)}</td>
                      <td>
                        {item.channel_name
                          ? `#${String(item.channel_name)}`
                          : String(item.target_external_id)}
                      </td>
                      <td class="search-content">{String(item.body)}</td>
                      <td>
                        <Chip
                          tone={severityTone(item.status)}
                          label={String(item.status ?? "—")}
                        />
                        {item.status === "failed" && item.last_error
                          ? (
                            <div class="delivery-error">
                              {String(item.last_error)}
                            </div>
                          )
                          : null}
                      </td>
                      <td class="num">{time(item.scheduled_at)}</td>
                      <td class="num">{String(item.attempt_count)}</td>
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
          )}
      </main>
    </div>
  );
}
