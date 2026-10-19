function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

export function dashboardDocument(
  html: string,
  title = "QBot4K dashboard",
): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${
    escapeHtml(title)
  }</title><link rel="stylesheet" href="/styles.css"><script>${THEME_BOOTSTRAP}</script></head><body>${html}</body></html>`;
}

const THEME_BOOTSTRAP = `
(() => {
  const root = document.documentElement;
  const swatches = ["sky", "mist", "mint", "lavender", "peach", "rose", "sand", "slate", "sage"];
  const mode = localStorage.getItem("qbot4k-theme-mode") || "dark";
  const accent = localStorage.getItem("qbot4k-theme-accent") || "sky";
  root.dataset.themeMode = mode === "light" ? "light" : "dark";
  root.dataset.themeAccent = swatches.includes(accent) ? accent : "sky";
  document.addEventListener("DOMContentLoaded", () => {
    const form = document.querySelector("[data-community-switcher]");
    if (!form) return;
    const select = form.querySelector("select");
    const submit = form.querySelector('[type="submit"]');
    const retry = form.querySelector("[data-community-retry]");
    const status = form.querySelector('[role="status"]');
    let activeCommunity = "";
    const load = async () => {
      retry.hidden = true;
      status.textContent = "";
      select.disabled = true;
      submit.disabled = true;
      try {
        const response = await fetch("/account/communities", {
          credentials: "same-origin", cache: "no-store"
        });
        if (!response.ok) throw new Error("Community lookup failed");
        const data = await response.json();
        activeCommunity = String(data.communityId ?? "");
        select.replaceChildren();
        if (data.communityId === null || data.memberships.length === 0) {
          select.add(new Option(data.memberships.length ? "Select a community" : "No communities available", ""));
        }
        for (const membership of data.memberships) {
          select.add(new Option(membership.name, String(membership.id)));
        }
        select.value = activeCommunity;
        select.disabled = data.memberships.length === 0;
      } catch {
        status.textContent = "Communities unavailable.";
        retry.hidden = false;
      }
    };
    select.addEventListener("change", () => {
      submit.disabled = !select.value || select.value === activeCommunity;
    });
    retry.addEventListener("click", load);
    form.addEventListener("submit", () => { submit.disabled = true; });
    load();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme-accent],[data-theme-mode]");
    if (!button) return;
    if (button.dataset.themeAccent) {
      localStorage.setItem("qbot4k-theme-accent", button.dataset.themeAccent);
      root.dataset.themeAccent = button.dataset.themeAccent;
    }
    if (button.dataset.themeMode) {
      localStorage.setItem("qbot4k-theme-mode", button.dataset.themeMode);
      root.dataset.themeMode = button.dataset.themeMode;
    }
  });
})();
`;
