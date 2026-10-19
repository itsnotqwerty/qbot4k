const primaryLinks = [
  ["Overview", "/dashboard"],
  ["Live operations", "/live-ops"],
  ["Moderation", "/moderation"],
  ["Commands", "/commands"],
  ["Integrations", "/integrations"],
] as const;

const workspaceLinks = [
  ["Intelligence", "/intelligence"],
  ["Users", "/users"],
  ["Search", "/search"],
  ["Signals", "/signals"],
  ["Analytics", "/analytics"],
  ["Announcements", "/announcements"],
  ["Audit", "/audit"],
  ["Settings", "/settings"],
  ["Billing", "/billing"],
] as const;

const accentSwatches = [
  ["Sky", "sky"],
  ["Mist", "mist"],
  ["Mint", "mint"],
  ["Lavender", "lavender"],
  ["Peach", "peach"],
  ["Rose", "rose"],
  ["Sand", "sand"],
  ["Slate", "slate"],
  ["Sage", "sage"],
] as const;

export function DashboardHeader(
  { active, platformAdmin = false }: {
    readonly active: string;
    readonly platformAdmin?: boolean;
  },
) {
  return (
    <header class="site-header dashboard-header">
      <a class="brand" href="/dashboard">QBot4K</a>
      <form
        class="community-switcher"
        method="post"
        action="/community/switch"
        data-community-switcher
      >
        <label for="header-community">Community</label>
        <select id="header-community" name="community_id" disabled required>
          <option value="">Loading communities...</option>
        </select>
        <button type="submit" disabled>Switch</button>
        <button type="button" data-community-retry hidden>Retry</button>
        <span class="community-switch-status" role="status" />
        <noscript>Enable JavaScript to select a community.</noscript>
      </form>
      <nav class="dashboard-nav" aria-label="Dashboard navigation">
        {primaryLinks.map(([label, href]) => (
          <a href={href} aria-current={active === href ? "page" : undefined}>
            {label}
          </a>
        ))}
        <details class="workspace-menu theme-menu">
          <summary>Theme</summary>
          <div>
            <p class="theme-label">Accent</p>
            <div class="theme-swatches">
              {accentSwatches.map(([label, value]) => (
                <button
                  type="button"
                  data-theme-accent={value}
                  class={`swatch swatch-${value}`}
                >
                  <span>{label}</span>
                </button>
              ))}
            </div>
            <p class="theme-label">Mode</p>
            <div class="theme-modes">
              <button type="button" data-theme-mode="dark">Dark</button>
              <button type="button" data-theme-mode="light">Light</button>
            </div>
          </div>
        </details>
        <details class="workspace-menu">
          <summary>More</summary>
          <div>
            {workspaceLinks.map(([label, href]) => (
              <a
                href={href}
                aria-current={active === href ? "page" : undefined}
              >
                {label}
              </a>
            ))}
            {platformAdmin && (
              <a
                href="/admin"
                aria-current={active === "/admin" ? "page" : undefined}
              >
                Platform admin
              </a>
            )}
          </div>
        </details>
      </nav>
    </header>
  );
}
