import type { DashboardItem } from "@/src/web/web_queries.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { Trend, WindowLabel } from "./ui.tsx";

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

type Tone = "ok" | "warn" | "danger" | "info" | "neutral";

interface Card {
  readonly label: string;
  readonly value: number;
  readonly tone: Tone;
  readonly window: string;
  readonly href: string;
  readonly current?: number;
  readonly previous?: number | null;
  readonly invert?: boolean;
}

export function OverviewWorkspace(
  { metrics }: { readonly metrics: DashboardItem },
) {
  const openReviews = num(metrics.open_reviews);
  const pendingActions = num(metrics.pending_actions);
  const highAlerts = num(metrics.high_alerts);
  const messages24h = num(metrics.messages_24h);

  const cards: readonly Card[] = [
    {
      label: "Attention required",
      value: openReviews + pendingActions + highAlerts,
      tone: (openReviews + pendingActions + highAlerts) > 0 ? "danger" : "ok",
      window: "now",
      href: "/moderation",
    },
    {
      label: "Messages · 24h",
      value: messages24h,
      tone: "info",
      window: "last 24h",
      href: "/search",
      current: messages24h,
      previous: num(metrics.messages_prev_24h),
    },
    {
      label: "Open reviews",
      value: openReviews,
      tone: openReviews > 0 ? "warn" : "ok",
      window: "queue",
      href: "/moderation",
      current: openReviews,
      previous: null,
      invert: true,
    },
    {
      label: "Pending actions",
      value: pendingActions,
      tone: pendingActions > 0 ? "warn" : "ok",
      window: "queue",
      href: "/moderation",
      invert: true,
    },
    {
      label: "High-severity alerts",
      value: highAlerts,
      tone: highAlerts > 0 ? "danger" : "ok",
      window: "open",
      href: "/intelligence",
      invert: true,
    },
    {
      label: "Active signals",
      value: num(metrics.derived_signals),
      tone: "info",
      window: "last 24h",
      href: "/signals",
    },
  ];

  return (
    <div class="app-shell">
      <DashboardHeader active="/dashboard" />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community workspace</p>
            <h1>Overview</h1>
            <p class="lede">
              Operational posture at a glance. Lead with what needs attention.
            </p>
          </div>
        </section>

        <div class="metric-cards">
          {cards.map((card) => (
            <a
              key={card.label}
              class="metric-card"
              data-tone={card.tone}
              href={card.href}
            >
              <span class="mc-label">{card.label}</span>
              <div class="mc-value num">{card.value}</div>
              <div class="mc-meta">
                {card.current !== undefined
                  ? (
                    <Trend
                      current={card.current}
                      previous={card.previous ?? null}
                      invert={card.invert ?? false}
                    />
                  )
                  : null}
                <span class="mc-window">{card.window}</span>
              </div>
            </a>
          ))}
        </div>
      </main>
    </div>
  );
}
