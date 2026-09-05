// Shared presentational primitives for the operational console surfaces.
// Implements the status color language, tabular numerals, magnitude meters,
// trend indicators, and freshness labels described in the design review.

export type Tone = "ok" | "warn" | "danger" | "info" | "neutral";

export function Chip(
  { tone = "neutral", label }: { readonly tone?: Tone; readonly label: string },
) {
  return <span class={`chip chip-${tone}`}>{label}</span>;
}

// Map free-form severity/status text onto the status ramp.
export function severityTone(value: unknown): Tone {
  const v = String(value ?? "").toLocaleLowerCase();
  if (["critical", "high", "danger", "failed", "error", "open"].includes(v)) {
    return v === "open" ? "warn" : "danger";
  }
  if (["medium", "warn", "warning", "pending", "degraded"].includes(v)) {
    return "warn";
  }
  if (["low", "info", "informational", "new"].includes(v)) return "info";
  if (
    [
      "ok",
      "ready",
      "resolved",
      "closed",
      "completed",
      "confirmed",
      "sent",
      "active",
      "acknowledged",
    ].includes(v)
  ) {
    return "ok";
  }
  return "neutral";
}

export function Meter(
  { value, max = 1, tone = "info", format }: {
    readonly value: number;
    readonly max?: number;
    readonly tone?: Tone;
    readonly format?: (value: number) => string;
  },
) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const text = format ? format(value) : String(Math.round(value * 100) / 100);
  return (
    <span class="meter">
      <span class="meter-track">
        <span
          class={`meter-fill${tone === "info" ? "" : ` tone-${tone}`}`}
          style={{ width: `${(ratio * 100).toFixed(1)}%` }}
        />
      </span>
      <span class="meter-value">{text}</span>
    </span>
  );
}

export function Trend(
  { current, previous, invert = false }: {
    readonly current: number;
    readonly previous: number | null | undefined;
    readonly invert?: boolean;
  },
) {
  if (previous === null || previous === undefined) {
    return <span class="trend flat">—</span>;
  }
  const delta = current - previous;
  if (delta === 0) return <span class="trend flat">▬ 0</span>;
  // `invert` flips whether an increase is good (e.g. open reviews going up is bad).
  const positive = invert ? delta < 0 : delta > 0;
  const cls = positive ? "up" : "down";
  const arrow = delta > 0 ? "▲" : "▼";
  return <span class={`trend ${cls}`}>{arrow} {Math.abs(delta)}</span>;
}

export function WindowLabel({ text }: { readonly text: string }) {
  return <span class="window-label">{text}</span>;
}

// Structured empty state: an icon, a title, guidance on what appears here and
// why, plus a ghost skeleton of the expected columns so the layout reads as
// intentional even with zero rows.
export function EmptyState(
  { title, hint, columns = [], rows = 3 }: {
    readonly title: string;
    readonly hint?: string;
    readonly columns?: readonly string[];
    readonly rows?: number;
  },
) {
  return (
    <div class="empty-block">
      <div class="empty-head">
        <span class="empty-icon" aria-hidden="true">▦</span>
        <div>
          <p class="empty-title">{title}</p>
          {hint ? <p class="empty-hint">{hint}</p> : null}
        </div>
      </div>
      {columns.length
        ? (
          <div class="empty-skeleton" aria-hidden="true">
            <div class="empty-skeleton-cols">
              {columns.map((column) => <span key={column}>{column}</span>)}
            </div>
            {Array.from({ length: rows }).map((_, index) => (
              <div class="empty-skeleton-row" key={index}>
                {columns.map((column) => <i key={column} />)}
              </div>
            ))}
          </div>
        )
        : null}
    </div>
  );
}
