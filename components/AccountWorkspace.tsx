export function AccountWorkspace({
  username,
  enabled,
  error = "",
  name = "",
  slug = "",
}: {
  readonly username: string;
  readonly enabled: boolean;
  readonly error?: string;
  readonly name?: string;
  readonly slug?: string;
}) {
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/">QBot4K</a>
        <form method="post" action="/logout">
          <button type="submit" class="button-secondary">Sign out</button>
        </form>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">{username}</p>
            <h1>{enabled ? "Create organization" : "Account access"}</h1>
          </div>
        </section>
        {error ? <p class="status-banner" role="alert">{error}</p> : null}
        {enabled
          ? (
            <form
              method="post"
              action="/account/organizations"
              class="settings-form"
            >
              <label>
                Organization name
                <input
                  name="name"
                  required
                  maxLength={120}
                  value={name}
                  autoComplete="organization"
                />
              </label>
              <label>
                Organization slug
                <input
                  name="slug"
                  required
                  maxLength={80}
                  pattern="[a-z0-9]+(-[a-z0-9]+)*"
                  aria-describedby="organization-slug-help"
                  title="Use lowercase letters and numbers, separated by single hyphens. No spaces or leading or trailing hyphens."
                  value={slug}
                  autoCapitalize="none"
                  spellcheck={false}
                />
              </label>
              <small id="organization-slug-help">
                Choose a unique slug, 1-80 characters long. Use lowercase
                letters (a-z) and numbers (0-9), separated by single hyphens. No
                spaces, underscores, or leading or trailing hyphens. Examples:
                my-team, team42, acme-2026.
              </small>
              <div class="form-actions">
                <button type="submit">Create organization</button>
              </div>
            </form>
          )
          : (
            <p class="status-banner">
              Organization signup is closed. A community invitation is required.
            </p>
          )}
        <footer class="form-actions">
          <a href="/terms">Terms</a>
          <a href="/privacy">Privacy</a>
        </footer>
      </main>
    </div>
  );
}
