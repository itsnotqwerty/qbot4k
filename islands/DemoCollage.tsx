import { useSignal } from "@preact/signals";

type PanelId =
  | "moderation"
  | "intelligence"
  | "liveops"
  | "commands"
  | "analytics";

const panels: { id: PanelId; label: string }[] = [
  { id: "moderation", label: "Moderation" },
  { id: "intelligence", label: "Intelligence" },
  { id: "liveops", label: "Live ops" },
  { id: "commands", label: "Commands" },
  { id: "analytics", label: "Analytics" },
];

function Chip({ tone, children }: { tone: string; children: string }) {
  return <span class={`chip chip-${tone}`}>{children}</span>;
}

/* ---------------- Moderation ---------------- */

interface Review {
  readonly id: number;
  readonly target: string;
  readonly content: string;
  readonly severity: string;
  readonly reasonCode: string;
  readonly platform: string;
}

const initialReviews: Review[] = [
  {
    id: 4821,
    target: "raid.spam.01",
    content: "FREE NITRO → https://nitro-gift.example.ru claim now @everyone",
    severity: "high",
    reasonCode: "scam_link",
    platform: "discord",
  },
  {
    id: 4822,
    target: "quiet.lurker",
    content: "mass mention: 24 users pinged across 3 channels in 40s",
    severity: "medium",
    reasonCode: "mass_mention",
    platform: "discord",
  },
  {
    id: 4823,
    target: "mod.alt.7",
    content: "account flagged by evasion heuristic — matches banned user #1183",
    severity: "critical",
    reasonCode: "ban_evasion",
    platform: "twitch",
  },
];

function ModerationDemo() {
  const resolved = useSignal<
    { id: number; resolution: string; action: string }[]
  >(
    [],
  );
  const resolution = useSignal("confirmed");
  const action = useSignal("timeout");
  const active = useSignal<Review | null>(null);

  const open = initialReviews.filter((r) =>
    !resolved.value.some((d) => d.id === r.id)
  );

  return (
    <div class="demo-body">
      <div class="demo-heading-row">
        <p class="demo-stat">
          <span class="num">{open.length}</span> open reviews
        </p>
        <span class="demo-evidence">
          queue: unassigned · oldest SLA age 2.3h
        </span>
      </div>
      <ul class="demo-list">
        {initialReviews.map((r) => {
          const done = resolved.value.find((d) => d.id === r.id);
          return (
            <li key={r.id} class={done ? "is-done" : ""}>
              <span class="demo-user">@{r.target}</span>
              <span class="demo-reason">
                {r.platform} · {r.content}
              </span>
              <Chip tone={r.severity === "medium" ? "warn" : "danger"}>
                {`${r.severity} · ${r.reasonCode}`}
              </Chip>
              {done
                ? (
                  <Chip tone="ok">
                    {done.action
                      ? `${done.resolution} · ${done.action}`
                      : done.resolution}
                  </Chip>
                )
                : (
                  <button
                    type="button"
                    onClick={() =>
                      active.value = active.value?.id === r.id ? null : r}
                  >
                    {active.value?.id === r.id ? "Cancel" : "Triage"}
                  </button>
                )}
            </li>
          );
        })}
      </ul>
      {active.value && (
        <form
          class="demo-resolve"
          onSubmit={(e) => {
            e.preventDefault();
            const review = active.value;
            if (!review) return;
            resolved.value = [
              ...resolved.value,
              {
                id: review.id,
                resolution: resolution.value,
                action: action.value,
              },
            ];
            active.value = null;
          }}
        >
          <p class="section-label">
            Resolve review #{active.value.id} — @{active.value.target}
          </p>
          <label>
            Resolution
            <select
              value={resolution.value}
              onChange={(e) =>
                resolution.value = (e.target as HTMLSelectElement).value}
            >
              <option value="confirmed">confirmed</option>
              <option value="dismissed">dismissed</option>
              <option value="escalated">escalated</option>
            </select>
          </label>
          <label>
            Action
            <select
              value={action.value}
              onChange={(e) =>
                action.value = (e.target as HTMLSelectElement).value}
            >
              <option value="">no action</option>
              <option value="warn">warn</option>
              <option value="timeout">timeout</option>
              <option value="ban">ban</option>
            </select>
          </label>
          <button type="submit">Resolve</button>
          <p class="demo-evidence">
            Sanctions queue as pending and complete only after provider
            confirmation — every outcome is reversible and audited.
          </p>
        </form>
      )}
    </div>
  );
}

/* ---------------- Intelligence ---------------- */

interface Alert {
  readonly id: number;
  readonly title: string;
  readonly subject: string;
  readonly severity: string;
  readonly confidence: number;
}

const alerts: Alert[] = [
  {
    id: 91,
    title: "Coordinated invite-wave across #general and #clips",
    subject: "raid.spam.01",
    severity: "critical",
    confidence: 0.93,
  },
  {
    id: 92,
    title: "Evasion cluster shares redirect domain with banned cohort",
    subject: "mod.alt.7",
    severity: "high",
    confidence: 0.81,
  },
  {
    id: 93,
    title: "New-account reply chains boosting a single external link",
    subject: "quiet.lurker",
    severity: "medium",
    confidence: 0.64,
  },
];

function IntelligenceDemo() {
  const status = useSignal<Record<number, string>>({});
  const caseOpened = useSignal(false);
  const open = alerts.filter((a) => !status.value[a.id]).length;

  return (
    <div class="demo-body">
      <div class="demo-heading-row">
        <p class="demo-stat">
          <span class="num">{open}</span> alerts awaiting triage
        </p>
        {caseOpened.value && <Chip tone="info">case #118 opened</Chip>}
      </div>
      <div class="table-wrap">
        <table class="demo-table">
          <thead>
            <tr>
              <th>Severity</th>
              <th>Finding</th>
              <th>Confidence</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((a) => (
              <tr key={a.id}>
                <td>
                  <Chip
                    tone={a.severity === "medium"
                      ? "warn"
                      : a.severity === "low"
                      ? "info"
                      : "danger"}
                  >
                    {a.severity}
                  </Chip>
                </td>
                <td>
                  <strong>{a.title}</strong>
                  <br />
                  <span class="demo-evidence">subject: @{a.subject}</span>
                </td>
                <td>
                  <div class="demo-meter small" aria-hidden="true">
                    <span
                      style={{ width: `${Math.round(a.confidence * 100)}%` }}
                    />
                  </div>
                  <span class="demo-evidence">
                    {Math.round(a.confidence * 100)}%
                  </span>
                </td>
                <td>
                  <Chip tone={status.value[a.id] ? "ok" : "danger"}>
                    {status.value[a.id] ?? "open"}
                  </Chip>
                </td>
                <td>
                  {!status.value[a.id] && (
                    <span class="demo-actions">
                      <button
                        type="button"
                        onClick={() =>
                          status.value = {
                            ...status.value,
                            [a.id]: "acknowledged",
                          }}
                      >
                        Acknowledge
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          status.value = { ...status.value, [a.id]: "in_case" };
                          caseOpened.value = true;
                        }}
                      >
                        Open case
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p class="demo-evidence">
        Cases aggregate entities, evidence, and relationships — e.g. 3 accounts
        sharing one redirect domain, strength 0.87, last observed 12m ago.
      </p>
    </div>
  );
}

/* ---------------- Live ops ---------------- */

function LiveOpsDemo() {
  const slowMode = useSignal(true);
  const raidGuard = useSignal(true);
  const incidentOpen = useSignal(true);

  return (
    <div class="demo-body">
      <div class="table-wrap">
        <table class="demo-table">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Title</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="demo-user">twitch / qbot4k_live</td>
              <td>Community night — raids welcome</td>
              <td>
                <Chip tone="ok">live</Chip>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="demo-metric-row">
        <div>
          <p class="demo-stat">
            <span class="num">
              {(incidentOpen.value ? 2841 : 1184).toLocaleString()}
            </span>
          </p>
          <p class="demo-evidence">messages · 5 min</p>
        </div>
        <div>
          <p class="demo-stat">
            <span class="num">{incidentOpen.value ? 412 : 168}</span>
          </p>
          <p class="demo-evidence">unique chatters</p>
        </div>
        <div>
          <p class="demo-stat">
            <span class="num">{incidentOpen.value ? 1 : 0}</span>
          </p>
          <p class="demo-evidence">active incidents</p>
        </div>
      </div>
      <div class="demo-guards">
        <button
          type="button"
          aria-pressed={slowMode.value}
          onClick={() => slowMode.value = !slowMode.value}
        >
          Slow mode {slowMode.value ? "on · 5s" : "off"}
        </button>
        <button
          type="button"
          aria-pressed={raidGuard.value}
          onClick={() => raidGuard.value = !raidGuard.value}
        >
          Raid guard {raidGuard.value ? "armed" : "disarmed"}
        </button>
      </div>
      {incidentOpen.value
        ? (
          <div class="demo-signal-card">
            <p class="section-label">incident · high / active</p>
            <p class="demo-headline">
              Raid surge — 41 joins in 10 minutes, 27 matching redirect links
            </p>
            <p class="demo-evidence">
              Escalation destination: #mod-alerts webhook · minimum severity
              high
            </p>
            <button type="button" onClick={() => incidentOpen.value = false}>
              Resolve incident
            </button>
          </div>
        )
        : (
          <p class="demo-stat">
            <Chip tone="ok">No active incidents — monitoring</Chip>
          </p>
        )}
    </div>
  );
}

/* ---------------- Commands ---------------- */

interface TenantCommand {
  readonly name: string;
  readonly template: string;
  enabled: boolean;
}

function CommandsDemo() {
  const commands = useSignal<TenantCommand[]>([
    {
      name: "rules",
      template:
        "Community rules: https://example.com/rules — read before chatting",
      enabled: true,
    },
    {
      name: "lurk",
      template: "Thanks for lurking, ${user}! o/",
      enabled: true,
    },
    {
      name: "socials",
      template:
        "VODs → https://youtube.example · Clips → https://clips.example",
      enabled: false,
    },
  ]);
  const log = useSignal<string[]>([]);

  const run = (cmd: TenantCommand) => {
    if (!cmd.enabled) {
      log.value = [...log.value, `!${cmd.name}`, "✗ command disabled"];
      return;
    }
    log.value = [
      ...log.value,
      `!${cmd.name}`,
      `✓ ${cmd.template.replace("${user}", "viewer42")}`,
    ];
  };

  return (
    <div class="demo-body">
      <div class="demo-heading-row">
        <p class="demo-stat">
          <span class="num">
            {commands.value.filter((c) => c.enabled).length}
          </span>{" "}
          / {commands.value.length} enabled
        </p>
        <span class="demo-evidence">
          runtime: both providers (Discord + Twitch)
        </span>
      </div>
      <ul class="demo-list">
        {commands.value.map((c) => (
          <li key={c.name}>
            <span class="demo-user">!{c.name}</span>
            <span class="demo-reason">{c.template}</span>
            <button
              type="button"
              aria-pressed={c.enabled}
              onClick={() => {
                commands.value = commands.value.map((x) =>
                  x.name === c.name ? { ...x, enabled: !x.enabled } : x
                );
              }}
            >
              {c.enabled ? "Disable" : "Enable"}
            </button>
            <button type="button" onClick={() => run(c)}>Run</button>
          </li>
        ))}
      </ul>
      <div class="demo-console" aria-live="polite">
        {log.value.length === 0
          ? <code>Run a command to see the rendered response</code>
          : log.value.map((line, i) => <code key={i}>{line}</code>)}
      </div>
      <p class="demo-evidence">
        Templates support {"${query}"}, {"${1..6}"} ranges, and{" "}
        {"${GET}(url)[k:path]"}. Reserved commands (addcom, delcom, editcom,
        alias) are provider-managed.
      </p>
    </div>
  );
}

/* ---------------- Analytics ---------------- */

function AnalyticsDemo() {
  const range = useSignal<"7d" | "30d">("7d");
  const joins = range.value === "7d"
    ? [12, 19, 8, 24, 31, 17, 41]
    : [9, 14, 11, 18, 22, 15, 27, 19, 33, 24, 38, 41];
  const outcomes = [
    { label: "substantiated", value: 62, tone: "ok" },
    { label: "dismissed", value: 27, tone: "neutral" },
    { label: "escalated", value: 11, tone: "warn" },
  ];
  const total = outcomes.reduce((a, o) => a + o.value, 0);

  return (
    <div class="demo-body">
      <div class="demo-heading-row">
        <p class="section-label">Member growth — daily joins</p>
        <div class="demo-toggle" role="group" aria-label="Range">
          <button
            type="button"
            aria-pressed={range.value === "7d"}
            onClick={() => range.value = "7d"}
          >
            7d
          </button>
          <button
            type="button"
            aria-pressed={range.value === "30d"}
            onClick={() => range.value = "30d"}
          >
            30d
          </button>
        </div>
      </div>
      <div
        class="demo-bars"
        role="img"
        aria-label={`Daily joins, last ${range.value}`}
      >
        {joins.map((v, i) => (
          <span
            key={`${range.value}-${i}`}
            style={{ height: `${Math.min(100, v * 2.4)}%` }}
            title={`${v} joins`}
          />
        ))}
      </div>
      <p class="section-label">Report outcomes</p>
      <div class="demo-proportion" role="img" aria-label="Report outcome mix">
        {outcomes.map((o) => (
          <span
            key={o.label}
            class={`demo-seg demo-seg-${o.tone}`}
            style={{ width: `${(o.value / total) * 100}%` }}
            title={`${o.label}: ${o.value}%`}
          />
        ))}
      </div>
      <div class="demo-legend">
        {outcomes.map((o) => (
          <span key={o.label}>
            <i class={`demo-dot demo-seg-${o.tone}`} /> {o.label} {o.value}%
          </span>
        ))}
      </div>
      <p class="demo-evidence">
        Emerging topics surface after the 24h analysis window; repeat offenses
        and cohort anomalies appear as signals accumulate.
      </p>
    </div>
  );
}

/* ---------------- Collage ---------------- */

export default function DemoCollage() {
  const active = useSignal<PanelId>("moderation");
  return (
    <section class="demo-collage" aria-labelledby="demo-collage-title">
      <div class="demo-collage-head">
        <p class="section-label">Interactive demo</p>
        <h2 id="demo-collage-title">The dashboard, live in miniature</h2>
        <p>
          Every tile mirrors a real operator workflow. Triage a review queue
          item, acknowledge an alert into a case, work a live raid incident, or
          run a tenant command.
        </p>
      </div>
      <div class="demo-tabs" role="tablist" aria-label="Demo panels">
        {panels.map((p) => (
          <button
            key={p.id}
            type="button"
            role="tab"
            aria-selected={active.value === p.id}
            class={active.value === p.id ? "is-active" : ""}
            onClick={() => active.value = p.id}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div class="demo-stage" role="tabpanel">
        {active.value === "moderation" && <ModerationDemo />}
        {active.value === "intelligence" && <IntelligenceDemo />}
        {active.value === "liveops" && <LiveOpsDemo />}
        {active.value === "commands" && <CommandsDemo />}
        {active.value === "analytics" && <AnalyticsDemo />}
      </div>
    </section>
  );
}
