import type { SettingsSnapshot } from "@/src/web/web_settings.ts";

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
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/settings" aria-current="page">Settings</a>
        </nav>
      </header>
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
          <h2>Profile and locale</h2>
          <input
            name="name"
            required
            maxLength={120}
            value={text(community, "name")}
          />
          <input
            name="locale"
            required
            maxLength={35}
            value={text(community, "locale")}
          />
          <input name="timezone" required value={text(community, "timezone")} />
          <label>
            <input
              type="checkbox"
              name="notifications_enabled"
              value="1"
              checked={checked(community, "notifications_enabled")}
            />{" "}
            Enable notifications
          </label>
          <textarea name="description" maxLength={1000}>
            {text(community, "description")}
          </textarea>
          <textarea name="guidelines" maxLength={10000}>
            {text(community, "guidelines")}
          </textarea>
          <h2>Retention and anti-abuse policy</h2>
          <input
            type="number"
            name="message_retention_days"
            min="1"
            max="3650"
            value={text(policy, "message_retention_days")}
            required
          />
          <input
            type="number"
            name="analytics_retention_days"
            min="1"
            max="3650"
            value={text(policy, "analytics_retention_days")}
            required
          />
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
          <label>
            <input
              type="checkbox"
              name="anti_abuse_enabled"
              value="1"
              checked={checked(policy, "anti_abuse_enabled")}
            />{" "}
            Enable anti-abuse controls
          </label>
          {[
            ["message_burst_limit", 2, 100],
            ["message_burst_window_seconds", 1, 300],
            ["mention_limit", 1, 100],
            ["join_raid_limit", 2, 1000],
            ["join_raid_window_seconds", 1, 3600],
          ].map(([name, min, max]) => (
            <input
              type="number"
              name={String(name)}
              min={String(min)}
              max={String(max)}
              value={text(policy, String(name))}
              required
            />
          ))}
          <button type="submit">Save settings</button>
        </form>
        <section>
          <h2>Integrations</h2>
          {installations.map((item) => (
            <p>
              {text(item, "platform")}: {text(item, "display_name")}{" "}
              ({text(item, "health_status")})
            </p>
          ))}
          {canManageIntegrations
            ? <a href="/integrations">Manage integrations</a>
            : null}
        </section>
        <section>
          <h2>Notification destinations</h2>
          {destinations.map((item) => (
            <p>{text(item, "name")}: {text(item, "minimum_severity")}</p>
          ))}
        </section>
        {canManageOperators
          ? (
            <section>
              <h2>Operators</h2>
              {operators.map((item) => (
                <p>{text(item, "discord_username")}: {text(item, "role")}</p>
              ))}
              <h3>Pending invitations</h3>
              {invitations.map((item) => (
                <p>
                  {text(item, "target_discord_user_id")}:{" "}
                  {text(item, "invited_role")}
                </p>
              ))}
              <form method="post" action="/settings/operators/invite">
                <input
                  name="discord_user_id"
                  required
                  placeholder="Discord user ID"
                />
                <select name="role">
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
