import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState } from "./ui.tsx";

const fieldLabel = (key: string): string =>
  ({
    user_id: "User ID",
    primary_display_name: "Display name",
    current_reputation_score: "Social score",
    candidate_flag: "Power user",
    score_confidence: "Score confidence",
    score_model_version: "Score model",
  })[key] ?? key.replaceAll("_", " ");

const formatValue = (key: string, value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (key === "candidate_flag") return value ? "Yes" : "No";
  if (key === "score_confidence") return `${Math.round(Number(value) * 100)}%`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

export function UserProfileWorkspace(
  { user, linkedAccounts, notes, status, accountStatus, canManage }: {
    readonly user: DashboardItem;
    readonly linkedAccounts: readonly DashboardItem[];
    readonly notes: readonly DashboardItem[];
    readonly status: string;
    readonly accountStatus: string;
    readonly canManage: boolean;
  },
) {
  const userId = Number(user.user_id);
  return (
    <div class="app-shell">
      <DashboardHeader active="/users" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Canonical profile</p>
            <h1>{String(user.primary_display_name ?? `User ${userId}`)}</h1>
            <p class="lede">
              Linked accounts, social score, and moderation lifecycle.
            </p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}
        {accountStatus ? <p class="status-banner">{accountStatus}</p> : null}

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Identity</p>
              <h2>Profile</h2>
            </div>
          </div>
          <dl class="metric-grid">
            {Object.entries(user).map(([key, value]) => (
              <div key={key}>
                <dt>{fieldLabel(key)}</dt>
                <dd>{formatValue(key, value)}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Accounts</p>
              <h2>Linked platform accounts</h2>
            </div>
            <p>
              Accounts from Discord and Twitch that resolve to this person.
              Unlinking detaches an account from this profile.
            </p>
          </div>
          {linkedAccounts.length === 0
            ? (
              <EmptyState
                title="No linked platform accounts"
                hint="Accounts from Discord and Twitch that resolve to this person appear here. Link an account below to merge activity into this profile."
                columns={["Platform", "Username", "Context", "Actions"]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Platform</th>
                      <th scope="col">Username</th>
                      <th scope="col">Account ID</th>
                      <th scope="col">Context</th>
                      {canManage ? <th scope="col"></th> : null}
                    </tr>
                  </thead>
                  <tbody>
                    {linkedAccounts.map((account) => (
                      <tr key={String(account.id)}>
                        <td>{String(account.platform)}</td>
                        <td>{String(account.username)}</td>
                        <td class="command-name">
                          {String(account.platform_user_id)}
                        </td>
                        <td>
                          {String(account.guild_or_channel_context ?? "—")}
                        </td>
                        {canManage
                          ? (
                            <td>
                              <form
                                method="post"
                                action="/users/unlink"
                                class="danger-action"
                              >
                                <input
                                  type="hidden"
                                  name="user_id"
                                  value={String(userId)}
                                />
                                <input
                                  type="hidden"
                                  name="platform_account_id"
                                  value={String(account.id)}
                                />
                                <input
                                  type="hidden"
                                  name="confirmation"
                                  value="UNLINK"
                                />
                                <button type="submit">Unlink</button>
                              </form>
                            </td>
                          )
                          : null}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          {canManage
            ? (
              <form method="post" action="/users/link" class="command-new">
                <input
                  type="hidden"
                  name="selected_user_id"
                  value={String(userId)}
                />
                <select name="platform" aria-label="Platform">
                  <option value="any">Any platform</option>
                  <option value="discord">Discord</option>
                  <option value="twitch">Twitch</option>
                </select>
                <input
                  name="usernames"
                  placeholder="Usernames to link, comma-separated"
                  aria-label="Usernames to link"
                />
                <button type="submit">Link accounts</button>
              </form>
            )
            : null}
        </section>

        {notes.length > 0 && (
          <section class="command-panel">
            <div class="panel-heading">
              <div>
                <p class="section-label">History</p>
                <h2>Operator notes</h2>
              </div>
            </div>
            <div class="table-wrap">
              <table class="command-table">
                <thead>
                  <tr>
                    <th scope="col">Note</th>
                    <th scope="col">Recorded</th>
                  </tr>
                </thead>
                <tbody>
                  {notes.map((note) => (
                    <tr key={String(note.id)}>
                      <td>{String(note.body)}</td>
                      <td>{String(note.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
