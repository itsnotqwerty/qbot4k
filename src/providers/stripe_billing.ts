import Stripe from "npm:stripe@17.7.0";
import { BILLING_CATALOG } from "../domain/billing.ts";

export type BillingCadence = "monthly" | "annual";
export type StripeEnvironment = "test" | "live";

export interface StripeBillingSettings {
  readonly stripeSecretKey: string | null;
  readonly stripeWebhookSecret: string | null;
  readonly stripePriceMonthly: string | null;
  readonly stripePriceAnnual: string | null;
  readonly stripeBackerCouponId?: string | null;
  readonly stripePortalConfigurationId?: string | null;
}

export interface BillingEvent {
  readonly id: string;
  readonly type: string;
  readonly customerId: string | null;
  readonly livemode: boolean;
  readonly chargeId?: string | null;
}

export interface CheckoutSession {
  readonly id: string;
  readonly url: string | null;
  readonly status: string | null;
  readonly expiresAt: number;
}

export interface PaidSubscription {
  readonly id: string;
  readonly status: string;
  readonly startsAt: string | null;
  readonly endsAt: string | null;
  readonly backerId?: string | null;
}

export interface StripeBillingGateway {
  readonly environment: StripeEnvironment;
  verify(body: string, signature: string): Promise<BillingEvent>;
  customerForCharge(chargeId: string): Promise<string | null>;
  validatePrice(cadence: BillingCadence): Promise<void>;
  createCustomer(organizationId: number, key: string): Promise<string>;
  createCheckout(
    customerId: string,
    cadence: BillingCadence,
    origin: string,
    organizationId: number,
    key: string,
    backerId?: string,
  ): Promise<CheckoutSession>;
  portal?(customerId: string, origin: string): Promise<string>;
  checkout(id: string): Promise<CheckoutSession>;
  subscriptions(customerId: string): Promise<readonly PaidSubscription[]>;
}

export class StripeBillingProvider implements StripeBillingGateway {
  readonly environment: StripeEnvironment;
  private readonly stripe: Stripe;

  constructor(
    private readonly settings: StripeBillingSettings,
    fetcher: typeof fetch = fetch,
  ) {
    if (
      !settings.stripeSecretKey || !settings.stripeWebhookSecret ||
      !settings.stripePriceMonthly || !settings.stripePriceAnnual
    ) {
      throw new TypeError(
        "Stripe API key, webhook secret and both prices are required",
      );
    }
    this.environment = settings.stripeSecretKey.split("_")[1] === "live"
      ? "live"
      : "test";
    this.stripe = new Stripe(settings.stripeSecretKey, {
      apiVersion: "2025-02-24.acacia",
      httpClient: Stripe.createFetchHttpClient(fetcher),
      maxNetworkRetries: 2,
      timeout: 10_000,
    });
  }

  async verify(body: string, signature: string): Promise<BillingEvent> {
    const event = await this.stripe.webhooks.constructEventAsync(
      body,
      signature,
      this.settings.stripeWebhookSecret!,
      300,
      Stripe.createSubtleCryptoProvider(),
    );
    const object = event.data.object as unknown as {
      customer?: string | { id: string } | null;
      charge?: string | { id: string } | null;
    };
    return {
      id: event.id,
      type: event.type,
      customerId: typeof object.customer === "string"
        ? object.customer
        : object.customer?.id ?? null,
      livemode: event.livemode,
      chargeId: typeof object.charge === "string"
        ? object.charge
        : object.charge?.id ?? null,
    };
  }

  async customerForCharge(chargeId: string): Promise<string | null> {
    const charge = await this.stripe.charges.retrieve(chargeId);
    return typeof charge.customer === "string"
      ? charge.customer
      : charge.customer?.id ?? null;
  }

  async validatePrice(cadence: BillingCadence): Promise<void> {
    const price = await this.stripe.prices.retrieve(this.priceId(cadence));
    const offer = BILLING_CATALOG.subscriptions[cadence];
    if (
      !price.active || price.livemode !== (this.environment === "live") ||
      price.currency !== "usd" || price.unit_amount !== offer.amountCents ||
      price.type !== "recurring" ||
      price.recurring?.interval !==
        (cadence === "monthly" ? "month" : "year") ||
      price.recurring.interval_count !== 1 ||
      price.recurring.usage_type !== "licensed" ||
      price.billing_scheme !== "per_unit"
    ) {
      throw new TypeError(
        "Stripe price does not match the approved subscription catalog",
      );
    }
  }

  async createCustomer(organizationId: number, key: string): Promise<string> {
    return (await this.stripe.customers.create({
      metadata: { qbot_organization_id: String(organizationId) },
    }, { idempotencyKey: key })).id;
  }

  async createCheckout(
    customerId: string,
    cadence: BillingCadence,
    origin: string,
    organizationId: number,
    key: string,
    backerId?: string,
  ): Promise<CheckoutSession> {
    if (backerId) await this.validateBackerCoupon();
    return this.session(
      await this.stripe.checkout.sessions.create({
        mode: "subscription",
        customer: customerId,
        client_reference_id: String(organizationId),
        line_items: [{ price: this.priceId(cadence), quantity: 1 }],
        success_url:
          `${origin}/billing?organization_id=${organizationId}&checkout=returned`,
        cancel_url: `${origin}/billing?organization_id=${organizationId}`,
        subscription_data: {
          metadata: {
            qbot_organization_id: String(organizationId),
            ...(backerId ? { qbot_backer_id: backerId } : {}),
          },
        },
        ...(backerId
          ? { discounts: [{ coupon: this.settings.stripeBackerCouponId! }] }
          : {}),
        allow_promotion_codes: false,
      }, { idempotencyKey: key }),
    );
  }

  async checkout(id: string): Promise<CheckoutSession> {
    return this.session(await this.stripe.checkout.sessions.retrieve(id));
  }

  async validateBackerCoupon(): Promise<void> {
    if (!this.settings.stripeBackerCouponId) {
      throw new Error(
        "Configure the private Kickstarter coupon in Stripe Dashboard.",
      );
    }
    const coupon = await this.stripe.coupons.retrieve(
      this.settings.stripeBackerCouponId,
    );
    if (
      !coupon.valid || coupon.livemode !== (this.environment === "live") ||
      coupon.percent_off !== 20 || coupon.duration !== "forever" ||
      coupon.amount_off !== null || coupon.max_redemptions !== null ||
      coupon.redeem_by !== null
    ) {
      throw new Error(
        "Backer coupon must be a valid unrestricted 20 percent forever coupon.",
      );
    }
    if (coupon.applies_to?.products) {
      for (const cadence of ["monthly", "annual"] as const) {
        const price = await this.stripe.prices.retrieve(this.priceId(cadence));
        const product = typeof price.product === "string"
          ? price.product
          : price.product.id;
        if (!coupon.applies_to.products.includes(product)) {
          throw new Error("Coupon excludes an approved product.");
        }
      }
    }
  }

  async portal(customerId: string, origin: string): Promise<string> {
    if (!this.settings.stripePortalConfigurationId) {
      throw new Error("Configure the Stripe customer portal first.");
    }
    const configuration = await this.stripe.billingPortal.configurations
      .retrieve(this.settings.stripePortalConfigurationId);
    const features = configuration.features;
    if (
      !configuration.active ||
      configuration.livemode !== (this.environment === "live") ||
      features.subscription_update.enabled ||
      !features.subscription_cancel.enabled ||
      features.subscription_cancel.mode !== "at_period_end" ||
      !features.payment_method_update.enabled ||
      !features.invoice_history.enabled
    ) {
      throw new Error(
        "Portal configuration does not match the supported subscription policy.",
      );
    }
    return (await this.stripe.billingPortal.sessions.create({
      customer: customerId,
      configuration: configuration.id,
      return_url: `${origin}/billing`,
    })).url;
  }

  async prepareReward(customerId: string, key: string): Promise<string | null> {
    for await (
      const session of this.stripe.checkout.sessions.list({
        customer: customerId,
        status: "open",
        limit: 100,
      })
    ) {
      await this.stripe.checkout.sessions.expire(session.id);
    }
    for await (
      const subscription of this.stripe.subscriptions.list({
        customer: customerId,
        status: "all",
        limit: 100,
      })
    ) {
      if (["canceled", "incomplete_expired"].includes(subscription.status)) {
        continue;
      }
      if (subscription.schedule) {
        throw new Error(
          "Scheduled subscription requires manual reward transition review.",
        );
      }
      const updated = await this.stripe.subscriptions.update(subscription.id, {
        cancel_at_period_end: true,
      }, { idempotencyKey: `${key}:stop-renewal:${subscription.id}` });
      if (!updated.cancel_at_period_end) {
        throw new Error("Cancellation was not confirmed.");
      }
    }
    const ends = (await this.subscriptions(customerId)).flatMap(
      (subscription) => subscription.endsAt ? [subscription.endsAt] : [],
    );
    return ends.sort().at(-1) ?? null;
  }

  async removeBackerDiscount(customerId: string): Promise<void> {
    for await (
      const session of this.stripe.checkout.sessions.list({
        customer: customerId,
        status: "open",
        limit: 100,
      })
    ) await this.stripe.checkout.sessions.expire(session.id);
    if (!this.settings.stripeBackerCouponId) {
      throw new Error(
        "Backer coupon configuration required for reconciliation.",
      );
    }
    for await (
      const subscription of this.stripe.subscriptions.list({
        customer: customerId,
        status: "all",
        limit: 100,
      })
    ) {
      if (
        subscription.discount?.coupon.id ===
          this.settings.stripeBackerCouponId &&
        !["canceled", "incomplete_expired"].includes(subscription.status)
      ) await this.stripe.subscriptions.deleteDiscount(subscription.id);
    }
  }

  async subscriptions(
    customerId: string,
  ): Promise<readonly PaidSubscription[]> {
    const subscriptions: PaidSubscription[] = [];
    for await (
      const subscription of this.stripe.subscriptions.list({
        customer: customerId,
        status: "all",
        limit: 100,
      })
    ) {
      let startsAt: string | null = null;
      let endsAt: string | null = null;
      const item = subscription.items.data[0];
      const eligible = subscription.items.data.length === 1 &&
        item.quantity === 1 &&
        [this.settings.stripePriceMonthly, this.settings.stripePriceAnnual]
          .includes(item.price.id);
      if (eligible) {
        await this.validatePrice(
          item.price.id === this.settings.stripePriceMonthly
            ? "monthly"
            : "annual",
        );
        for await (
          const invoice of this.stripe.invoices.list({
            subscription: subscription.id,
            status: "paid",
            limit: 100,
          })
        ) {
          if (!invoice.amount_paid || invoice.paid_out_of_band) continue;
          const chargeId = typeof invoice.charge === "string"
            ? invoice.charge
            : invoice.charge?.id;
          if (!chargeId) continue;
          const charge = await this.stripe.charges.retrieve(chargeId);
          if (
            !charge.paid || charge.refunded || charge.disputed ||
            charge.amount_refunded > 0
          ) continue;
          for await (
            const line of this.stripe.invoices.listLineItems(invoice.id!, {
              limit: 100,
            })
          ) {
            if (
              line.type !== "subscription" || line.proration ||
              line.price?.id !== item.price.id || line.quantity !== 1 ||
              line.amount <= 0
            ) continue;
            if (!endsAt || line.period.end * 1000 > Date.parse(endsAt)) {
              startsAt = new Date(line.period.start * 1000).toISOString();
              endsAt = new Date(line.period.end * 1000).toISOString();
            }
          }
        }
      }
      subscriptions.push({
        id: subscription.id,
        status: subscription.status,
        startsAt,
        endsAt,
        ...(subscription.metadata?.qbot_backer_id
          ? { backerId: subscription.metadata.qbot_backer_id }
          : {}),
      });
    }
    return subscriptions;
  }

  private priceId(cadence: BillingCadence): string {
    return (cadence === "monthly"
      ? this.settings.stripePriceMonthly
      : this.settings.stripePriceAnnual)!;
  }

  private session(session: Stripe.Checkout.Session): CheckoutSession {
    return {
      id: session.id,
      url: session.url,
      status: session.status,
      expiresAt: session.expires_at,
    };
  }
}
