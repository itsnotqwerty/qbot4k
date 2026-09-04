import type { ComponentChildren } from "preact";

interface AppShellProps {
  readonly children: ComponentChildren;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/" aria-label="QBot4K home">QBot4K</a>
        <nav aria-label="Primary navigation">
          <a href="/">Home</a>
          <a href="/health/ready">System status</a>
        </nav>
      </header>
      <main class="page-content">{children}</main>
      <footer>
        <span>Community operations</span>
        <nav aria-label="Legal and service links">
          <a href="/privacy">Privacy</a>
          <a href="/terms">Terms</a>
          <a href="/health/live">Service health</a>
        </nav>
      </footer>
    </div>
  );
}
