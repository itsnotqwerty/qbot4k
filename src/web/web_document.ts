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
