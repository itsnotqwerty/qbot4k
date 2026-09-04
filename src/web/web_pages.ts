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
    ? `<p>${organization} processes community, account, and moderation data only to operate QBot4K.</p><h2>Data handling</h2><p>Community data is tenant-isolated. Access is limited to authorized operators and audited service processes.</p><h2>Retention and requests</h2><p>Retention follows community policy and legal obligations. Contact ${contact} for access, correction, or deletion requests.</p>`
    : `<p>By using QBot4K, operators agree to use the service only for authorized community operations.</p><h2>Operator responsibility</h2><p>Operators remain responsible for provider permissions, moderation decisions, and compliance with applicable platform rules.</p><h2>Governing terms</h2><p>These terms are governed by ${jurisdiction}. Questions may be sent to ${contact}.</p>`;
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${title} | QBot4K</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav></header><main class="page-content"><article class="legal-page"><p class="eyebrow">Legal</p><h1>${title}</h1><p class="lede">Effective ${effectiveDate}</p>${content}</article></main><footer><span>${organization}</span><a href="/">Home</a></footer></div></body></html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}
