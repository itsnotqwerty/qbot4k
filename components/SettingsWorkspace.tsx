import type { SettingsSnapshot } from "@/src/web/web_settings.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

export function SettingsWorkspace({
  community,
  policy,
  installations,
  destinations,
  operators,
  invitations,
  canManageOperators,
  canManageIntegrations,
  status,
}: SettingsSnapshot & {
  readonly canManageOperators: boolean;
  readonly canManageIntegrations: boolean;
  readonly status: string;
}) {
  const text = (row: Record<string, unknown>, key: string) =>
    String(row[key] ?? "");
  const checked = (row: Record<string, unknown>, key: string) =>
    Boolean(row[key]);
  return (
    <div class="app-shell">
      <DashboardHeader active="/settings" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community administration</p>
            <h1>{text(community, "name")}</h1>
            <p class="lede">
              Profile, policy, integrations, notifications, and access for{" "}
              {text(community, "slug")}.
            </p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}

        <form method="post" action="/settings">
          <section class="command-panel">
            <div class="panel-heading">
              <div>
                <p class="section-label">Community</p>
                <h2>Profile and locale</h2>
              </div>
              <p>Identity, language, timezone, and member-facing text.</p>
            </div>
            <div class="settings-form">
              <label>
                Community name
                <input
                  name="name"
                  required
                  maxLength={120}
                  value={text(community, "name")}
                />
              </label>
              <label>
                Locale
                <input
                  name="locale"
                  required
                  maxLength={35}
                  value={text(community, "locale")}
                />
              </label>
              <label>
                Timezone
                <input
                  name="timezone"
                  required
                  value={text(community, "timezone")}
                />
              </label>
              <label>
                Description
                <textarea name="description" maxLength={1000}>
                  {text(community, "description")}
                </textarea>
              </label>
              <label>
                Guidelines
                <textarea name="guidelines" maxLength={10000}>
                  {text(community, "guidelines")}
                </textarea>
              </label>
              <label class="toggle">
                <input
                  type="checkbox"
                  name="notifications_enabled"
                  value="1"
                  checked={checked(community, "notifications_enabled")}
                />
                <span>Enable notifications</span>
              </label>
            </div>
          </section>

          <section class="command-panel">
            <div class="panel-heading">
              <div>
                <p class="section-label">Safety</p>
                <h2>Retention and anti-abuse policy</h2>
              </div>
              <p>Data retention windows and automated abuse controls.</p>
            </div>
            <div class="settings-form">
              <label>
                Message retention (days)
                <input
                  type="number"
                  name="message_retention_days"
                  min="1"
                  max="3650"
                  value={text(policy, "message_retention_days")}
                  required
                />
              </label>
              <label>
                Analytics retention (days)
                <input
                  type="number"
                  name="analytics_retention_days"
                  min="1"
                  max="3650"
                  value={text(policy, "analytics_retention_days")}
                  required
                />
              </label>
              <label>
                Enforcement mode
                <select name="anti_abuse_enforcement_mode">
                  <option
                    value="shadow"
                    selected={policy.anti_abuse_enforcement_mode === "shadow"}
                  >
                    Shadow
                  </option>
                  <option
                    value="enforce"
                    selected={policy.anti_abuse_enforcement_mode === "enforce"}
                  >
                    Enforce
                  </option>
                </select>
              </label>
              <label>
                Message burst limit
                <input
                  type="number"
                  name="message_burst_limit"
                  min="2"
                  max="100"
                  value={text(policy, "message_burst_limit")}
                  required
                />
              </label>
              <label>
                Burst window (seconds)
                <input
                  type="number"
                  name="message_burst_window_seconds"
                  min="1"
                  max="300"
                  value={text(policy, "message_burst_window_seconds")}
                  required
                />
              </label>
              <label>
                Mention limit
                <input
                  type="number"
                  name="mention_limit"
                  min="1"
                  max="100"
                  value={text(policy, "mention_limit")}
                  required
                />
              </label>
              <label>
                Join raid limit
                <input
                  type="number"
                  name="join_raid_limit"
                  min="2"
                  max="1000"
                  value={text(policy, "join_raid_limit")}
                  required
                />
              </label>
              <label>
                Raid window (seconds)
                <input
                  type="number"
                  name="join_raid_window_seconds"
                  min="1"
                  max="3600"
                  value={text(policy, "join_raid_window_seconds")}
                  required
                />
              </label>
              <label class="toggle">
                <input
                  type="checkbox"
                  name="anti_abuse_enabled"
                  value="1"
                  checked={checked(policy, "anti_abuse_enabled")}
                />
                <span>Enable anti-abuse controls</span>
              </label>
            </div>
            <div class="settings-actions">
              <button type="submit">Save all settings</button>
            </div>
          </section>
        </form>

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Providers</p>
              <h2>Integrations</h2>
            </div>
            <p>Connected Discord servers and Twitch channels.</p>
          </div>
          {installations.length === 0
            ? <p class="empty-state">No integrations connected.</p>
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Platform</th>
                      <th scope="col">Name</th>
                      <th scope="col">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {installations.map((item) => (
                      <tr>
                        <td>{text(item, "platform")}</td>
                        <td class="command-name">
                          {text(item, "display_name")}
                        </td>
                        <td>{text(item, "health_status")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          {canManageIntegrations
            ? (
              <p>
                <a href="/integrations" class="text-link">
                  Manage integrations
                </a>
              </p>
            )
            : null}
        </section>

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Alerts</p>
              <h2>Notification destinations</h2>
            </div>
            <p>Where high-severity alerts are delivered.</p>
          </div>
          {destinations.length === 0
            ? (
              <p class="empty-state">
                No notification destinations configured.
              </p>
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Name</th>
                      <th scope="col">Type</th>
                      <th scope="col">Minimum severity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {destinations.map((item) => (
                      <tr>
                        <td>{text(item, "name")}</td>
                        <td>{text(item, "destination_type")}</td>
                        <td>{text(item, "minimum_severity")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>

        {canManageOperators
          ? (
            <section class="command-panel">
              <div class="panel-heading">
                <div>
                  <p class="section-label">Access</p>
                  <h2>Operators</h2>
                </div>
                <p>People with dashboard access to this community.</p>
              </div>
              {operators.length === 0
                ? <p class="empty-state">No operators.</p>
                : (
                  <div class="table-wrap">
                    <table class="command-table">
                      <thead>
                        <tr>
                          <th scope="col">Operator</th>
                          <th scope="col">Role</th>
                        </tr>
                      </thead>
                      <tbody>
                        {operators.map((item) => (
                          <tr>
                            <td>{text(item, "discord_username")}</td>
                            <td>{text(item, "role")}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              {invitations.length > 0 && (
                <>
                  <h3>Pending invitations</h3>
                  {invitations.map((item) => (
                    <p>
                      {text(item, "target_discord_user_id")}:{" "}
                      {text(item, "invited_role")}
                    </p>
                  ))}
                </>
              )}
              <form
                method="post"
                action="/settings/operators/invite"
                class="command-new"
              >
                <input
                  name="discord_user_id"
                  required
                  placeholder="Discord user ID"
                  aria-label="Discord user ID"
                />
                <select name="role" aria-label="Role">
                  <option value="viewer">Viewer</option>
                  <option value="analyst">Analyst</option>
                  <option value="moderator">Moderator</option>
                  <option value="admin">Admin</option>
                </select>
                <input
                  type="number"
                  name="expires_hours"
                  min="1"
                  max="720"
                  value="72"
                  aria-label="Expires in hours"
                />
                <button type="submit">Invite operator</button>
              </form>
            </section>
          )
          : null}
      </main>
    </div>
  );
}
