import type { IntelligenceSnapshot } from "@/src/web/web_intelligence.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { Chip, EmptyState, Meter, severityTone, WindowLabel } from "./ui.tsx";

const cell = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  const ts = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?/u);
  return ts ? `${ts[1]} ${ts[2]}` : text;
};

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export function IntelligenceWorkspace(
  { snapshot }: { readonly snapshot: IntelligenceSnapshot },
) {
  const { summary, alerts, cases, relationships, reports } = snapshot;
  return (
    <div class="app-shell">
      <DashboardHeader active="/intelligence" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Investigations</p>
            <h1>Intelligence</h1>
            <p class="lede">
              Triage alerts, work cases, and trace entity relationships.
            </p>
          </div>
        </section>

        <div class="metric-cards">
          <div class="metric-card" data-tone="warn">
            <span class="mc-label">Open alerts</span>
            <div class="mc-value num">{num(summary.open_alerts)}</div>
            <div class="mc-meta">
              <WindowLabel text="awaiting triage" />
            </div>
          </div>
          <div class="metric-card" data-tone="info">
            <span class="mc-label">Open cases</span>
            <div class="mc-value num">{num(summary.open_cases)}</div>
            <div class="mc-meta">
              <WindowLabel text="active" />
            </div>
          </div>
          <div class="metric-card" data-tone="ok">
            <span class="mc-label">Relationships</span>
            <div class="mc-value num">{num(summary.relationships)}</div>
            <div class="mc-meta">
              <WindowLabel text="tracked" />
            </div>
          </div>
          <div class="metric-card" data-tone="neutral">
            <span class="mc-label">Reports</span>
            <div class="mc-value num">{num(summary.reports)}</div>
            <div class="mc-meta">
              <WindowLabel text="generated" />
            </div>
          </div>
        </div>

        <section class="panel analytics-section">
          <h2>
            Alert queue <WindowLabel text="severity order" />
          </h2>
          {alerts.length === 0
            ? (
              <EmptyState
                title="No open alerts"
                hint="Alerts queue here when analysis flags content or behavior above the community threshold, ordered by severity for triage."
                columns={[
                  "Severity",
                  "Finding",
                  "Subject",
                  "Confidence",
                  "Status",
                  "Raised",
                ]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Severity</th>
                      <th scope="col">Finding</th>
                      <th scope="col">Subject</th>
                      <th scope="col">Confidence</th>
                      <th scope="col">Status</th>
                      <th scope="col">Raised</th>
                    </tr>
                  </thead>
                  <tbody>
                    {alerts.map((alert) => (
                      <tr key={String(alert.id)}>
                        <td>
                          <Chip
                            tone={severityTone(alert.severity)}
                            label={String(alert.severity ?? "?")}
                          />
                        </td>
                        <td>{cell(alert.title)}</td>
                        <td>
                          {alert.user_id != null
                            ? (
                              <a
                                href={`/users/${String(alert.user_id)}`}
                                class="text-link"
                              >
                                {cell(alert.primary_display_name)}
                              </a>
                            )
                            : cell(alert.primary_display_name)}
                        </td>
                        <td>
                          <Meter
                            value={num(alert.confidence)}
                            tone={severityTone(alert.severity)}
                            format={(v) => `${Math.round(v * 100)}%`}
                          />
                        </td>
                        <td>
                          <Chip
                            tone={severityTone(alert.status)}
                            label={String(alert.status ?? "—")}
                          />
                        </td>
                        <td class="num">{cell(alert.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>

        <div class="analytics-grid">
          <section class="panel analytics-section">
            <h2>Cases</h2>
            {cases.length === 0
              ? (
                <EmptyState
                  title="No investigation cases"
                  hint="Promote an alert to a case to begin a structured investigation with entities, evidence, and activity."
                  columns={[
                    "Case",
                    "Priority",
                    "Status",
                    "Entities",
                    "Evidence",
                    "Updated",
                  ]}
                />
              )
              : (
                <div class="table-wrap">
                  <table class="command-table">
                    <thead>
                      <tr>
                        <th scope="col">Case</th>
                        <th scope="col">Priority</th>
                        <th scope="col">Status</th>
                        <th scope="col">Entities</th>
                        <th scope="col">Evidence</th>
                        <th scope="col">Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {cases.map((c) => (
                        <tr key={String(c.id)}>
                          <td>
                            <a
                              href={`/intelligence/cases/${String(c.id)}`}
                              class="text-link"
                            >
                              {cell(c.title)}
                            </a>
                          </td>
                          <td>
                            <Chip
                              tone={severityTone(c.priority)}
                              label={String(c.priority ?? "—")}
                            />
                          </td>
                          <td>
                            <Chip
                              tone={severityTone(c.status)}
                              label={String(c.status ?? "—")}
                            />
                          </td>
                          <td class="num">{num(c.entity_count)}</td>
                          <td class="num">{num(c.evidence_count)}</td>
                          <td class="num">{cell(c.updated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
          </section>

          <section class="panel analytics-section">
            <h2>Entity relationships</h2>
            {relationships.length === 0
              ? (
                <EmptyState
                  title="No relationships inferred yet"
                  hint="Links between users form as they interact across platforms, building a relationship graph with strength and evidence."
                  columns={[
                    "Source",
                    "Relationship",
                    "Target",
                    "Strength",
                    "Evidence",
                  ]}
                />
              )
              : (
                <div class="table-wrap">
                  <table class="command-table">
                    <thead>
                      <tr>
                        <th scope="col">Source</th>
                        <th scope="col">Relationship</th>
                        <th scope="col">Target</th>
                        <th scope="col">Strength</th>
                        <th scope="col">Evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {relationships.map((r) => (
                        <tr key={String(r.id)}>
                          <td>{cell(r.source_name)}</td>
                          <td>{cell(r.relationship_type)}</td>
                          <td>{cell(r.target_name)}</td>
                          <td>
                            <Meter value={num(r.strength)} tone="info" />
                          </td>
                          <td class="num">{num(r.evidence_count)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
          </section>
        </div>

        <section class="panel analytics-section">
          <h2>Reports</h2>
          {reports.length === 0
            ? (
              <EmptyState
                title="No reports generated"
                hint="Reports produced from the report action — daily summaries and entity profiles — are listed here."
                columns={["Title", "Type", "Summary", "Generated"]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Title</th>
                      <th scope="col">Type</th>
                      <th scope="col">Summary</th>
                      <th scope="col">Generated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reports.map((r) => (
                      <tr key={String(r.id)}>
                        <td>{cell(r.title)}</td>
                        <td>
                          <Chip tone="info" label={cell(r.report_type)} />
                        </td>
                        <td>{cell(r.summary)}</td>
                        <td class="num">{cell(r.generated_at)}</td>
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
