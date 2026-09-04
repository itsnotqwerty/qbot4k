import type { LiveOpsSnapshot } from "@/src/web/web_live_ops.ts";
export function LiveOpsWorkspace(
  { snapshot, canManage }: {
    readonly snapshot: LiveOpsSnapshot;
    readonly canManage: boolean;
  },
) {
  const community = snapshot.community as Record<string, unknown>;
  const incidents = snapshot.active_incidents as Array<Record<string, unknown>>;
  const alerts = snapshot.open_alerts as Array<Record<string, unknown>>;
  const operations = snapshot.operations as Record<string, unknown>;
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/live-ops" aria-current="page">Live operations</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Live operations</p>
            <h1>{String(community.name)}</h1>
            <p class="lede">
              Current incidents, alerts, moderation actions, and provider
              confirmations.
            </p>
          </div>
        </section>
        <dl>
          <dt>Pending actions</dt>
          <dd>{String(operations.pending_actions)}</dd>
          <dt>Open reviews</dt>
          <dd>{String(operations.open_reviews)}</dd>
          <dt>Dead letters</dt>
          <dd>{String(operations.dead_letters)}</dd>
        </dl>
        <section>
          <h2>Active incidents</h2>
          {incidents.map((incident) => (
            <article>
              <h3>{String(incident.title)}</h3>
              <p>{String(incident.severity)} / {String(incident.status)}</p>
            </article>
          ))}
        </section>
        <section>
          <h2>Open alerts</h2>
          {alerts.map((alert) => (
            <article>
              <h3>{String(alert.title)}</h3>
              <p>{String(alert.summary)}</p>
            </article>
          ))}
        </section>
        {canManage
          ? (
            <form data-live-ops-destination>
              <h2>Escalation destination</h2>
              <input name="name" required />
              <select name="destination_type">
                <option value="discord_webhook">Discord webhook</option>
                <option value="slack_webhook">Slack webhook</option>
                <option value="generic_webhook">Generic webhook</option>
              </select>
              <input type="url" name="target" required />
              <select name="minimum_severity">
                <option value="high">High</option>
                <option value="critical">Critical</option>
                <option value="medium">Medium</option>
              </select>
              <button type="submit">Add destination</button>
            </form>
          )
          : null}
      </main>
    </div>
  );
}
