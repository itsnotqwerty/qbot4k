import type { AppSettings } from "../core/config.ts";

export type LegalSettings = Pick<
  AppSettings,
  | "legalOrganizationName"
  | "legalContactEmail"
  | "legalJurisdiction"
  | "legalEffectiveDate"
>;

const escapeHtml = (value: string): string =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

export function legalPage(
  kind: "privacy" | "terms",
  settings?: LegalSettings,
): Response {
  const organization = escapeHtml(
    settings?.legalOrganizationName || "QBot4K",
  );
  const contact = escapeHtml(
    settings?.legalContactEmail || "the service operator",
  );
  const jurisdiction = escapeHtml(
    settings?.legalJurisdiction || "the service operator's jurisdiction",
  );
  const effectiveDate = escapeHtml(
    settings?.legalEffectiveDate || "Not specified",
  );
  const privacy = kind === "privacy";
  const title = privacy ? "Privacy policy" : "Terms of service";
  const content = privacy
    ? `<p>This privacy policy describes how ${organization} ("we", "us") collects, uses, stores, and discloses information when communities ("tenants") deploy QBot4K and when operators and end users interact with the service.</p>
<h2>1. Scope and roles</h2>
<p>For community content and member data ingested from third-party platforms (e.g. Discord, Twitch), the community operator acts as the data controller and ${organization} acts as a data processor (or service provider) processing data solely on the operator's documented instructions. For operator accounts and service telemetry, ${organization} is the controller.</p>
<h2>2. Data we process</h2>
<ul>
<li><strong>Operator account data</strong>: Discord identifiers, usernames, OAuth tokens, community role assignments, and invitation records.</li>
<li><strong>Community content</strong>: messages, member metadata, moderation events, and engagement signals from installed platforms, ingested only within installations authorized by the community.</li>
<li><strong>Moderation and audit records</strong>: sanctions, reversals, evidence bundles, operator actions, and immutable audit logs.</li>
<li><strong>Service telemetry</strong>: health checks, error logs, and performance metrics used to operate and secure the service.</li>
</ul>
<h2>3. Tenant isolation</h2>
<p>Community data is logically isolated per tenant. Access is restricted to operators holding an active, scoped role for that tenant and to audited service processes. Cross-tenant access is denied by default, recorded when attempted, and reviewable in the audit workspace.</p>
<h2>4. Legal bases (EEA/UK)</h2>
<p>Where the GDPR or UK GDPR applies, we rely on: performance of a contract (providing the service to operators), legitimate interests (security, abuse prevention, service improvement, balanced against member rights), and legal obligations (retention required by law). Community operators are responsible for establishing their own lawful basis for member data processed through their installation.</p>
<h2>5. Retention</h2>
<p>Retention follows each community's configured policy, subject to legal holds and statutory obligations. Audit and moderation evidence is retained for the period configured by the tenant; deletion requests propagate to replicas and backups on the next rotation cycle, at the latest within 30 days.</p>
<h2>6. Sub-processors and transfers</h2>
<p>Data is shared only with the infrastructure and platform providers required to deliver the service, under data processing terms. Where data is transferred internationally, we rely on adequacy decisions or standard contractual clauses as the transfer mechanism.</p>
<h2>7. Security</h2>
<p>We apply encryption in transit, scoped credentials, least-privilege access, origin-checked mutations, and continuous health monitoring. No method of transmission or storage is perfectly secure; material incidents affecting your data will be notified to affected operators without undue delay.</p>
<h2>8. Your rights</h2>
<p>Depending on your jurisdiction you may have rights of access, rectification, erasure, restriction, portability, and objection. End users should direct requests to their community operator first; operators may contact ${contact}. We respond to verified requests within the period required by applicable law.</p>
<h2>9. Children</h2>
<p>The service is an operations tool for community staff and is not directed at children under 13 (or the minimum digital-consent age in the applicable jurisdiction). We do not knowingly collect operator account data from children.</p>
<h2>10. Changes and contact</h2>
<p>We may update this policy; material changes are announced through the service before taking effect. Questions and requests: ${contact}.</p>`
    : `<p>These terms of service ("Terms") govern access to and use of QBot4K, operated by ${organization} ("we", "us"). By linking an account, installing the bot, or using the dashboard, you agree to these Terms.</p>
<h2>1. The service</h2>
<p>QBot4K provides tenant-isolated community operations tooling: moderation command, evidence capture, announcements, live operations, and operational intelligence for communities on third-party platforms. Features may evolve; we do not guarantee any particular feature will be retained.</p>
<h2>2. Eligibility and accounts</h2>
<p>You must be authorized to administer the community you connect, and your platform account must be in good standing. You are responsible for safeguarding credentials and for all actions taken under your operator role. Installation tokens are scoped to the community and must not be shared across tenants.</p>
<h2>3. Operator responsibility</h2>
<p>Operators remain solely responsible for: moderation decisions and their consequences; securing and honoring provider (Discord, Twitch) permissions and platform rules; the lawfulness of data ingested by their installations, including notice and consent obligations toward members; and compliance with applicable laws in the jurisdictions where they operate. QBot4K records evidence and enforces scoped authorization, but does not make moderation decisions on your behalf unless you configure it to.</p>
<h2>4. Acceptable use</h2>
<p>You must not use the service to: surveil or profile individuals beyond legitimate community safety purposes; violate platform terms or rate limits; attempt cross-tenant access or probe tenant isolation; reverse engineer except as permitted by law; or process data you lack the right to process. We may suspend access for material violations, with notice where practicable.</p>
<h2>5. Third-party platforms</h2>
<p>The service depends on third-party platforms and is not affiliated with or endorsed by them. Platform outages, API changes, or account enforcement actions may affect the service; we are not liable for third-party conduct. Your use of those platforms remains governed by their own terms.</p>
<h2>6. Service levels and changes</h2>
<p>We target high availability (see the status page) but the service is provided "as is" and "as available" unless a separate written service-level agreement applies to your deployment. We may modify, suspend, or discontinue features with reasonable notice for material changes.</p>
<h2>7. Data and privacy</h2>
<p>Processing of personal data is governed by the privacy policy, which is incorporated by reference. As between you and us, you retain all rights in your community's data; you grant us only the rights needed to operate the service on your instructions.</p>
<h2>8. Disclaimers and limitation of liability</h2>
<p>To the maximum extent permitted by law, we disclaim all implied warranties, including merchantability, fitness for a particular purpose, and non-infringement. We are not liable for indirect, incidental, consequential, or punitive damages, nor for lost data, profits, or goodwill. Our aggregate liability arising out of these Terms is limited to the amounts you paid us for the service in the twelve months preceding the claim (or, if none, one hundred US dollars). Nothing in these Terms limits liability that cannot be limited by law, including liability for willful misconduct or, where applicable, under mandatory data protection law.</p>
<h2>9. Indemnity</h2>
<p>You will indemnify and hold us harmless from claims arising out of your moderation decisions, your community's content, or your breach of these Terms or of third-party platform rules.</p>
<h2>10. Termination</h2>
<p>You may stop using the service and request data deletion at any time. We may terminate or suspend access for material breach, legal risk, or non-payment where applicable. Upon termination we return or delete tenant data in accordance with the privacy policy, subject to legal retention duties.</p>
<h2>11. Governing law and disputes</h2>
<p>These Terms are governed by the laws of ${jurisdiction}, excluding conflict-of-law rules. Disputes will be resolved in the competent courts of ${jurisdiction}, unless mandatory consumer or data protection law grants you another forum. The UN Convention on Contracts for the International Sale of Goods does not apply.</p>
<h2>12. General</h2>
<p>If any provision is unenforceable, the remainder stays in effect. Failure to enforce a provision is not a waiver. You may not assign these Terms without our consent; we may assign them in connection with a reorganization or sale of the service. These Terms, together with the privacy policy, are the entire agreement. Questions: ${contact}.</p>`;
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${title} | QBot4K</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/">Home</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav></header><main class="page-content"><article class="legal-page"><p class="eyebrow">Legal</p><h1>${title}</h1><p class="lede">Effective ${effectiveDate}</p>${content}</article></main><footer><span>${organization}</span><a href="/">Home</a></footer></div></body></html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

interface StatusServiceMeta {
  readonly role: string;
  readonly name: string;
  readonly description: string;
}

const STATUS_WINDOW_DAYS = 90;

const statusServices: readonly StatusServiceMeta[] = [
  {
    role: "web",
    name: "Web dashboard",
    description: "Operator workspaces and session authentication",
  },
  {
    role: "discord",
    name: "Discord ingestion",
    description: "Gateway events, commands, and provider confirmations",
  },
  {
    role: "twitch",
    name: "Twitch live ops",
    description: "EventSub ingestion and stream-state automation",
  },
  {
    role: "analysis",
    name: "Moderation analysis",
    description: "Signals, content analysis, and intelligence alerts",
  },
  {
    role: "jobs",
    name: "Background jobs",
    description: "Maintenance, retention, backups, and dispatch workers",
  },
];

interface DaySummary {
  date: string;
  observed: number;
  up: number;
  degraded: number;
}

/** Collapse per-minute buckets (possibly from multiple observers) into days. */
function summarizeDays(
  buckets: readonly import("../core/health.ts").ReliabilityBucket[],
  now: Date,
): DaySummary[] {
  const byMinute = new Map<
    string,
    { up: number; total: number; degraded: number }
  >();
  for (const b of buckets) {
    const key = b.bucketStart.slice(0, 16);
    const entry = byMinute.get(key) ?? { up: 0, total: 0, degraded: 0 };
    entry.total += 1;
    if (b.isUp) entry.up += 1;
    if (b.status === "degraded") entry.degraded += 1;
    byMinute.set(key, entry);
  }
  const byDay = new Map<string, DaySummary & { minutes: number }>();
  for (const [minute, entry] of byMinute) {
    const day = minute.slice(0, 10);
    const d = byDay.get(day) ?? {
      date: day,
      observed: 0,
      up: 0,
      degraded: 0,
      minutes: 0,
    };
    d.minutes += 1;
    d.observed += entry.total;
    d.up += entry.up;
    d.degraded += entry.degraded;
    byDay.set(day, d);
  }
  const days: DaySummary[] = [];
  for (let i = STATUS_WINDOW_DAYS - 1; i >= 0; i--) {
    const date = new Date(now.valueOf() - i * 86_400_000)
      .toISOString().slice(0, 10);
    days.push(
      byDay.get(date) ?? { date, observed: 0, up: 0, degraded: 0 },
    );
  }
  return days;
}

interface DowntimeEvent {
  readonly service: string;
  readonly start: Date;
  readonly minutes: number;
  readonly status: string;
  readonly ongoing: boolean;
}

/** Group consecutive down minutes (any observer reported down) into events. */
function downtimeEvents(
  service: string,
  buckets: readonly import("../core/health.ts").ReliabilityBucket[],
  now: Date,
): DowntimeEvent[] {
  const downMinutes = new Map<number, string>();
  for (const b of buckets) {
    if (b.isUp) continue;
    const t = new Date(b.bucketStart).valueOf();
    // Keep the most severe-looking status for the note.
    if (b.status === "down" || !downMinutes.has(t)) {
      downMinutes.set(t, b.status);
    }
  }
  const times = [...downMinutes.keys()].sort((a, b) => a - b);
  const events: DowntimeEvent[] = [];
  let start = -1;
  let prev = -1;
  let worst = "down";
  for (const t of times) {
    if (start < 0 || t - prev > 60_000) {
      if (start >= 0) {
        events.push({
          service,
          start: new Date(start),
          minutes: Math.round((prev - start) / 60_000) + 1,
          status: worst,
          ongoing: false,
        });
      }
      start = t;
      worst = downMinutes.get(t) ?? "down";
    } else if (downMinutes.get(t) === "down") {
      worst = "down";
    }
    prev = t;
  }
  if (start >= 0) {
    events.push({
      service,
      start: new Date(start),
      minutes: Math.round((prev - start) / 60_000) + 1,
      status: worst,
      ongoing: now.valueOf() - prev < 5 * 60_000,
    });
  }
  return events;
}

/** Convert an uptime percentage into an approximate sigma reliability level. */
function sigmaLevel(uptime: number): { label: string; cls: string } {
  // sigma levels map to one-sided defect rates: 3σ≈93.32%, 4σ≈99.379%,
  // 5σ≈99.9767%, 6σ≈99.99966% availability.
  if (uptime >= 99.99966) return { label: "6σ", cls: "sigma-6" };
  if (uptime >= 99.9767) return { label: "5σ", cls: "sigma-5" };
  if (uptime >= 99.379) return { label: "4σ", cls: "sigma-4" };
  if (uptime >= 93.32) return { label: "3σ", cls: "sigma-3" };
  return { label: "<3σ", cls: "sigma-low" };
}

const dayLabel = (date: string, d: DaySummary): string => {
  if (d.observed === 0) return `${date}: no data`;
  const pct = ((d.up / d.observed) * 100).toFixed(1);
  return `${date}: ${pct}% available (${d.observed} observations)`;
};

export async function statusPage(
  store?: import("../core/health.ts").StatusStore,
): Promise<Response> {
  const now = new Date();
  const generated = now.toISOString().replace("T", " ").slice(0, 16) + " UTC";
  const since = new Date(now.valueOf() - STATUS_WINDOW_DAYS * 86_400_000)
    .toISOString();
  const current: Record<string, { status: string; updatedAt: string }> = store
    ? await store.roleHeartbeats().catch(() => ({}))
    : {};

  const allEvents: DowntimeEvent[] = [];
  const rows = (await Promise.all(statusServices.map(async (meta) => {
    const buckets = store
      ? await store.buckets(meta.role, since).catch(() => [])
      : [];
    const days = summarizeDays(buckets, now);
    let observed = 0;
    let up = 0;
    const cells = days.map((d) => {
      observed += d.observed;
      up += d.up;
      const cls = d.observed === 0
        ? "nodata"
        : d.up === 0
        ? "down"
        : d.up < d.observed
        ? "degraded"
        : d.degraded > 0
        ? "degraded"
        : "up";
      const label = dayLabel(d.date, d);
      return `<span class="strip-cell ${cls}" title="${label}" aria-label="${label}"></span>`;
    }).join("");
    const uptime = observed > 0 ? (up / observed) * 100 : null;
    const sigma = uptime === null
      ? { label: "no data", cls: "sigma-low" }
      : sigmaLevel(uptime);
    allEvents.push(...downtimeEvents(meta.name, buckets, now));

    const heartbeat = current[meta.role];
    const ageSeconds = heartbeat
      ? (now.valueOf() - new Date(heartbeat.updatedAt).valueOf()) / 1000
      : Infinity;
    const live = heartbeat && ageSeconds <= 120;
    const liveStatus = live ? heartbeat.status : heartbeat ? "down" : "unknown";
    const liveCls = liveStatus === "ready"
      ? "chip chip-ok"
      : liveStatus === "degraded" || liveStatus === "unknown"
      ? "chip chip-warn"
      : "chip chip-danger";

    return `<article class="status-service">
<header class="status-service-head">
<div><h2>${meta.name}</h2><p>${meta.description}</p></div>
<span class="${liveCls}">${liveStatus}</span>
</header>
<div class="uptime-strip" role="img" aria-label="${STATUS_WINDOW_DAYS}-day availability for ${meta.name}">${cells}</div>
<footer class="status-service-foot">
<span class="num">${
      uptime === null
        ? `no observations yet · ${STATUS_WINDOW_DAYS} days`
        : `${
          uptime.toFixed(3)
        }% uptime · ${observed.toLocaleString()} observations · ${STATUS_WINDOW_DAYS} days`
    }</span>
<span class="sigma-badge ${sigma.cls}" title="Reliability level derived from recorded availability over the rolling window">${sigma.label} reliability</span>
</footer>
</article>`;
  }))).join("\n");

  allEvents.sort((a, b) => b.start.valueOf() - a.start.valueOf());
  const events = allEvents.slice(0, 25).map((e) => {
    const ago = Math.max(
      1,
      Math.round((now.valueOf() - e.start.valueOf()) / 60_000),
    );
    const when = ago >= 1440
      ? `${Math.round(ago / 1440)}d ago`
      : ago >= 60
      ? `${Math.round(ago / 60)}h ago`
      : `${ago}m ago`;
    return `<li class="downtime-event">
<div class="downtime-when"><span class="num">${e.minutes}m</span><span>${when}</span></div>
<div><strong>${e.service}</strong><p>Recorded status: ${e.status} · started ${
      e.start.toISOString().replace("T", " ").slice(0, 16)
    } UTC</p></div>
<span class="chip ${e.ongoing ? "chip-danger" : "chip-ok"}">${
      e.ongoing ? "Ongoing" : "Resolved"
    }</span>
</li>`;
  }).join("\n");

  const eventsHtml = allEvents.length === 0
    ? `<p class="status-note">No downtime recorded in the window. Events appear here when an observer records a service as down or stale.</p>`
    : `<ul class="downtime-list">${events}</ul>`;

  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Status | QBot4K</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/">Home</a><a href="/status" aria-current="page">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav></header><main class="page-content"><div class="data-heading"><div><p class="eyebrow">Service status</p><h1>Status</h1><p class="lede">Recorded per-minute availability from every running service role, rolled up over ${STATUS_WINDOW_DAYS} days. Generated ${generated}. Live probes: <a class="text-link" href="/health/ready">/health/ready</a></p></div></div>
<section class="status-grid" aria-label="Services">${rows}</section>
<section aria-labelledby="downtime-title"><h2 id="downtime-title">Downtime events</h2>${eventsHtml}</section>
<p class="status-note">Each minute, every service role records its own status and its view of peers (via heartbeats) into service_reliability_buckets. A minute with no observation at all means no role was alive to record it and counts as unavailable. Sigma reliability levels: 3σ ≈ 93.32%, 4σ ≈ 99.38%, 5σ ≈ 99.977%, 6σ ≈ 99.9997% availability over the rolling window.</p>
</main><footer><span>QBot4K</span><a href="/">Home</a></footer></div></body></html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}
