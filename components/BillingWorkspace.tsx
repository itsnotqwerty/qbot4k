import type { DatabaseRow } from "../src/data/database.ts";
import { DashboardHeader } from "./DashboardHeader.tsx";

export function BillingWorkspace(
  { organizations, sandbox, platformAdmin = false }: {
    organizations: readonly DatabaseRow[];
    sandbox: boolean;
    platformAdmin?: boolean;
  },
) {
  return (
    <div class="app-shell">
      <DashboardHeader active="/billing" platformAdmin={platformAdmin} />
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">QBot4K</p>
            <h1>Billing</h1>
          </div>
        </section>
        {sandbox && (
          <p class="status-banner">
            Stripe test mode. Test payments do not grant production access.
          </p>
        )}
        {organizations.length === 0 && (
          <p>No organizations with billing access.</p>
        )}
        <nav class="admin-navigation">
          <a href="/billing/rewards">Rewards and claims</a>
        </nav>
        {organizations.map((organization) => (
          <section key={String(organization.id)}>
            <h2>{String(organization.name)}</h2>
            {organization.has_customer === true && (
              <form method="post" action="/billing/portal">
                <input
                  type="hidden"
                  name="organization_id"
                  value={String(organization.id)}
                />
                <button type="submit">Manage Stripe billing</button>
              </form>
            )}
            <p>
              Subscription: {String(organization.subscription_status ?? "none")}
            </p>
            {organization.paid_until && (
              <p>
                Paid through:{" "}
                {new Date(String(organization.paid_until)).toISOString().slice(
                  0,
                  10,
                )}
              </p>
            )}
            {organization.complimentary === true
              ? (
                <p class="status-banner">
                  Complimentary staff access. No subscription required.
                </p>
              )
              : (
                <form
                  method="post"
                  action="/billing/checkout"
                  class="settings-form"
                  style={{
                    gridTemplateColumns: "minmax(0, 1fr)",
                    maxWidth: "40rem",
                  }}
                >
                  <input
                    type="hidden"
                    name="organization_id"
                    value={String(organization.id)}
                  />
                  <label for={`cadence-${organization.id}`}>Subscription</label>
                  <select
                    id={`cadence-${organization.id}`}
                    name="cadence"
                    required
                  >
                    <option value="monthly">$9.99 USD / month</option>
                    <option value="annual">$99.99 USD / year</option>
                  </select>
                  <p>
                    One community per organization. Renews automatically until
                    canceled. Payment is confirmed on Stripe.
                  </p>
                  <div class="form-actions">
                    <button type="submit">Continue to Stripe Checkout</button>
                  </div>
                </form>
              )}
          </section>
        ))}
      </main>
    </div>
  );
}
