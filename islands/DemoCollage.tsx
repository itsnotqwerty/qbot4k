import { useSignal } from "@preact/signals";

type PanelId = "moderation" | "signals" | "liveops" | "commands" | "analytics";

const panels: { id: PanelId; label: string }[] = [
  { id: "moderation", label: "Moderation" },
  { id: "signals", label: "Signals" },
  { id: "liveops", label: "Live ops" },
  { id: "commands", label: "Commands" },
  { id: "analytics", label: "Analytics" },
];

function ModerationDemo() {
  const reviews = useSignal([
    { id: 4821, user: "raid.spam.01", reason: "Cross-post spam wave", state: "open" },
    { id: 4822, user: "quiet.lurker", reason: "Mass mention anomaly", state: "open" },
    { id: 4823, user: "mod.alt.7", reason: "Evasion heuristic match", state: "open" },
  ]);
  const resolve = (id: number) => {
    reviews.value = reviews.value.map((r) =>
      r.id === id ? { ...r, state: "resolved" } : r
    );
  };
  const open = reviews.value.filter((r) => r.state === "open").length;
  return (
    <div class="demo-body">
      <p class="demo-stat">
        <span class="num">{open}</span> open reviews
      </p>
      <ul class="demo-list">
        {reviews.value.map((r) => (
          <li key={r.id} class={r.state === "resolved" ? "is-done" : ""}>
            <span class="demo-user">@{r.user}</span>
            <span class="demo-reason">{r.reason}</span>
            {r.state === "open"
              ? (
                <button type="button" onClick={() => resolve(r.id)}>
                  Resolve
                </button>
              )
              : <span class="chip chip-ok">Resolved</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SignalsDemo() {
  const acked = useSignal(false);
  return (
    <div class="demo-body">
      <div class="demo-signal-card">
        <p class="section-label">Anomaly · severity 3</p>
        <p class="demo-headline">
          Invite-join velocity 6.4σ above baseline in #general
        </p>
        <p class="demo-evidence">
          Evidence: 41 joins / 10 min · 38 with default avatars · 27 matching
          redirect links
        </p>
        {acked.value
          ? <span class="chip chip-ok">Acknowledged · case opened</span>
          : (
            <button type="button" onClick={() => acked.value = true}>
              Acknowledge &amp; open case
            </button>
          )}
      </div>
    </div>
  );
}

function LiveOpsDemo() {
  const paused = useSignal(false);
  return (
    <div class="demo-body">
      <p class="demo-stat">
        Stream state:{" "}
        <span class={paused.value ? "chip chip-warn" : "chip chip-ok"}>
          {paused.value ? "Alerts paused" : "Live · ingesting"}
        </span>
      </p>
      <div class="demo-meter" aria-hidden="true">
        <span style={{ width: paused.value ? "22%" : "86%" }} />
      </div>
      <p class="demo-evidence">
        Chat throughput {paused.value ? "142" : "1,284"} msg/min · raids
        guarded · slow mode auto
      </p>
      <button type="button" onClick={() => paused.value = !paused.value}>
        {paused.value ? "Resume alerts" : "Pause alerts"}
      </button>
    </div>
  );
}

function CommandsDemo() {
  const log = useSignal<string[]>([
    "$ !timeout raid.spam.01 10m cross-post spam",
    "✓ provider-confirmed · reversible · audit #9321",
  ]);
  const run = () => {
    log.value = [
      ...log.value,
      "$ !case open invite-wave-41",
      "✓ case #118 opened · evidence attached",
    ];
  };
  return (
    <div class="demo-body">
      <div class="demo-console">
        {log.value.map((line) => <code key={line}>{line}</code>)}
      </div>
      <button
        type="button"
        onClick={run}
        disabled={log.value.length > 2}
      >
        {log.value.length > 2 ? "Executed" : "Run next command"}
      </button>
    </div>
  );
}

function AnalyticsDemo() {
  const range = useSignal<"7d" | "30d">("7d");
  const bars = range.value === "7d"
    ? [42, 58, 36, 71, 64, 80, 55]
    : [30, 44, 38, 52, 61, 47, 69, 58, 74, 66, 80, 71];
  return (
    <div class="demo-body">
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
      <div class="demo-bars" aria-label="Moderation actions trend">
        {bars.map((v, i) => (
          <span key={`${range.value}-${i}`} style={{ height: `${v}%` }} />
        ))}
      </div>
      <p class="demo-evidence">
        Reversal rate 1.2% · median time-to-action 38s
      </p>
    </div>
  );
}

export default function DemoCollage() {
  const active = useSignal<PanelId>("moderation");
  return (
    <section class="demo-collage" aria-labelledby="demo-collage-title">
      <div class="demo-collage-head">
        <p class="section-label">Interactive demo</p>
        <h2 id="demo-collage-title">The dashboard, live in miniature</h2>
        <p>
          Every tile below is a working slice of the operator dashboard. Click
          around — resolve a review, acknowledge a signal, pause live ops.
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
        {active.value === "signals" && <SignalsDemo />}
        {active.value === "liveops" && <LiveOpsDemo />}
        {active.value === "commands" && <CommandsDemo />}
        {active.value === "analytics" && <AnalyticsDemo />}
      </div>
    </section>
  );
}
