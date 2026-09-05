import type { DatabaseRow } from "@/src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";
import { EmptyState } from "./ui.tsx";

export function CommandsWorkspace(
  { builtins, simple, status }: {
    readonly builtins: readonly DatabaseRow[];
    readonly simple: readonly DatabaseRow[];
    readonly status: string;
  },
) {
  const commands = [...builtins, ...simple];
  return (
    <div class="app-shell">
      <DashboardHeader active="/commands" />
      <main class="page-content">
        <section class="data-heading command-heading">
          <div>
            <p class="eyebrow">Command operations</p>
            <h1>Command menu</h1>
            <p class="lede">
              Manage built-in responses and tenant-specific plaintext commands
              across Discord and Twitch.
            </p>
          </div>
          <dl class="command-stats" aria-label="Command inventory">
            <div>
              <dt>Total</dt>
              <dd>{commands.length}</dd>
            </div>
            <div>
              <dt>Enabled</dt>
              <dd>
                {commands.filter((command) => Boolean(command.enabled)).length}
              </dd>
            </div>
            <div>
              <dt>Runtime</dt>
              <dd>Both providers</dd>
            </div>
          </dl>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}

        {builtins.length > 0 && (
          <section class="command-panel">
            <div class="panel-heading">
              <div>
                <p class="section-label">System commands</p>
                <h2>Built-ins</h2>
              </div>
              <p>
                Edit title, body, and footer templates. Reserved operational
                commands remain available in chat for moderators.
              </p>
            </div>
            <div class="table-wrap">
              <table class="command-table">
                <thead>
                  <tr>
                    <th scope="col">Command</th>
                    <th scope="col">Title</th>
                    <th scope="col">Description template</th>
                    <th scope="col">Footer template</th>
                    <th scope="col">Enabled</th>
                    <th scope="col"></th>
                  </tr>
                </thead>
                <tbody>
                  {builtins.map((command) => (
                    <tr key={String(command.command_name)}>
                      <th scope="row" class="command-name">
                        !{String(command.command_name)}
                      </th>
                      <td colSpan={5}>
                        <form
                          method="post"
                          action="/commands"
                          class="command-row"
                        >
                          <input
                            type="hidden"
                            name="record_type"
                            value="builtin"
                          />
                          <input
                            type="hidden"
                            name="command_name"
                            value={String(command.command_name)}
                          />
                          <input
                            name="title"
                            value={String(command.title)}
                            required
                            aria-label="Title"
                          />
                          <input
                            name="description_template"
                            value={String(command.description_template)}
                            required
                            aria-label="Description template"
                          />
                          <input
                            name="footer_template"
                            value={String(command.footer_template ?? "")}
                            aria-label="Footer template"
                          />
                          <label class="toggle">
                            <input
                              type="checkbox"
                              name="enabled"
                              value="1"
                              checked={Boolean(command.enabled)}
                            />
                            <span class="sr-only">Enabled</span>
                          </label>
                          <button type="submit">Save</button>
                        </form>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        <section class="command-panel">
          <div class="panel-heading">
            <div>
              <p class="section-label">Tenant commands</p>
              <h2>Plaintext commands</h2>
            </div>
            <p>
              Create simple templated responses. Use {"${query}"}{" "}
              for caller input and range macros such as {"${1..6}"}.
            </p>
          </div>

          <form method="post" action="/commands" class="command-new">
            <input type="hidden" name="record_type" value="simple" />
            <input
              name="command_name"
              required
              pattern="[A-Za-z0-9_\-]+"
              placeholder="name"
              aria-label="Command name"
            />
            <input
              name="response_template"
              required
              placeholder="Response — supports ${query}, ${1..6}, ${GET}(url)[k:path]"
              aria-label="Plain text response"
            />
            <label class="toggle">
              <input type="checkbox" name="enabled" value="1" checked />
              <span>Enabled</span>
            </label>
            <button type="submit">Create</button>
          </form>

          {simple.length === 0
            ? (
              <EmptyState
                title="No custom commands yet"
                hint="Create a plaintext response command above. It becomes available in chat immediately on every linked platform."
                columns={["Command", "Response", "Actions"]}
              />
            )
            : (
              <div class="table-wrap">
                <table class="command-table">
                  <thead>
                    <tr>
                      <th scope="col">Command</th>
                      <th scope="col">Response template</th>
                      <th scope="col">Enabled</th>
                      <th scope="col"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {simple.map((command) => (
                      <tr key={String(command.command_name)}>
                        <th scope="row" class="command-name">
                          !{String(command.command_name)}
                        </th>
                        <td colSpan={3}>
                          <div class="command-row">
                            <form method="post" action="/commands">
                              <input
                                type="hidden"
                                name="record_type"
                                value="simple"
                              />
                              <input
                                type="hidden"
                                name="command_name"
                                value={String(command.command_name)}
                              />
                              <input
                                name="response_template"
                                value={String(command.response_template)}
                                required
                                aria-label="Response template"
                              />
                              <label class="toggle">
                                <input
                                  type="checkbox"
                                  name="enabled"
                                  value="1"
                                  checked={Boolean(command.enabled)}
                                />
                                <span class="sr-only">Enabled</span>
                              </label>
                              <button type="submit">Save</button>
                            </form>
                            <form
                              method="post"
                              action="/commands"
                              class="danger-action"
                            >
                              <input
                                type="hidden"
                                name="record_type"
                                value="simple"
                              />
                              <input
                                type="hidden"
                                name="action"
                                value="delete"
                              />
                              <input
                                type="hidden"
                                name="command_name"
                                value={String(command.command_name)}
                              />
                              <button
                                type="submit"
                                aria-label={`Delete ${
                                  String(command.command_name)
                                }`}
                              >
                                Delete
                              </button>
                            </form>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>
      </main>
    </div>
  );
}
