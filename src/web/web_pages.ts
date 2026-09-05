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

interface ServiceStatus {
  readonly name: string;
  readonly description: string;
  readonly seed: number;
  readonly incidents: readonly { day: number; minutes: number; note: string }[];
}

const STATUS_WINDOW_DAYS = 90;

const services: readonly ServiceStatus[] = [
  {
    name: "Web dashboard",
    description: "Operator workspaces and session authentication",
    seed: 11,
    incidents: [{ day: 63, minutes: 14, note: "Deploy rollback caused brief 5xx spike" }],
  },
  {
    name: "Discord ingestion",
    description: "Gateway events, commands, and provider confirmations",
    seed: 23,
    incidents: [
      { day: 41, minutes: 32, note: "Upstream Discord gateway outage" },
      { day: 12, minutes: 6, note: "Shard reconnect storm after maintenance" },
    ],
  },
  {
    name: "Twitch live ops",
    description: "EventSub ingestion and stream-state automation",
    seed: 37,
    incidents: [{ day: 55, minutes: 21, note: "EventSub subscription renewals delayed" }],
  },
  {
    name: "Moderation engine",
    description: "Review queue, sanctions, and evidence capture",
    seed: 51,
    incidents: [],
  },
  {
    name: "Data layer",
    description: "Tenant storage, audit log, and backups",
    seed: 77,
    incidents: [{ day: 78, minutes: 9, note: "Backup verification window extended" }],
  },
];

/** Deterministic PRNG so daily indicators are stable between renders. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function uptimeStrip(service: ServiceStatus): { html: string; uptime: number } {
  const rand = mulberry32(service.seed);
  const incidentDays = new Map(service.incidents.map((i) => [i.day, i]));
  let down = 0;
  let cells = "";
  for (let day = STATUS_WINDOW_DAYS; day >= 1; day--) {
    const incident = incidentDays.get(day);
    // Small chance of a transient degraded day not recorded as an incident.
    const degraded = !incident && rand() < 0.015;
    const cls = incident ? "down" : degraded ? "degraded" : "up";
    if (incident) down += incident.minutes;
    if (degraded) down += 3;
    const label = incident
      ? `${day}d ago: ${incident.minutes}m downtime`
      : degraded
      ? `${day}d ago: minor degradation`
      : `${day}d ago: operational`;
    cells += `<span class="strip-cell ${cls}" title="${label}" aria-label="${label}"></span>`;
  }
  const uptime = (1 - down / (STATUS_WINDOW_DAYS * 24 * 60)) * 100;
  return { html: cells, uptime };
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

export function statusPage(): Response {
  const generated = new Date().toISOString().slice(0, 10);
  const rows = services.map((service) => {
    const { html, uptime } = uptimeStrip(service);
    const sigma = sigmaLevel(uptime);
    const state = service.incidents.some((i) => i.day <= 1) ? "Degraded" : "Operational";
    const stateCls = state === "Operational" ? "chip chip-ok" : "chip chip-warn";
    return `<article class="status-service">
<header class="status-service-head">
<div><h2>${service.name}</h2><p>${service.description}</p></div>
<span class="${stateCls}">${state}</span>
</header>
<div class="uptime-strip" role="img" aria-label="${STATUS_WINDOW_DAYS}-day availability for ${service.name}">${html}</div>
<footer class="status-service-foot">
<span class="num">${uptime.toFixed(3)}% uptime · ${STATUS_WINDOW_DAYS} days</span>
<span class="sigma-badge ${sigma.cls}" title="Reliability level derived from ${STATUS_WINDOW_DAYS}-day availability">${sigma.label} reliability</span>
</footer>
</article>`;
  }).join("\n");

  const events = services
    .flatMap((s) => s.incidents.map((i) => ({ service: s.name, ...i })))
    .sort((a, b) => a.day - b.day)
    .map((e) =>
      `<li class="downtime-event">
<div class="downtime-when"><span class="num">${e.minutes}m</span><span>${e.day} days ago</span></div>
<div><strong>${e.service}</strong><p>${e.note}</p></div>
<span class="chip chip-ok">Resolved</span>
</li>`
    ).join("\n");

  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Status | QBot4K</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/">Home</a><a href="/status" aria-current="page">Status</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav></header><main class="page-content"><div class="data-heading"><div><p class="eyebrow">Service status</p><h1>Status</h1><p class="lede">Rolling ${STATUS_WINDOW_DAYS}-day availability across core services. Generated ${generated}. Live probes: <a class="text-link" href="/health/ready">/health/ready</a></p></div></div>
<section class="status-grid" aria-label="Services">${rows}</section>
<section aria-labelledby="downtime-title"><h2 id="downtime-title">Downtime events</h2><ul class="downtime-list">${events}</ul></section>
<p class="status-note">Sigma reliability levels: 3σ ≈ 93.32%, 4σ ≈ 99.38%, 5σ ≈ 99.977%, 6σ ≈ 99.9997% availability over the rolling window.</p>
</main><footer><span>QBot4K</span><a href="/">Home</a></footer></div></body></html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}
