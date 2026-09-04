import type { DatabaseRow } from "@/src/data/database.ts";

export function CommandsWorkspace(
  { builtins, simple, status }: {
    readonly builtins: readonly DatabaseRow[];
    readonly simple: readonly DatabaseRow[];
    readonly status: string;
  },
) {
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/commands" aria-current="page">Commands</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Commands</p>
            <h1>Command menu</h1>
            <p class="lede">Keep Discord and Twitch command output in sync.</p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}
        <section>
          <h2>Built-ins</h2>
          {builtins.map((command) => (
            <form
              method="post"
              action="/commands"
              class="moderation-item"
              key={String(command.command_name)}
            >
              <input type="hidden" name="record_type" value="builtin" />
              <input
                type="hidden"
                name="command_name"
                value={String(command.command_name)}
              />
              <strong>!{String(command.command_name)}</strong>
              <input name="title" value={String(command.title)} required />
              <textarea name="description_template" required>
                {String(command.description_template)}
              </textarea>
              <textarea name="footer_template">
                {String(command.footer_template ?? "")}
              </textarea>
              <label>
                <input
                  type="checkbox"
                  name="enabled"
                  value="1"
                  checked={Boolean(command.enabled)}
                />{" "}
                Enabled
              </label>
              <button type="submit">Save</button>
            </form>
          ))}
        </section>
        <section>
          <h2>Plaintext commands</h2>
          <form method="post" action="/commands">
            <input type="hidden" name="record_type" value="simple" />
            <input name="command_name" required placeholder="Command name" />
            <input
              name="response_template"
              required
              placeholder="Plain text response"
            />
            <label>
              <input type="checkbox" name="enabled" value="1" checked /> Enabled
            </label>
            <button type="submit">New command</button>
          </form>
          {simple.map((command) => (
            <div class="moderation-item" key={String(command.command_name)}>
              <form method="post" action="/commands">
                <input type="hidden" name="record_type" value="simple" />
                <input
                  type="hidden"
                  name="command_name"
                  value={String(command.command_name)}
                />
                <strong>!{String(command.command_name)}</strong>
                <input
                  name="response_template"
                  value={String(command.response_template)}
                  required
                />
                <label>
                  <input
                    type="checkbox"
                    name="enabled"
                    value="1"
                    checked={Boolean(command.enabled)}
                  />{" "}
                  Enabled
                </label>
                <button type="submit">Save</button>
              </form>
              <form method="post" action="/commands">
                <input type="hidden" name="record_type" value="simple" />
                <input type="hidden" name="action" value="delete" />
                <input
                  type="hidden"
                  name="command_name"
                  value={String(command.command_name)}
                />
                <button type="submit">Delete</button>
              </form>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}
