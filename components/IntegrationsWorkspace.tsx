import type { IntegrationSnapshot } from "@/src/web/web_integrations.ts";
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
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/integrations" aria-current="page">Integrations</a>
        </nav>
      </header>
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
        <h2>Discord</h2>
        {canManage && guilds.length
          ? (
            <form method="post" action="/integrations/discord/link">
              <input
                type="hidden"
                name="community_id"
                value={String(community.id ?? "")}
              />
              <select name="guild_id" required>
                {guilds.map((guild) => (
                  <option value={String(guild.guild_id)}>
                    {String(guild.guild_id)}
                  </option>
                ))}
              </select>
              <input name="pilot_invite_code" required autoComplete="off" />
              <button type="submit">Link Discord</button>
            </form>
          )
          : <p>No manageable Discord servers are available.</p>}
        {discord.map((item) => (
          <p>{String(item.display_name)}: {String(item.status)}</p>
        ))}
        <h2>Twitch</h2>
        {canManage
          ? (
            <form method="post" action="/integrations/twitch/link">
              <input name="broadcaster_login" required autoComplete="off" />
              {[
                "moderator:read:followers",
                "channel:read:subscriptions",
                "moderator:manage:banned_users",
                "moderator:manage:chat_settings",
                "moderator:manage:shield_mode",
              ].map((scope, index) => (
                <label>
                  <input
                    type="checkbox"
                    name="scope"
                    value={scope}
                    checked={index < 2}
                  />{" "}
                  {scope}
                </label>
              ))}
              <button type="submit">Link Twitch</button>
            </form>
          )
          : null}
        {twitch.map((item) => (
          <p>
            {String(item.display_name)}: {String(item.status)} /{" "}
            {String(item.health_status)}
          </p>
        ))}
      </main>
    </div>
  );
}
