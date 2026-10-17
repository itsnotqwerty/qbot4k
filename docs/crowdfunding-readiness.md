# Crowdfunding Readiness

## Reward Release: Schema 32

The reward workflow is implemented behind operational activation gates. This is
not approval to publish the campaign or enable paid enforcement. Production must
start with no live campaigns and `platform_billing_policy.enforce_paid_access`
false until the acceptance and external-setup requirements below are complete.

- `/admin/rewards`: explicit platform-admin campaign registration, CSV preview
  and confirmation, import history, redacted fulfillment/audit views, paginated
  reconciliation CSV, email resend and failed-fulfillment retry.
- `/billing/rewards`: current organization-owner claim, explicit cancellation
  consent, organization binding and benefits status. One reward per organization
  is currently supported; additional rewards require support review.
- Claims expire seven days after successful delivery processing. Codes are
  HMAC-derived from a durable random nonce and the existing
  credential-encryption key, with only a digest stored for lookup. Retries reuse
  the code and email idempotency key. Resend rotates the nonce and invalidates
  the old code. Claim attempts are limited to ten per account per fifteen
  minutes.
- Verified refunds revoke only the affected Kickstarter grant in the import
  transaction, even if Stripe is unavailable. Provider failures remain visible
  and retry after five minutes. Missing backers in an import are unchanged.
- Reward fulfillment expires open Checkout sessions and confirms period-end
  cancellation before granting new access. Term rewards start after verified
  paid time; lifetime starts immediately. Repeat processing cannot extend a
  term.
- A qualifying backer can explicitly buy a post-reward subscription with the
  private 20%-forever coupon. It is bound to that subscription; cancellation
  ends this discount lifecycle. A refund below eligibility removes the coupon
  without deleting unrelated payment history. Restoring a previously removed
  discount on an existing subscription requires support review.
- Hosted Customer Portal supports payment-method updates, invoice history and
  period-end cancellation. Plan changes must remain disabled. The application
  validates this configuration before creating a portal session.

### Resend Setup

Populate these blank fields from `.env.example` in the private `.env` file:

```dotenv
QBOT_REWARD_EMAIL_API_KEY=
QBOT_REWARD_EMAIL_FROM=
QBOT_CREDENTIAL_ENCRYPTION_KEY=
QBOT_STRIPE_BACKER_COUPON_ID=
QBOT_STRIPE_PORTAL_CONFIGURATION_ID=
```

Use a verified Resend domain and a plain sender address, without a display name.
The API key needs email-sending and domain-read permissions. Never paste the key
into chat or commit it. Preserve an existing `QBOT_CREDENTIAL_ENCRYPTION_KEY`;
replacing it would affect encrypted provider credentials and pending claim
codes. Production uses `/etc/qbot4k/qbot4k.env`, not the developer's `.env`;
transfer selected values securely and restart the web role after configuration.
The service does not automatically load `.env.example`. Runtime permissions
include these variables and HTTPS to `api.resend.com:443`.

Configuration presence is not proof of sender verification. Campaign activation
now queries Resend and requires a matching verified sender domain; unauthorized
keys, unavailable verification and pending domains fail closed. Verify the
sender in Resend, send a controlled test, and rehearse the complete claim before
staff activate a real campaign. Without key, sender, signing key or HTTPS
origin, delivery is disabled and activation is rejected. Rotating the signing
key while mail is pending requires deliberate resend and credential-migration
review.

### Verified Imports

Use the actual Kickstarter campaign and reward IDs, never invented production
IDs or pledge-size inference. Register the immutable reward mapping and exact
CSV column mapping while inactive. Normalize a creator export using verified
collection/refund evidence into these columns (up to 2 MB and 5,000 rows):

```csv
backer_id,email,reward_id,currency,collected_cents,refunded_cents,collection_status
```

Currency is `USD`; amounts are integer cents, excluding tax and shipping.
Collection status is `pending`, `collected` or `failed`; uncollected rows must
have zero collected cents. A pledged amount alone is not collection evidence.
Preview every row and confirm explicitly. Imports are digest-deduplicated;
identical previously confirmed contents are a no-op, not a restoration tool.
Changed email or reward identity is rejected for manual review. Do not directly
rewrite claimed identities or reward dates. Import payloads contain email PII;
restrict database/backups, establish retention and erasure procedures before
accepting real backers. The staff UI and CSV omit email and claim credentials.

After sender and payment rehearsal, select the campaign, confirm successful
collection and mapping, then activate fulfillment. The web worker processes up
to ten due records per thirty-second cycle. Monitor failed rows, `attempts`,
`last_error`, email-provider delivery/bounces and the reward audit. Retry is
idempotent; resend is for eligible unclaimed backers only. Stop a campaign via a
reviewed database update to `backer_campaigns.live_enabled=FALSE` if delivery
needs to be paused. In-flight external requests cannot be recalled.

### Community Connection Policy

The shared billing catalog permits one Twitch channel and up to three Discord
servers per community. The integration repository enforces these limits at OAuth
start and completion, independently of the paid-access rollout flag. Completion
locks the community row before checking capacity and writing the installation,
so simultaneous callbacks cannot exceed the limit. All non-revoked
installations, including pending or degraded connections, consume capacity.
Reconnecting the same provider identity does not consume another slot;
reactivating a revoked connection requires available capacity.

Only an active operator with an explicit platform staff role (`support`,
`billing`, or `admin`) is exempt from capacity limits. Community owners/admins
and staff-owned organizations do not confer this exemption on other users. Staff
still need community authorization and Discord guild permissions. These
permissions and staff status are rechecked at completion. Cross-community
installation reassignment is rejected, including after an OAuth race.

Existing connections are not automatically disconnected. Review any existing
over-limit communities before rollout; non-staff connection changes that would
leave the selected platform over its limit are rejected until capacity is freed.

### Enforcement Rollout

Schema 32 adds `qbot_community_has_access`, a default-off platform policy and
optional `organizations.beta_access_until`. The predicate recognizes current
grants, explicit beta expiry and active admin-owner complimentary access. Web
requests, quota-consuming ingestion/actions, machine API authorization,
queued-job selection and pre-dispatch checks consult it. Discord/Twitch action
queries and announcement selection/claiming also check current access. Jobs
deferred after claim return to pending without consuming a retry attempt.
Billing, claims, login, legal pages, community switching, permitted exports,
health and platform-admin recovery remain reachable. Unpaid queued jobs remain
pending; existing safety quotas still apply. Web denials are HTTP 402;
quota-path denials currently use the quota error and retry delay.

Do not enable enforcement yet: real tenant/provider end-to-end acceptance is
still required. External requests already accepted by a provider cannot be
recalled, and access checks do not cancel in-flight provider requests. Current
beta access is preserved by leaving the policy false. Before enabling, assign
explicit beta expiries or paid/staff entitlements, audit every tenant and
rehearse access expiry and recovery. Staff MFA, privacy retention/erasure,
identity/reward correction and real Stripe sandbox acceptance remain launch
requirements. No real email, pledge import or payment is part of automated
tests.

### Environment Follow-up

The 2026-10-17 follow-up validates all service roles before replacing the live
environment. Preserve production database/storage paths, OAuth callback and the
existing credential-encryption key when merging a developer environment. Invalid
Resend keys were rejected; the replacement key and verified sender domain passed
a read-only check and a single Resend simulator send. No customer email or
reward claim was sent. Campaigns and paid enforcement remain disabled at the
owner's request pending actual IDs and sandbox fixtures.

Reward-only claims no longer create unused Stripe setup records. Checkout, claim
and fulfillment use a common organization advisory lock, including before a
Stripe customer exists. PostgreSQL regression coverage exercises reward expiry
followed by the first discounted subscription purchase.

## Approved Offers

All prices are USD. One organization owns billing and initially receives access
for one QBot4K community. Provider installation authorization remains separate.

| Channel     | Offer                | Price   | Access                                  |
| ----------- | -------------------- | ------- | --------------------------------------- |
| Stripe      | Monthly subscription | $9.99   | One calendar month per renewal          |
| Stripe      | Annual subscription  | $99.99  | One year per renewal                    |
| Kickstarter | Three-month reward   | $25     | Three calendar months                   |
| Kickstarter | Six-month reward     | $50     | Six calendar months                     |
| Kickstarter | One-year reward      | $100    | Twelve calendar months                  |
| Kickstarter | Lifetime reward      | $149.99 | Lifetime under published campaign terms |

Annual billing saves $19.89 compared with twelve monthly payments, approximately
two months. Lifetime access is Kickstarter-exclusive, not a Stripe product.

Collected Kickstarter contributions of at least $50 qualify for a 20% ongoing
recurring subscription discount. Eligibility is calculated from the eligible USD
amount after refunds, excluding tax and shipping. The six-month and one-year
rewards therefore also qualify; the three-month reward alone does not. Reward
selection determines access duration, not an inferred pledge-amount bracket.

Fixed-term access does not auto-renew or charge a payment method. New customers
activate it explicitly; an existing subscriber's reward should start after their
paid-through period with subsequent recurring renewal stopped. Discount
eligibility survives that transition and applies when the backer explicitly
starts a post-reward subscription. A lifetime claim must stop future recurring
renewals without deleting independent payment history.

## Implemented Foundation

- [Versioned offer catalog and domain model](../src/domain/billing.ts): exact
  prices, one-community limit, calendar-month expiry with month-end/leap-year
  handling, net-collected discount eligibility, and source-specific entitlement
  resolution. Expiry is exclusive; revoked, future, and foreign-organization
  grants do not confer current access.
- [Billing repository](../src/data/billing_repository.ts): explicit organization
  billing roles, separate platform-staff roles, active-account checks,
  transactional organization/workspace/community creation, scoped audit and
  session invalidation, and entitlement reads. Customer provisioning now uses
  authenticated server state and a locked session-version check to prevent
  duplicate organizations from concurrent signup requests.
- [Migration 29](../src/data/operations.ts): additive organization membership,
  platform staff, access grant and billing audit tables. Source references are
  unique to prevent allocating one grant to multiple organizations. Only
  Kickstarter lifetime grants may have an unbounded term.
- [Login isolation](../src/data/repository.ts): login no longer automatically
  makes a new account a member or owner of default community 1. Existing
  memberships and explicit invitation acceptance are retained.
- Opt-in customer signup at `/account`, with a responsive organization form,
  same-origin POST validation, bounded input, safe error responses and fresh
  login after provisioning. Accounts without a community cannot authorize
  community dashboard requests. Signup creates no paid access or staff role.
- OAuth requires both a signed state and its matching browser cookie. Disabled
  accounts cannot be reactivated by a pending invitation, and community
  switching checks current account status and session version.
- Shared service restart and database reset now require an explicit active
  platform admin, not a community owner role. No staff roles are auto-assigned.
- Discord command management recognizes active organization and community owners
  using the persisted message author's Discord ID, even when Discord supplies no
  moderator flag or named role. Username matching is not authority.
- Migration 30 scopes custom commands and aliases to their community across
  Discord, Twitch and the dashboard. Shared built-in template edits require
  platform-admin access; community owners cannot change global responses.
- Migration 31 adds environment-specific Stripe customer/Checkout records and a
  durable webhook inbox. The billing page, hosted subscription Checkout, signed
  event ingestion and background reconciliation are implemented.

Configured Checkout can initiate a subscription only after an organization owner
or billing manager explicitly proceeds to Stripe and confirms payment. Signup
itself grants no paid access or staff role and starts no subscription. Paid
restrictions on existing workflows and cap enforcement across all
installation/creation paths remain pending. Do not publish live sales yet.

## Upgrade Boundary

Run migrations with the normal backup procedure before starting the updated
services. Readiness expects schema version 32. Before stopping services, run
`deno install --frozen`, `deno task build` and `deno cache --frozen runtime.ts`
under the service account, using its Deno cache. The read-only systemd sandbox
cannot cache new runtime dependencies; a Vite build alone does not prepare every
runtime import. Keep the sandbox protection enabled.

Migration 30 changes custom command uniqueness to `(community_id, command_name)`
and assigns legacy global custom commands to default community 1 only. Review
legacy ownership and deliberately recreate commands for any other intended
communities; the upgrade does not copy private responses to every tenant. Direct
custom-command writes must now specify a community ID. Stop old writers during
this upgrade because their unscoped SQL is no longer compatible.

No organization owners or platform staff are inferred or backfilled from
community roles. Audit legacy ownership, especially shared default
organizations, before assigning billing owners or enabling customer signup. Do
not treat community owner wildcard permissions as staff or organization billing
authority.

Customer signup defaults off (`QBOT_CUSTOMER_SIGNUP_ENABLED=false`). Enabling it
requires an explicit HTTPS `QBOT_DISCORD_OAUTH_REDIRECT_URI` and allows Discord
accounts outside the operator guild list to sign in as account-only viewers. The
web-role permission allowlist includes the new flag. Provisioning creates an
explicit organization/community owner and invalidates the old session; the
customer must sign in again. With signup disabled, the operator-guild gate
remains and new accounts need an invitation or deliberate provisioning.

Do not enable public signup yet. Explicit staff bootstrap and legacy ownership
review remain pending, as do paid-access enforcement and the remaining shared
administrative-surface isolation review. Operators relying on community-owner
access to shared restart/reset must arrange explicit platform-admin membership
through a reviewed database administration procedure before upgrading. There is
no self-service staff grant or bootstrap command yet.

## Stripe Runtime Configuration

The runtime recognizes these variables from its process environment or an
explicitly supplied environment file. The tracked `.env.example` is a template,
not an automatically loaded configuration file. Keep real secrets in the
service's private environment file and restart affected services after changes.

| Variable                    | AppSettings field      | Process access |
| --------------------------- | ---------------------- | -------------- |
| `STRIPE_PUBLISHABLE_KEY`    | `stripePublishableKey` | web            |
| `STRIPE_SECRET_KEY`         | `stripeSecretKey`      | web, jobs      |
| `STRIPE_WEBHOOK_SECRET`     | `stripeWebhookSecret`  | web            |
| `QBOT_STRIPE_PRICE_MONTHLY` | `stripePriceMonthly`   | web, jobs      |
| `QBOT_STRIPE_PRICE_ANNUAL`  | `stripePriceAnnual`    | web, jobs      |

Web and jobs tasks permit HTTPS access to `api.stripe.com:443`. The billing
worker currently runs with the web service; no jobs-role billing worker is
needed. Discord, Twitch and analysis tasks do not receive Stripe environment
permissions. With no billing settings, billing endpoints return 503. Once any
server billing setting is supplied, the web role requires the API key, signing
secret, both prices and an explicit HTTPS `QBOT_DISCORD_OAUTH_REDIRECT_URI`. Its
origin supplies canonical Checkout return URLs, never the incoming Host header.
A publishable key is not needed for hosted Checkout.

Public/API keys must use matching test/live modes and prices must differ. Before
Checkout and entitlement reconciliation, Stripe prices are checked for active
USD licensed recurring pricing at exactly 999 cents monthly or 9999 cents
annually, with one interval and one unit. Restricted `rk_` keys need customer
and Checkout write access plus subscription, invoice, price and charge read
access. The official SDK is pinned to 17.7.0 and API version
`2025-02-24.acacia`.

With the service environment loaded, `deno task check-config` reports
`stripe_api_configured`, `stripe_publishable_key_configured`,
`stripe_webhook_configured`, `stripe_prices_configured` and `stripe_mode`. These
are configuration indicators, not payment readiness checks. Credential values
are excluded from the summary and Stripe secrets are redacted from structured
logs. Configuring these settings now enables billing routes and reconciliation;
it does not automatically charge existing users.

## Checkout And Webhooks

1. Back up and run `deno task migrate` under the private service environment,
   then restart the web role with schema version 32. Keep public signup off
   until the other launch gates below are complete.
2. Configure Stripe to deliver events to `https://YOUR_HOST/webhooks/stripe`,
   with the corresponding endpoint signing secret in `STRIPE_WEBHOOK_SECRET`.
   Select Checkout session, customer subscription, invoice, charge/refund and
   dispute events. Use separate test/live endpoint secrets and keys. For a local
   sandbox, Stripe CLI can forward events to the local web-role port; use the
   CLI's signing secret for that test run.
3. Sign in as an explicit organization owner/billing manager and open `/billing`
   (also under the dashboard's More menu). Choose monthly or annual, proceed to
   Stripe, and complete the payment there. Community ownership alone does not
   grant organization billing authority.
4. Check `stripe_event_inbox` status and the organization's billing page. Valid
   events receive HTTP 200 only after durable insertion. Invalid/stale
   signatures receive 400; oversized bodies receive 413; failed persistence
   receives 503 so Stripe retries. Event IDs are unique per environment.

The web worker checks pending events every 30 seconds and known customers at
least every 15 minutes when the queue permits. Processing fetches current
subscriptions and paid invoice lines under a customer lock; event order and the
success redirect are never payment proof. Failed events retry after one minute.
Unknown customers are quarantined and checked again after 15 minutes; event
metadata cannot assign a customer to an organization. Read-only mode blocks
Checkout/webhook writes and does not start the worker.

Only confirmed paid invoice periods for approved subscription items produce live
Stripe grants. Test-mode payments update sandbox billing state but never grant
production access. Cancellation retains confirmed paid-through access; failed
renewals do not extend it. Refunded (including partially refunded) or disputed
charges are conservatively excluded pending the final refund/grace policy.
Unrelated Kickstarter/manual grants are preserved. Zero-value, out-of-band and
prorated invoice lines do not create new access periods.

Concurrent purchase requests reuse one Checkout session. Open subscriptions,
unexpired paid periods and active Kickstarter rewards block new Checkout.
Expired sessions can be replaced, and ended subscriptions can be purchased
again. Attempt keys survive lost responses; unresolved attempts older than 23
hours fail closed instead of risking Stripe's expiring idempotency window.
Support must review the Stripe customer/session and bind the known result or
confirm expiration before resetting such an attempt. Do not blindly clear
customer mappings or retry with a new key. Staff recovery UI is still pending.

Before live rollout, run an actual Stripe sandbox purchase, SCA/asynchronous
payment, renewal and refund/dispute rehearsal with this endpoint. Automated
tests use synthetic Stripe responses and official signature verification, not
real Stripe account credentials. Customer Portal, self-service cancellation,
campaign discounts and reward transitions are implemented in schema 32 as
described above. Refund issuance still requires Stripe Dashboard support.

## Remaining Milestones

### Platform Staff Access

Active platform admins can open `/admin` for paginated organization, account and
billing-audit views, with links to existing settings, commands and system
health. Billing shows the Platform admin link only after a current staff check.
The admin overview is read-only; staff provisioning and customer/reward
mutations still require a reviewed administrative procedure.

An active organization owned by an active platform admin receives complimentary
access while that ownership and staff assignment remain valid. Billing hides its
Checkout form and the server rejects subscription creation for that
organization. This is a staff exemption, not a Stripe payment or Kickstarter
lifetime reward. It does not grant membership in other tenants, remove
operational safety limits, or cancel an existing subscription. Staff grants must
be audited and invalidate existing sessions. Disabled accounts or removal of
admin/owner roles revoke the exemption. No schema change beyond version 31 is
needed.

### Launch Work

1. Complete onboarding with explicit operator/staff bootstrap, legacy ownership
   review, organization invitation and owner-transfer workflows. Guard against
   last-owner removal and stale sessions. Enforce the community cap on every
   relevant creation path and verify external installation ownership.
2. Complete real Stripe sandbox acceptance testing, including SCA, renewal,
   refund, cancellation and the implemented portal/reward transitions. Verify
   every execution boundary before enabling the default-off access policy.
3. Supply actual campaign mappings, verified collection exports and a verified
   sender. Rehearse import/claim/retry/refund using controlled fixtures first.
   Implement reviewed identity-correction and privacy retention/erasure flows.
4. Complete staff MFA and exceptional support-recovery procedures. Routine
   import confirmation, claim resend and failed-fulfillment retry now have
   separate staff forms; refunds/disputes remain in Stripe Dashboard.
5. Test end-to-end payment/reward workflows, recovery, restore/reconciliation,
   customer/staff isolation and desktop/mobile pages. Configure scoped runtime
   permissions, secrets, monitoring, transactional email and operational
   runbooks.
6. Publish only after working paid service, campaign approval, demo and support
   readiness, and signed-off terms. Resolve lifetime obligations and quantity
   cap, refund/grace policies, tax treatment, funding target and fulfillment
   dates. No automatic live charges are authorized by development tests.

The approved prices are settled. External account setup and legal/campaign
decisions gate publication, not further foundational implementation.

## Verification

Run focused unit tests:

```sh
deno task test:billing
```

For desktop/mobile Chromium coverage of signup and billing selection, input
validation, duplicate-slug recovery and the hosted Checkout redirect:

```sh
deno run -A npm:@playwright/test@1.62.1 install chromium
deno task test:billing:browser
```

The browser task uses isolated fake authentication and provisioning, never live
OAuth or payment providers. Screenshots are written to `test-results/`.

With a disposable local PostgreSQL database configured:

```sh
QBOT_TEST_POSTGRES_URL=postgres://postgres@127.0.0.1:55432/postgres deno task test:billing:postgres
```

Use the URL and credentials for your own test instance. The integration test
creates a unique schema, migrates it repeatedly, checks populated-data survival,
exercises concurrent provisioning and rollback, tests first-login isolation and
grant constraints, then removes only that schema. The test role needs permission
to create schemas. This task skips when the database URL is absent; a skipped
run is not database verification. Both tasks are included in `deno task check`.

The billing and signup foundations were verified against disposable PostgreSQL
17, including concurrent self-service attempts using different slugs. No Stripe,
email or Kickstarter account credentials were used. Browser coverage passes at
1280px and 390px widths. The existing frozen route-manifest test still reports
five legacy `/onboarding` registrations absent from the committed app; the new
`/account` routes have separate passing controller/browser coverage. The
security-review task also has three existing installer-contract failures because
the root release installer is absent; its account and authorization regressions
pass. The full schema inventory also has an existing unclassified
`role_heartbeats` table; Stripe tables have focused passing classification
tests. These repository gates must be repaired before release.
