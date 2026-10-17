import type { BackerService } from "../src/data/backer_repository.ts";
import type { DatabaseRow } from "../src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

function Rows({ rows }: { rows: readonly DatabaseRow[] }) {
  if (!rows.length) return <p>No records.</p>;
  const keys = Object.keys(rows[0]);
  return (
    <div class="table-wrap">
      <table class="command-table">
        <thead>
          <tr>
            {keys.map((key) => (
              <th key={key} scope="col">{key.replaceAll("_", " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {keys.map((key) => (
                <td
                  key={key}
                  style={{ maxWidth: "26rem", overflowWrap: "anywhere" }}
                >
                  {row[key] === null ? "None" : String(row[key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RewardsWorkspace(
  {
    admin,
    campaign = "",
    page = 1,
    overview,
    preview,
    previewId,
    benefits = [],
    organizations = [],
    emailReady = false,
  }: {
    admin: boolean;
    campaign?: string;
    page?: number;
    overview?: Awaited<ReturnType<BackerService["overview"]>>;
    preview?: Awaited<ReturnType<BackerService["previewDetail"]>>;
    previewId?: string;
    benefits?: readonly DatabaseRow[];
    organizations?: readonly DatabaseRow[];
    emailReady?: boolean;
  },
) {
  return (
    <div class="app-shell">
      <DashboardHeader
        active={admin ? "/admin" : "/billing"}
        platformAdmin={admin}
      />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">QBot4K</p>
            <h1>{admin ? "Backer fulfillment" : "Rewards and claims"}</h1>
          </div>
        </section>
        <nav class="admin-navigation">
          <a href="/billing">Billing</a>
          {admin && <a href="/admin">Platform admin</a>}
        </nav>
        {!admin && (
          <>
            <form
              method="post"
              action="/billing/rewards/claim"
              class="settings-form"
              style={{
                gridTemplateColumns: "minmax(0,1fr)",
                maxWidth: "42rem",
              }}
            >
              <label for="claim-organization">Organization</label>
              <select id="claim-organization" name="organization_id" required>
                {organizations.map((organization) => (
                  <option
                    value={String(organization.id)}
                    key={String(organization.id)}
                  >
                    {String(organization.name)}
                  </option>
                ))}
              </select>
              <label for="claim-code">Claim code</label>
              <input
                id="claim-code"
                name="code"
                required
                minLength={64}
                maxLength={64}
                autoComplete="off"
                spellcheck={false}
              />
              <label>
                <input
                  type="checkbox"
                  name="consent"
                  value="confirmed"
                  required
                />{" "}
                I own this organization and authorize stopping existing
                subscription renewals. Fixed-term rewards begin after paid
                access and do not auto-renew.
              </label>
              <div class="form-actions">
                <button type="submit" disabled={!organizations.length}>
                  Claim reward
                </button>
              </div>
            </form>
            <h2>Benefits</h2>
            <Rows rows={benefits} />
          </>
        )}
        {admin && (
          <>
            {!emailReady && (
              <p class="status-banner">
                Claim delivery disabled: verified sender configuration required.
              </p>
            )}
            <details>
              <summary>Register campaign</summary>
              <form
                method="post"
                action="/admin/rewards/campaign"
                class="settings-form"
                style={{
                  gridTemplateColumns: "minmax(0,1fr)",
                  maxWidth: "42rem",
                }}
              >
                <label for="campaign-id">Campaign ID</label>
                <input
                  id="campaign-id"
                  name="campaign_id"
                  required
                  maxLength={100}
                />
                {[
                  ["threeMonths", "Three-month reward ID"],
                  ["sixMonths", "Six-month reward ID"],
                  ["oneYear", "One-year reward ID"],
                  ["lifetime", "Lifetime reward ID"],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input name={key} maxLength={100} />
                  </label>
                ))}
                <label for="columns">CSV column mapping (JSON)</label>
                <textarea
                  id="columns"
                  name="columns"
                  rows={8}
                  required
                  defaultValue={JSON.stringify(
                    {
                      backer_id: "backer_id",
                      email: "email",
                      reward_id: "reward_id",
                      currency: "currency",
                      collected_cents: "collected_cents",
                      refunded_cents: "refunded_cents",
                      collection_status: "collection_status",
                    },
                    null,
                    2,
                  )}
                />
                <button type="submit">Register inactive campaign</button>
              </form>
            </details>
            <form method="get" action="/admin/rewards" class="admin-navigation">
              <label>
                Campaign{" "}
                <select name="campaign" value={campaign}>
                  {overview?.campaigns.map((row) => (
                    <option key={String(row.id)} value={String(row.id)}>
                      {String(row.id)}
                      {row.live_enabled ? " (live)" : " (inactive)"}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit">Open campaign</button>
            </form>
            {campaign && (
              <>
                <form method="post" action="/admin/rewards/activate">
                  <input type="hidden" name="campaign_id" value={campaign} />
                  <label>
                    <input
                      type="checkbox"
                      name="consent"
                      value="confirmed"
                      required
                    />{" "}
                    Campaign funded, payments collected, reward mapping
                    verified.
                  </label>{" "}
                  <button type="submit" disabled={!emailReady}>
                    Activate fulfillment
                  </button>
                </form>
                <h2>Import backers</h2>
                <form
                  method="post"
                  encType="multipart/form-data"
                  action="/admin/rewards/preview"
                  class="settings-form"
                  style={{
                    gridTemplateColumns: "minmax(0,1fr)",
                    maxWidth: "42rem",
                  }}
                >
                  <input type="hidden" name="campaign_id" value={campaign} />
                  <label>
                    Verified backer CSV<input
                      type="file"
                      name="csv"
                      accept=".csv,text/csv"
                      required
                    />
                  </label>
                  <button type="submit">Preview import</button>
                </form>
              </>
            )}
            {preview && (
              <>
                <h2>Import preview: {preview.count} backers</h2>
                <Rows rows={preview.rows} />
                {!preview.confirmed && (
                  <form method="post" action="/admin/rewards/confirm">
                    <input type="hidden" name="import_id" value={previewId} />
                    <input
                      type="hidden"
                      name="campaign_id"
                      value={preview.campaign}
                    />
                    <label>
                      <input
                        type="checkbox"
                        name="consent"
                        value="confirmed"
                        required
                      />{" "}
                      I verified collection, refunds, reward IDs and every
                      preview row.
                    </label>{" "}
                    <button type="submit">Confirm import</button>
                  </form>
                )}
              </>
            )}
            <h2>Import history</h2>
            <Rows rows={overview?.imports ?? []} />
            {overview?.imports.map((row) => (
              <p key={String(row.id)}>
                <a
                  href={`/admin/rewards?campaign=${
                    encodeURIComponent(campaign)
                  }&preview=${encodeURIComponent(String(row.id))}`}
                >
                  Review import {String(row.id)}
                </a>
              </p>
            ))}
            <h2>Fulfillment</h2>
            <Rows rows={overview?.backers.slice(0, 50) ?? []} />
            <p>
              <a
                href={`/admin/rewards/export.csv?campaign=${
                  encodeURIComponent(campaign)
                }&page=${page}`}
              >
                Download reconciliation page CSV
              </a>
            </p>
            <nav class="admin-navigation">
              {page > 1 && (
                <a
                  href={`/admin/rewards?campaign=${
                    encodeURIComponent(campaign)
                  }&page=${page - 1}`}
                >
                  Previous
                </a>
              )}
              <span>Page {page}</span>
              {(overview?.backers.length ?? 0) > 50 && (
                <a
                  href={`/admin/rewards?campaign=${
                    encodeURIComponent(campaign)
                  }&page=${page + 1}`}
                >
                  Next
                </a>
              )}
            </nav>
            <form
              method="post"
              action="/admin/rewards/recovery"
              class="admin-navigation"
            >
              <input type="hidden" name="campaign_id" value={campaign} />
              <label>
                Backer record ID{" "}
                <input type="number" name="backer_id" min="1" required />
              </label>
              <label>
                Action{" "}
                <select name="action">
                  <option value="resend">Resend claim email</option>
                  <option value="retry">Retry fulfillment</option>
                </select>
              </label>
              <button type="submit">Queue recovery</button>
            </form>
            <h2>Reward audit</h2>
            <Rows rows={overview?.audit ?? []} />
          </>
        )}
      </main>
    </div>
  );
}
