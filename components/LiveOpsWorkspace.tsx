import type { LiveOpsSnapshot } from "@/src/web/web_live_ops.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
export function LiveOpsWorkspace(
  { snapshot, canManage, canGoLive, status }: {
    readonly snapshot: LiveOpsSnapshot;
    readonly canManage: boolean;
    readonly canGoLive?: boolean;
    readonly status?: string;
  },
) {
  const community = snapshot.community as Record<string, unknown>;
  const incidents = snapshot.active_incidents as Array<Record<string, unknown>>;
  const alerts = snapshot.open_alerts as Array<Record<string, unknown>>;
  const operations = snapshot.operations as Record<string, unknown>;
  const liveStreams = (snapshot.live_streams ?? []) as Array<
    Record<string, unknown>
  >;
  const liveNow = (snapshot.last_5_minutes ?? {}) as Record<string, unknown>;
  return (
    <div class="app-shell">
      <DashboardHeader active="/live-ops" />
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
        {status ? <p class="status-banner">{status}</p> : null}

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Broadcast</p>
              <h2>Stream status</h2>
            </div>
            <p>
              Announce the current Twitch stream to your Discord servers.
            </p>
          </div>
          {liveStreams.length === 0
            ? (
              <p class="empty-state">
                No live Twitch stream detected for this community.
              </p>
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Channel</th>
                      <th scope="col">Title</th>
                      <th scope="col">Status</th>
                      <th scope="col">Started</th>
                    </tr>
                  </thead>
                  <tbody>
                    {liveStreams.map((stream) => (
                      <tr key={String(stream.id)}>
                        <td class="command-name">
                          {String(stream.stream_key)}
                        </td>
                        <td>{String(stream.title ?? "—")}</td>
                        <td>{String(stream.status)}</td>
                        <td>{String(stream.started_at ?? "—")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          {canGoLive
            ? (
              <form
                method="post"
                action="/dashboard/go-live"
                class="command-new"
              >
                <p class="lede" style="margin: 0; flex: 1;">
                  Send a “now live” announcement to every linked Discord server
                  for the active Twitch stream.
                </p>
                <button type="submit">Announce live stream</button>
              </form>
            )
            : null}
        </section>

        <dl class="metric-grid">
          <div>
            <dt>Pending actions</dt>
            <dd>{String(operations.pending_actions)}</dd>
          </div>
          <div>
            <dt>Open reviews</dt>
            <dd>{String(operations.open_reviews)}</dd>
          </div>
          <div>
            <dt>Dead letters</dt>
            <dd>{String(operations.dead_letters)}</dd>
          </div>
          <div>
            <dt>Messages (5 min)</dt>
            <dd>{String(liveNow.messages ?? 0)}</dd>
          </div>
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
