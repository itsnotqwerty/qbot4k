import type { IntegrationSnapshot } from "@/src/web/web_integrations.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState } from "./ui.tsx";
export function IntegrationsWorkspace(
  { community, guilds, installations, canManage, status, error }:
    & IntegrationSnapshot
    & {
      readonly canManage: boolean;
      readonly status: string;
      readonly error: string;
    },
) {
  const discord = installations.filter((item) => item.platform === "discord");
  const twitch = installations.filter((item) => item.platform === "twitch");
  return (
    <div class="app-shell">
      <DashboardHeader active="/integrations" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community integrations</p>
            <h1>Discord and Twitch</h1>
            <p class="lede">
              Connect provider installations to {String(community.name)}.
            </p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}
        {error
          ? (
            <p class="status-banner error">
              {error} Retry the Twitch connection.
            </p>
          )
          : null}

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Discord</p>
              <h2>Servers</h2>
            </div>
            <p>
              Add QBot4K to a server you administer. The bot is added with
              Administrator permissions.
            </p>
          </div>
          {canManage && guilds.length > 0
            ? (
              <form
                method="post"
                action="/integrations/discord/link"
                class="command-new"
              >
                <input
                  type="hidden"
                  name="community_id"
                  value={String(community.id ?? "")}
                />
                <select name="guild_id" required aria-label="Discord server">
                  {guilds.map((guild) => (
                    <option value={String(guild.guild_id)}>
                      {String(guild.guild_name ?? guild.guild_id)}
                    </option>
                  ))}
                </select>
                <button type="submit">Add QBot4K to server</button>
              </form>
            )
            : canManage
            ? (
              <p class="empty-state">
                No manageable Discord servers available.
              </p>
            )
            : null}
          {discord.length === 0
            ? (
              <EmptyState
                title="No Discord servers connected"
                hint="Add QBot4K to a server you administer. Connected servers and their ingestion status appear here."
                columns={["Server", "Channels", "Status", "Health"]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Server</th>
                      <th scope="col">Status</th>
                      <th scope="col">Health</th>
                    </tr>
                  </thead>
                  <tbody>
                    {discord.map((item) => (
                      <tr key={String(item.id)}>
                        <td class="command-name">
                          {String(item.display_name)}
                        </td>
                        <td>{String(item.status)}</td>
                        <td>{String(item.health_status ?? "—")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Twitch</p>
              <h2>Channels</h2>
            </div>
            <p>
              Connect a channel you broadcast on. Chat read/edit scopes let the
              bot read and reply to chat.
            </p>
          </div>
          {canManage
            ? (
              <form method="post" action="/integrations/twitch/link">
                <div class="command-new">
                  <input
                    name="broadcaster_login"
                    required
                    autoComplete="off"
                    placeholder="channel name"
                    aria-label="Twitch channel"
                  />
                  <button type="submit">Connect Twitch channel</button>
                </div>
                <fieldset class="scope-grid">
                  <legend>Permissions</legend>
                  {([
                    ["chat:read", true],
                    ["chat:edit", true],
                    ["moderator:read:followers", true],
                    ["channel:read:subscriptions", true],
                    ["moderator:manage:banned_users", false],
                    ["moderator:manage:chat_settings", false],
                    ["moderator:manage:shield_mode", false],
                  ] as [string, boolean][]).map(([scope, checked]) => (
                    <label class="toggle" key={scope}>
                      <input
                        type="checkbox"
                        name="scope"
                        value={scope}
                        checked={checked}
                      />
                      <span>{scope}</span>
                    </label>
                  ))}
                </fieldset>
              </form>
            )
            : null}
          {twitch.length === 0
            ? (
              <EmptyState
                title="No Twitch channels connected"
                hint="Link a Twitch channel to ingest chat and run commands. Connected channels and their health appear here."
                columns={["Channel", "Status", "Health", "Scopes"]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Channel</th>
                      <th scope="col">Status</th>
                      <th scope="col">Health</th>
                    </tr>
                  </thead>
                  <tbody>
                    {twitch.map((item) => (
                      <tr key={String(item.id)}>
                        <td class="command-name">
                          {String(item.display_name)}
                        </td>
                        <td>{String(item.status)}</td>
                        <td>{String(item.health_status ?? "—")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>
      </main>
    </div>
  );
}
