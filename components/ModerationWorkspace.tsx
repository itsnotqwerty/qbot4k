import type {
  ModerationSnapshot,
  ModerationWorkQuery,
  ModerationWorkResult,
} from "@/src/domain/moderation.ts";

export function ModerationWorkspace(
  { snapshot, work, query }: {
    readonly snapshot: ModerationSnapshot;
    readonly work: ModerationWorkResult;
    readonly query: ModerationWorkQuery;
  },
) {
  const filters = Object.fromEntries(
    Object.entries(query).filter(([key, value]) =>
      key !== "page" && value !== "" && value !== undefined
    ).map((
      [key, value],
    ) => [
      key === "startAt" ? "start_at" : key === "endAt" ? "end_at" : key,
      String(value),
    ]),
  );
  const pageHref = (page: number) =>
    `/moderation?${new URLSearchParams({ ...filters, page: String(page) })}`;
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav aria-label="Dashboard navigation">
          <a href="/dashboard">Overview</a>
          <a href="/moderation" aria-current="page">Moderation</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">Moderation</p>
            <h1>Review and policy operations</h1>
            <p class="lede">
              Adjudicate findings and track provider-confirmed outcomes.
            </p>
          </div>
        </section>
        <section>
          <h2>Work queue</h2>
          <nav class="row-actions" aria-label="Work queue views">
            {["unassigned", "mine", "escalated", "appeals", "resolved", "all"]
              .map((name) => (
                <a
                  href={`/moderation?queue=${name}`}
                  aria-current={query.queue === name ? "page" : undefined}
                >
                  {name}
                </a>
              ))}
          </nav>
          <form method="get" action="/moderation">
            <input
              type="hidden"
              name="queue"
              value={query.queue ?? "unassigned"}
            />
            <input
              name="search"
              value={query.search}
              placeholder="Search"
              aria-label="Search moderation work"
            />
            <input
              name="severity"
              value={query.severity}
              placeholder="Severity"
            />
            <input
              name="rule"
              value={query.rule}
              placeholder="Rule or reason"
            />
            <input
              name="platform"
              value={query.platform}
              placeholder="Platform"
            />
            <input
              type="datetime-local"
              name="start_at"
              value={query.startAt}
              aria-label="Created after"
            />
            <input
              type="datetime-local"
              name="end_at"
              value={query.endAt}
              aria-label="Created before"
            />
            <select name="assignment" aria-label="Assignment">
              <option value="">Any assignment</option>
              <option value="unassigned">Unassigned</option>
              <option value="mine">Mine</option>
            </select>
            <button type="submit">Apply filters</button>
          </form>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Work</th>
                  <th>Platform</th>
                  <th>Member</th>
                  <th>Severity</th>
                  <th>Reason</th>
                  <th>Summary</th>
                  <th>SLA age</th>
                  <th>Assignee</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {work.items.map((item) => (
                  <tr key={`${item.work_type}:${item.item_id}`}>
                    <td>{String(item.work_type)} #{String(item.item_id)}</td>
                    <td>{String(item.platform)}</td>
                    <td>{String(item.username)}</td>
                    <td>{String(item.severity)}</td>
                    <td>{String(item.reason)}</td>
                    <td>{String(item.summary)}</td>
                    <td>{Number(item.sla_age_hours).toFixed(1)}h</td>
                    <td>
                      {item.assigned_operator_id
                        ? String(item.assigned_operator_id)
                        : "Unassigned"}
                    </td>
                    <td>{String(item.status)}</td>
                    <td>
                      {item.status === "open" && !item.assigned_operator_id
                        ? (
                          <form
                            method="post"
                            action={`/moderation/work/${item.work_type}/${item.item_id}/assign`}
                          >
                            <button type="submit">Assign to me</button>
                          </form>
                        )
                        : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav class="row-actions" aria-label="Work queue pages">
            {work.page > 1
              ? <a rel="prev" href={pageHref(work.page - 1)}>Previous</a>
              : null}
            <span>Page {work.page}</span>
            {work.page * 25 < work.total
              ? <a rel="next" href={pageHref(work.page + 1)}>Next</a>
              : null}
          </nav>
        </section>
        <section>
          <h2>Open reviews</h2>
          {snapshot.reviews.map((review) => (
            <article class="moderation-item" key={String(review.review_id)}>
              <div>
                <strong>{String(review.target_username)}</strong>
                <p>{String(review.content)}</p>
                <small>
                  {String(review.severity)} · {String(review.reason_code)}
                </small>
              </div>
              <form
                method="post"
                action={`/moderation/reviews/${review.review_id}/resolve`}
              >
                <select name="resolution" aria-label="Resolution">
                  <option value="dismissed">Dismiss</option>
                  <option value="confirmed">Confirm</option>
                  <option value="escalated">Escalate</option>
                </select>
                <select name="action_type" aria-label="Action">
                  <option value="">No action</option>
                  <option value="warn">Warn</option>
                  <option value="timeout">Timeout</option>
                  <option value="ban">Ban</option>
                </select>
                <input
                  name="duration_seconds"
                  type="number"
                  min="1"
                  max="2419200"
                  value="600"
                  aria-label="Duration seconds"
                />
                <input
                  name="confirmation"
                  placeholder="PERMANENT BAN for bans"
                />
                <input name="note" placeholder="Analyst note" />
                <button type="submit">Resolve</button>
              </form>
            </article>
          ))}
          {!snapshot.reviews.length
            ? <p class="empty-state">No open reviews.</p>
            : null}
        </section>
        <section>
          <h2>Saved filters</h2>
          <div class="row-actions">
            {snapshot.savedFilters.map((filter) => (
              <a
                href={`/moderation?${new URLSearchParams(
                  filter.filters as Record<string, string>,
                )}`}
                key={String(filter.id)}
              >
                {String(filter.name)}
              </a>
            ))}
          </div>
          <form method="post" action="/moderation/filters">
            <input
              name="name"
              required
              maxLength={80}
              placeholder="Saved filter name"
            />
            <input
              type="hidden"
              name="filters"
              value={JSON.stringify(filters)}
            />
            <button type="submit">Save current filter</button>
          </form>
        </section>
        <section class="moderation-columns">
          <div>
            <h2>Member reports</h2>
            <p>{snapshot.reports.length} open</p>
          </div>
          <div>
            <h2>Sanction appeals</h2>
            <p>{snapshot.appeals.length} open</p>
          </div>
          <div>
            <h2>Rules</h2>
            <p>{snapshot.rules.length} configured</p>
          </div>
        </section>
        <section>
          <h2>Bulk action</h2>
          <form method="post" action="/moderation/bulk">
            <input
              name="target_platform_account_ids"
              required
              placeholder="Account IDs, comma separated"
            />
            <select name="action_type">
              <option value="warn">Warn</option>
              <option value="timeout">Timeout</option>
              <option value="ban">Permanent ban</option>
            </select>
            <input
              name="duration_seconds"
              type="number"
              min="1"
              max="2419200"
              value="600"
            />
            <input name="reason" required placeholder="Reason" />
            <input
              name="confirmation"
              required
              placeholder="BULK ACTION COUNT"
            />
            <button type="submit">Queue action</button>
          </form>
        </section>
        <section>
          <h2>Member reports</h2>
          {snapshot.reports.map((item) => (
            <article class="moderation-item" key={String(item.item_id)}>
              <div>
                <strong>{String(item.username)}</strong>
                <p>{String(item.summary)}</p>
              </div>
              <form
                method="post"
                action={`/moderation/reports/${item.item_id}/resolve`}
              >
                <select name="resolution">
                  <option value="substantiated">Substantiate</option>
                  <option value="dismissed">Dismiss</option>
                  <option value="escalated">Escalate</option>
                </select>
                <input name="note" required placeholder="Resolution note" />
                <button type="submit">Resolve</button>
              </form>
            </article>
          ))}
        </section>
        <section>
          <h2>Sanction appeals</h2>
          {snapshot.appeals.map((item) => (
            <article class="moderation-item" key={String(item.item_id)}>
              <div>
                <strong>{String(item.username)}</strong>
                <p>{String(item.summary)}</p>
              </div>
              <form
                method="post"
                action={`/moderation/appeals/${item.item_id}/resolve`}
              >
                <select name="resolution">
                  <option value="upheld">Uphold</option>
                  <option value="reversed">Reverse</option>
                  <option value="modified">Modify</option>
                </select>
                <input
                  name="note"
                  required
                  placeholder="Independent review note"
                />
                <button type="submit">Resolve</button>
              </form>
            </article>
          ))}
        </section>
        <section>
          <h2>Provider actions</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Action</th>
                  <th>Queue state</th>
                  <th>Provider state</th>
                  <th>Confirmation</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.actions.map((action) => (
                  <tr key={String(action.action_id)}>
                    <td>{String(action.target_username ?? "")}</td>
                    <td>{String(action.action_type ?? "")}</td>
                    <td>{String(action.status ?? "pending")}</td>
                    <td>
                      {String(action.provider_status ?? "Awaiting provider")}
                    </td>
                    <td>
                      {action.provider_confirmed_at
                        ? String(action.provider_confirmed_at)
                        : "Not confirmed"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!snapshot.actions.length
            ? <p class="empty-state">No provider actions yet.</p>
            : null}
        </section>
        <section>
          <h2>Active rules</h2>
          <form method="post" action="/moderation/rules/drafts">
            <input name="name" required placeholder="Rule name" />
            <select name="rule_type">
              <option value="exact_term">Exact term</option>
              <option value="banned_phrase">Phrase</option>
              <option value="link_restriction">Link restriction</option>
              <option value="duplicate_message">Duplicate</option>
              <option value="egregious_term">Egregious</option>
            </select>
            <input name="pattern" required placeholder="Pattern" />
            <select name="severity">
              <option>low</option>
              <option>medium</option>
              <option>high</option>
              <option>critical</option>
            </select>
            <select name="auto_enforce_action">
              <option value="">Review only</option>
              <option value="warn">Warn</option>
              <option value="timeout">Timeout</option>
              <option value="ban">Ban</option>
            </select>
            <input
              name="action_duration_seconds"
              type="number"
              min="1"
              max="2419200"
              value="600"
            />
            <label>
              <input
                type="checkbox"
                name="platform_scope"
                value="discord"
                checked
              />{" "}
              Discord
            </label>
            <label>
              <input
                type="checkbox"
                name="platform_scope"
                value="twitch"
                checked
              />{" "}
              Twitch
            </label>
            <button type="submit">Create draft</button>
          </form>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Severity</th>
                  <th>Mode</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.rules.map((rule) => (
                  <tr key={String(rule.rule_id)}>
                    <td>{String(rule.name)}</td>
                    <td>{String(rule.severity ?? "")}</td>
                    <td>{String(rule.enforcement_mode ?? "")}</td>
                    <td>{String(rule.auto_enforce_action ?? "Review only")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section>
          <h2>Rule versions</h2>
          {snapshot.ruleVersions.map((version) => (
            <article class="moderation-item" key={String(version.version_id)}>
              <div>
                <strong>
                  {String(version.name)} v{String(version.version_number)}
                </strong>
                <p>{String(version.lifecycle_state)}</p>
              </div>
              <div>
                <form
                  method="post"
                  action={`/moderation/rule-versions/${version.version_id}/preview`}
                >
                  <textarea
                    name="samples"
                    required
                    placeholder="One sample per line"
                  />
                  <button type="submit">Test samples</button>
                </form>
                <form
                  method="post"
                  action={`/moderation/rule-versions/${version.version_id}/publish`}
                >
                  <select name="lifecycle_state">
                    <option value="shadow">Shadow</option>
                    <option value="enforce">Enforce</option>
                  </select>
                  <button type="submit">Publish</button>
                </form>
                <form
                  method="post"
                  action={`/moderation/rule-versions/${version.version_id}/rollback`}
                >
                  <button type="submit">Rollback</button>
                </form>
              </div>
            </article>
          ))}
          <h3>Add exemption</h3>
          <form method="post" action="/moderation/rules/0/exemptions">
            <input
              name="rule_id"
              type="number"
              min="1"
              required
              placeholder="Rule ID"
            />
            <select name="exemption_type">
              <option value="channel">Channel</option>
              <option value="platform_account">Member account</option>
            </select>
            <input
              name="exemption_value"
              required
              placeholder="Channel or account ID"
            />
            <input name="reason" required placeholder="Reason" />
            <button type="submit">Add exemption</button>
          </form>
        </section>
      </main>
    </div>
  );
}
