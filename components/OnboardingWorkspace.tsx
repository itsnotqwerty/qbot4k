import type { OnboardingSnapshot } from "@/src/web/web_onboarding.ts";

export function OnboardingWorkspace(
  { installations, settings, members, resources, canManage, status }:
    & OnboardingSnapshot
    & { readonly canManage: boolean; readonly status: string },
) {
  const value = (name: string, fallback = "") =>
    String(settings?.[name] ?? fallback);
  const checked = (name: string) => Boolean(settings?.[name]);
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav>
          <a href="/dashboard">Overview</a>
          <a href="/onboarding" aria-current="page">Onboarding</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Community onboarding</p>
            <h1>Welcome automation</h1>
            <p class="lede">
              Configure welcome messages, resources, and verification
              checkpoints.
            </p>
          </div>
        </section>
        {status ? <p class="status-banner">{status}</p> : null}
        {canManage && installations.length
          ? (
            <form method="post" action="/onboarding">
              <h2>Configuration</h2>
              <select name="discord_installation_id" required>
                {installations.map((item) => (
                  <option
                    value={String(item.id)}
                    selected={item.id === settings?.discord_installation_id}
                  >
                    {String(item.display_name)}
                  </option>
                ))}
              </select>
              <input
                name="welcome_channel_id"
                value={value("welcome_channel_id")}
                required
                placeholder="Welcome channel ID"
              />
              <textarea name="welcome_template" required maxLength={2000}>
                {value(
                  "welcome_template",
                  "Welcome {mention} to the community!",
                )}
              </textarea>
              <label>
                <input
                  type="checkbox"
                  name="enabled"
                  value="1"
                  checked={checked("welcome_enabled")}
                />{" "}
                Enabled
              </label>
              <input
                name="newcomer_role_id"
                value={value("newcomer_role_id")}
                placeholder="Newcomer role ID"
              />
              <label>
                <input
                  type="checkbox"
                  name="newcomer_role_enabled"
                  value="1"
                  checked={checked("newcomer_role_enabled")}
                />{" "}
                Assign newcomer role
              </label>
              <input
                type="number"
                name="checkpoint_due_hours"
                min="1"
                max="720"
                value={value("checkpoint_due_hours", "24")}
                required
              />
              <textarea name="checkpoint_reminder_template" required>
                {value(
                  "checkpoint_reminder_template",
                  "Reminder {mention}: please complete community verification.",
                )}
              </textarea>
              <label>
                <input
                  type="checkbox"
                  name="checkpoint_reminder_enabled"
                  value="1"
                  checked={checked("checkpoint_reminder_enabled")}
                />{" "}
                Send overdue reminder
              </label>
              <input
                type="url"
                name="verification_resource_url"
                value={value("verification_resource_url")}
                placeholder="Resource URL"
              />
              <textarea name="verification_resource_template" required>
                {value(
                  "verification_resource_template",
                  "You are verified, {mention}. Community resources: {resource_url}",
                )}
              </textarea>
              <label>
                <input
                  type="checkbox"
                  name="verification_resource_enabled"
                  value="1"
                  checked={checked("verification_resource_enabled")}
                />{" "}
                Send resources after verification
              </label>
              <label>
                <input
                  type="checkbox"
                  name="verification_evidence_required"
                  value="1"
                  checked={checked("verification_evidence_required")}
                />{" "}
                Require verification evidence
              </label>
              <label>
                <input
                  type="checkbox"
                  name="self_service_verification_enabled"
                  value="1"
                  checked={checked("self_service_verification_enabled")}
                />{" "}
                Allow self-service verification
              </label>
              <button type="submit">Save automation</button>
            </form>
          )
          : null}
        {canManage
          ? (
            <section>
              <h2>Resource catalog</h2>
              <form method="post" action="/onboarding/resources">
                <input
                  name="title"
                  required
                  maxLength={120}
                  placeholder="Resource title"
                />
                <input
                  type="url"
                  name="resource_url"
                  required
                  placeholder="https://example.com/resource"
                />
                <input
                  name="message_template"
                  value="{mention}: {title} - {resource_url}"
                  required
                  maxLength={2000}
                />
                <input
                  type="number"
                  name="sort_order"
                  value="0"
                  min="-1000"
                  max="1000"
                />
                <label>
                  <input type="checkbox" name="enabled" value="1" checked />
                  {" "}
                  Enabled
                </label>
                <button type="submit">Add resource</button>
              </form>
              {resources.map((resource) => (
                <form method="post" action="/onboarding/resources">
                  <input
                    type="hidden"
                    name="resource_id"
                    value={String(resource.id)}
                  />
                  <input name="title" value={String(resource.title)} required />
                  <input
                    type="url"
                    name="resource_url"
                    value={String(resource.resource_url)}
                    required
                  />
                  <input
                    name="message_template"
                    value={String(resource.message_template)}
                    required
                  />
                  <input
                    type="number"
                    name="sort_order"
                    value={String(resource.sort_order)}
                  />
                  <button type="submit">Save</button>
                  <button
                    type="submit"
                    formAction={`/onboarding/resources/${resource.id}/delete`}
                  >
                    Delete
                  </button>
                </form>
              ))}
            </section>
          )
          : null}
        <section>
          <h2>Verification checkpoints</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Status</th>
                  <th>Role</th>
                  <th>Due</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <tr>
                    <td>{String(member.username)}</td>
                    <td>{String(member.status)}</td>
                    <td>{String(member.role_assignment_status)}</td>
                    <td>{String(member.checkpoint_due_at ?? "")}</td>
                    <td>
                      {canManage && member.status === "newcomer"
                        ? (
                          <form method="post" action="/onboarding/verify">
                            <input
                              type="hidden"
                              name="platform_user_id"
                              value={String(member.platform_user_id)}
                            />
                            {checked("verification_evidence_required")
                              ? (
                                <input
                                  name="verification_evidence"
                                  required
                                  maxLength={2000}
                                />
                              )
                              : null}
                            <button type="submit">Verify</button>
                          </form>
                        )
                        : String(
                          member.verification_evidence ?? member.verified_at ??
                            "",
                        )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  );
}
