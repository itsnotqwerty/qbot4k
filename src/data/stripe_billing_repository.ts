import type { DatabaseConnection, DatabaseRow } from "./database.ts";
import {
  PostgresBillingRepository,
  STAFF_ORGANIZATION_ACCESS_SQL,
} from "./billing_repository.ts";
import type {
  BillingCadence,
  BillingEvent,
  StripeBillingGateway,
} from "../providers/stripe_billing.ts";

export class BillingRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export interface StripeBillingService {
  portal?(
    operatorId: number,
    organizationId: number,
    origin: string,
  ): Promise<string>;
  organizations(operatorId: number): Promise<readonly DatabaseRow[]>;
  checkout(
    operatorId: number,
    organizationId: number,
    cadence: BillingCadence,
    origin: string,
  ): Promise<string>;
  receive(event: BillingEvent): Promise<void>;
  processPending(): Promise<void>;
}

export class PostgresStripeBillingRepository implements StripeBillingService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly provider: StripeBillingGateway,
  ) {}

  async organizations(operatorId: number): Promise<readonly DatabaseRow[]> {
    return await this.connection.query(
      `SELECT organization.id,organization.name,billing.subscription_status,billing.paid_until,billing.customer_id IS NOT NULL AS has_customer,
              ${STAFF_ORGANIZATION_ACCESS_SQL} AS complimentary
       FROM organizations organization
       JOIN organization_operator_roles membership ON membership.organization_id=organization.id
       JOIN operator_accounts operator ON operator.id=membership.operator_id
       LEFT JOIN organization_stripe_billing billing ON billing.organization_id=organization.id AND billing.environment=$2
       WHERE operator.id=$1 AND operator.status='active' AND organization.status='active'
         AND membership.role IN ('owner','billing_manager') ORDER BY organization.id`,
      [operatorId, this.provider.environment],
    );
  }

  async checkout(
    operatorId: number,
    organizationId: number,
    cadence: BillingCadence,
    origin: string,
  ): Promise<string> {
    if (
      !await new PostgresBillingRepository(this.connection).organizationAllows(
        operatorId,
        organizationId,
        "billing.manage",
      )
    ) {
      throw new BillingRequestError(
        403,
        "Organization billing access required.",
      );
    }
    await this.connection.query(
      `INSERT INTO organization_stripe_billing(organization_id,environment,customer_key,checkout_key)
       VALUES ($1,$2,$3,$4) ON CONFLICT(organization_id,environment) DO NOTHING`,
      [
        organizationId,
        this.provider.environment,
        crypto.randomUUID(),
        crypto.randomUUID(),
      ],
    );
    const result = await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtextextended('qbot4k:organization-billing:' || $1::text,0))",
        [organizationId],
      );
      const [billing] = await connection.query(
        "SELECT * FROM organization_stripe_billing WHERE organization_id=$1 AND environment=$2 FOR UPDATE",
        [organizationId, this.provider.environment],
      );
      const repository = new PostgresBillingRepository(connection);
      if (
        !await repository.organizationAllows(
          operatorId,
          organizationId,
          "billing.manage",
        )
      ) {
        throw new BillingRequestError(
          403,
          "Organization billing access required.",
        );
      }
      const entitlement = await repository.entitlement(organizationId);
      if (entitlement.complimentary) {
        throw new BillingRequestError(
          409,
          "Complimentary staff access does not require a subscription.",
        );
      }
      if (entitlement.grants.some((grant) => grant.source === "kickstarter")) {
        throw new BillingRequestError(
          409,
          "An active backer reward already provides access.",
        );
      }
      const [pendingReward] = await connection.query(
        "SELECT id FROM backer_records WHERE organization_id=$1 AND (fulfillment_status IN ('pending','failed') OR (fulfillment_status='fulfilled' AND (access_ends_at IS NULL OR access_ends_at>CURRENT_TIMESTAMP))) LIMIT 1",
        [organizationId],
      );
      if (pendingReward) {
        throw new BillingRequestError(
          409,
          "A claimed reward provides access or is awaiting fulfillment.",
        );
      }
      const [discount] = await connection.query(
        "SELECT id FROM backer_records WHERE organization_id=$1 AND discount_percent=20 AND discount_ended=FALSE AND discount_subscription_id IS NULL AND fulfillment_status IN ('fulfilled','revoked') ORDER BY id LIMIT 1",
        [organizationId],
      );
      if (
        !billing.customer_id &&
        Date.now() - new Date(String(billing.created_at)).valueOf() >
          23 * 60 * 60_000
      ) {
        throw new BillingRequestError(
          409,
          "An unresolved customer setup needs support review.",
        );
      }
      await this.provider.validatePrice(cadence);
      const customerId = billing.customer_id
        ? String(billing.customer_id)
        : await this.provider.createCustomer(
          organizationId,
          `customer:${billing.customer_key}`,
        );
      const subscriptions = await this.provider.subscriptions(customerId);
      if (
        subscriptions.some((subscription) =>
          !["canceled", "incomplete_expired"].includes(subscription.status) ||
          (subscription.endsAt && Date.parse(subscription.endsAt) > Date.now())
        )
      ) {
        throw new BillingRequestError(
          409,
          "This organization already has a subscription or paid access. Manage the existing subscription before purchasing again.",
        );
      }
      const key = String(billing.checkout_key);
      if (billing.checkout_session_id) {
        const existing = await this.provider.checkout(
          String(billing.checkout_session_id),
        );
        if (existing.status === "open") {
          if (billing.checkout_cadence !== cadence) {
            throw new BillingRequestError(
              409,
              "A Checkout session for another billing interval is already open.",
            );
          }
          return this.checkoutUrl(existing.url);
        }
        if (
          existing.status !== "expired" &&
          !(existing.status === "complete" && subscriptions.length > 0)
        ) {
          throw new BillingRequestError(
            409,
            "Checkout has completed and is awaiting reconciliation.",
          );
        }
        await connection.query(
          `UPDATE organization_stripe_billing SET customer_id=$3,checkout_key=$4,checkout_session_id=NULL,
           checkout_created_at=CURRENT_TIMESTAMP WHERE organization_id=$1 AND environment=$2`,
          [
            organizationId,
            this.provider.environment,
            customerId,
            crypto.randomUUID(),
          ],
        );
        return null;
      } else if (
        Date.now() - new Date(String(billing.checkout_created_at)).valueOf() >
          23 * 60 * 60_000
      ) {
        throw new BillingRequestError(
          409,
          "An unresolved Checkout attempt needs support review.",
        );
      }
      const session = await this.provider.createCheckout(
        customerId,
        cadence,
        origin,
        organizationId,
        `checkout:${key}`,
        discount ? String(discount.id) : undefined,
      );
      const url = this.checkoutUrl(session.url);
      await connection.query(
        `UPDATE organization_stripe_billing SET customer_id=$3,checkout_key=$4,checkout_session_id=$5,
           checkout_cadence=$6,checkout_created_at=CURRENT_TIMESTAMP WHERE organization_id=$1 AND environment=$2`,
        [
          organizationId,
          this.provider.environment,
          customerId,
          key,
          session.id,
          cadence,
        ],
      );
      await connection.query(
        `INSERT INTO organization_billing_audit(organization_id,operator_id,action,reason)
         VALUES ($1,$2,'stripe.checkout.created','Explicit subscription purchase request')`,
        [organizationId, operatorId],
      );
      return url;
    });
    return result ??
      await this.checkout(operatorId, organizationId, cadence, origin);
  }

  async receive(event: BillingEvent): Promise<void> {
    if (event.livemode !== (this.provider.environment === "live")) {
      throw new BillingRequestError(400, "Stripe event environment mismatch.");
    }
    const supported =
      /^(checkout\.session\.|customer\.subscription\.|invoice\.|charge\.|refund\.)/u
        .test(event.type);
    await this.connection.query(
      `INSERT INTO stripe_event_inbox(environment,event_id,event_type,customer_id,charge_id,status)
       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT(environment,event_id) DO NOTHING`,
      [
        this.provider.environment,
        event.id,
        event.type,
        event.customerId,
        event.chargeId ?? null,
        supported ? "pending" : "ignored",
      ],
    );
  }

  async portal(
    operatorId: number,
    organizationId: number,
    origin: string,
  ): Promise<string> {
    if (
      !await new PostgresBillingRepository(this.connection).organizationAllows(
        operatorId,
        organizationId,
        "billing.manage",
      )
    ) {
      throw new BillingRequestError(
        403,
        "Organization billing access required.",
      );
    }
    const [billing] = await this.connection.query(
      "SELECT customer_id FROM organization_stripe_billing WHERE organization_id=$1 AND environment=$2",
      [organizationId, this.provider.environment],
    );
    if (!billing?.customer_id || !this.provider.portal) {
      throw new BillingRequestError(
        409,
        "No Stripe billing account is available.",
      );
    }
    const url = new URL(
      await this.provider.portal(String(billing.customer_id), origin),
    );
    if (
      url.protocol !== "https:" || url.hostname !== "billing.stripe.com" ||
      url.username || url.password
    ) throw new Error("Invalid portal URL");
    return url.href;
  }

  async processPending(): Promise<void> {
    const events = await this.connection.query(
      `SELECT event_id,customer_id,charge_id FROM stripe_event_inbox
       WHERE environment=$1 AND status IN ('pending','failed','quarantined') AND next_attempt_at<=CURRENT_TIMESTAMP
       ORDER BY received_at LIMIT 10`,
      [this.provider.environment],
    );
    for (const event of events) {
      try {
        const customerId = event.customer_id
          ? String(event.customer_id)
          : event.charge_id
          ? await this.provider.customerForCharge(String(event.charge_id))
          : null;
        const processed = customerId
          ? await this.reconcile(customerId, String(event.event_id))
          : false;
        if (!processed) {
          await this.connection.query(
            `UPDATE stripe_event_inbox SET status='quarantined',attempts=attempts+1,
           last_error='Unknown customer mapping',next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '15 minutes'
           WHERE environment=$1 AND event_id=$2 AND status<>'processed'`,
            [this.provider.environment, String(event.event_id)],
          );
        }
      } catch {
        await this.connection.query(
          `UPDATE stripe_event_inbox SET status='failed',attempts=attempts+1,
           last_error='Stripe reconciliation failed',next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '1 minute'
           WHERE environment=$1 AND event_id=$2 AND status<>'processed'`,
          [this.provider.environment, String(event.event_id)],
        );
      }
    }
    const due = await this.connection.query(
      `SELECT customer_id FROM organization_stripe_billing WHERE environment=$1 AND customer_id IS NOT NULL
       AND (reconciled_at IS NULL OR reconciled_at<CURRENT_TIMESTAMP-INTERVAL '15 minutes')
       ORDER BY reconciled_at NULLS FIRST LIMIT 10`,
      [this.provider.environment],
    );
    for (const row of due) {
      try {
        await this.reconcile(String(row.customer_id));
      } catch {
        await this.connection.query(
          `UPDATE organization_stripe_billing SET reconciled_at=CURRENT_TIMESTAMP-INTERVAL '14 minutes'
           WHERE environment=$1 AND customer_id=$2`,
          [this.provider.environment, String(row.customer_id)],
        );
      }
    }
  }

  private async reconcile(
    customerId: string,
    eventId?: string,
  ): Promise<boolean> {
    return await this.connection.transaction(async (connection) => {
      const [billing] = await connection.query(
        "SELECT * FROM organization_stripe_billing WHERE environment=$1 AND customer_id=$2 FOR UPDATE",
        [this.provider.environment, customerId],
      );
      if (!billing) return false;
      if (eventId) {
        const [event] = await connection.query(
          "SELECT status FROM stripe_event_inbox WHERE environment=$1 AND event_id=$2 FOR UPDATE",
          [this.provider.environment, eventId],
        );
        if (event.status === "processed") return true;
      }
      const subscriptions = await this.provider.subscriptions(customerId);
      const organizationId = Number(billing.organization_id);
      const prefix = `${this.provider.environment}:${customerId}:`;
      await connection.query(
        "UPDATE organization_access_grants SET status='revoked' WHERE organization_id=$1 AND source='stripe' AND starts_with(source_id,$2)",
        [organizationId, prefix],
      );
      let paidUntil: string | null = null;
      for (const subscription of subscriptions) {
        if (
          this.provider.environment === "live" && subscription.backerId &&
          /^\d+$/u.test(subscription.backerId)
        ) {
          await connection.query(
            "UPDATE backer_records SET discount_subscription_id=$3,discount_ended=discount_ended OR $4 WHERE id=$1 AND organization_id=$2 AND (discount_subscription_id IS NULL OR discount_subscription_id=$3)",
            [
              Number(subscription.backerId),
              organizationId,
              subscription.id,
              ["canceled", "incomplete_expired"].includes(subscription.status),
            ],
          );
        }
        if (!subscription.startsAt || !subscription.endsAt) continue;
        if (
          !paidUntil || Date.parse(subscription.endsAt) > Date.parse(paidUntil)
        ) paidUntil = subscription.endsAt;
        if (this.provider.environment === "live") {
          await connection.query(
            `INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,status,starts_at,ends_at)
           VALUES ($1,'stripe',$2,1,'fixed_term','active',$3,$4)
           ON CONFLICT(source,source_id) DO UPDATE SET status='active',starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at`,
            [
              organizationId,
              `${prefix}${subscription.id}`,
              subscription.startsAt,
              subscription.endsAt,
            ],
          );
        }
      }
      const status = subscriptions.find((subscription) =>
        !["canceled", "incomplete_expired"].includes(subscription.status)
      )?.status ?? subscriptions[0]?.status ?? "none";
      await connection.query(
        `UPDATE organization_stripe_billing SET subscription_status=$3,paid_until=$4,reconciled_at=CURRENT_TIMESTAMP
         WHERE organization_id=$1 AND environment=$2`,
        [organizationId, this.provider.environment, status, paidUntil],
      );
      if (eventId) {
        await connection.query(
          `UPDATE stripe_event_inbox SET status='processed',processed_at=CURRENT_TIMESTAMP,last_error=NULL,
         customer_id=$3,attempts=attempts+1 WHERE environment=$1 AND event_id=$2`,
          [this.provider.environment, eventId, customerId],
        );
      }
      await connection.query(
        `INSERT INTO organization_billing_audit(organization_id,action,reason)
         VALUES ($1,'stripe.reconciled','Current Stripe paid invoices reconciled')`,
        [organizationId],
      );
      return true;
    });
  }

  private checkoutUrl(value: string | null): string {
    if (!value || !URL.canParse(value)) {
      throw new Error("Checkout URL unavailable");
    }
    const url = new URL(value);
    if (
      url.protocol !== "https:" || url.hostname !== "checkout.stripe.com" ||
      url.username || url.password
    ) throw new Error("Invalid Checkout URL");
    return value;
  }
}
