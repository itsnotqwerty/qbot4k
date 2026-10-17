import type { DatabaseConnection, DatabaseRow } from "./database.ts";
import { PostgresBillingRepository } from "./billing_repository.ts";
import { BillingRequestError } from "./stripe_billing_repository.ts";
import {
  type BackerRecord,
  type CampaignMapping,
  DEFAULT_IMPORT_COLUMNS,
  type ImportColumns,
  parseBackerImport,
  rewardEligible,
  validateCampaign,
} from "../domain/backer_rewards.ts";
import { kickstarterAccessEnd } from "../domain/billing.ts";

export interface RewardDelivery {
  readonly ready: boolean;
  verifySender(): Promise<void>;
  claimCode(nonce: string): Promise<string>;
  send(email: string, code: string, key: string): Promise<void>;
}
export interface RewardPayments {
  readonly environment: "test" | "live";
  prepareReward(customerId: string, key: string): Promise<string | null>;
  removeBackerDiscount(customerId: string): Promise<void>;
}
export interface BackerService {
  activate(actor: number, campaign: string): Promise<void>;
  previewDetail(
    actor: number,
    importId: string,
  ): Promise<
    {
      campaign: string;
      confirmed: boolean;
      rows: readonly DatabaseRow[];
      count: number;
    }
  >;
  overview(
    actor: number,
    campaign: string,
    page: number,
  ): Promise<
    {
      campaigns: readonly DatabaseRow[];
      imports: readonly DatabaseRow[];
      backers: readonly DatabaseRow[];
      audit: readonly DatabaseRow[];
    }
  >;
  campaign(
    actor: number,
    mapping: CampaignMapping,
    columns: ImportColumns,
    live: boolean,
  ): Promise<void>;
  preview(actor: number, campaign: string, csv: string): Promise<string>;
  confirm(actor: number, importId: string): Promise<void>;
  resend(actor: number, backerId: number): Promise<void>;
  retry(actor: number, backerId: number): Promise<void>;
  claim(
    actor: number,
    sessionVersion: number,
    organizationId: number,
    code: string,
  ): Promise<void>;
  benefits(actor: number): Promise<readonly DatabaseRow[]>;
  process(): Promise<void>;
}

export async function claimDigest(value: string): Promise<string> {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
    ),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}

export class PostgresBackerRepository implements BackerService {
  constructor(
    private readonly connection: DatabaseConnection,
    private readonly delivery: RewardDelivery,
    private readonly payments: RewardPayments | null,
  ) {}

  private async staff(
    connection: DatabaseConnection,
    actor: number,
  ): Promise<void> {
    if (
      !await new PostgresBillingRepository(connection).platformAllows(
        actor,
        "staff.manage",
      )
    ) throw new BillingRequestError(403, "Platform admin access required.");
  }

  async overview(actor: number, campaign: string, page: number) {
    await this.staff(this.connection, actor);
    if (!Number.isSafeInteger(page) || page < 1 || page > 100_000) {
      throw new BillingRequestError(400, "Invalid page.");
    }
    const campaigns = await this.connection.query(
      "SELECT id,live_enabled,created_at FROM backer_campaigns ORDER BY created_at DESC LIMIT 100",
    );
    const imports = await this.connection.query(
      "SELECT id,digest,created_at,confirmed_at,json_array_length(records_json::json) AS backers FROM backer_imports WHERE campaign_id=$1 ORDER BY created_at DESC LIMIT 20",
      [campaign],
    );
    const backers = await this.connection.query(
      "SELECT id,backer_id,organization_id,fulfillment_status,discount_percent,access_starts_at,access_ends_at,mail_pending,mail_sent_at,attempts,last_error FROM backer_records WHERE campaign_id=$1 ORDER BY id LIMIT 51 OFFSET $2",
      [campaign, (page - 1) * 50],
    );
    const audit = await this.connection.query(
      "SELECT action,reason,operator_id,created_at FROM backer_audit WHERE campaign_id=$1 ORDER BY id DESC LIMIT 20",
      [campaign],
    );
    return { campaigns, imports, backers, audit };
  }

  async campaign(
    actor: number,
    mapping: CampaignMapping,
    columns: ImportColumns = DEFAULT_IMPORT_COLUMNS,
    live = false,
  ): Promise<void> {
    validateCampaign(mapping);
    if (
      live && (!this.delivery.ready || this.payments?.environment !== "live")
    ) {
      throw new BillingRequestError(
        409,
        "Verified claim sender and live Stripe are required before campaign activation.",
      );
    }
    if (
      Object.keys(DEFAULT_IMPORT_COLUMNS).some((key) =>
        typeof columns[key as keyof ImportColumns] !== "string" ||
        !columns[key as keyof ImportColumns].trim()
      )
    ) throw new BillingRequestError(400, "All CSV columns must be mapped.");
    await this.connection.transaction(async (connection) => {
      await this.staff(connection, actor);
      await connection.query(
        "INSERT INTO backer_campaigns(id,mapping_json,columns_json,live_enabled,created_by) VALUES ($1,$2,$3,$4,$5)",
        [
          mapping.campaignId,
          JSON.stringify(mapping),
          JSON.stringify(columns),
          live,
          actor,
        ],
      );
      if (live) await this.delivery.verifySender();
      await this.audit(
        connection,
        mapping.campaignId,
        actor,
        "campaign.created",
        live
          ? "Verified collected campaign enabled explicitly"
          : "Campaign created with live fulfillment disabled",
      );
    });
  }

  async activate(actor: number, campaign: string): Promise<void> {
    await this.staff(this.connection, actor);
    if (!this.delivery.ready || this.payments?.environment !== "live") {
      throw new BillingRequestError(
        409,
        "Verified claim sender and live Stripe are required before campaign activation.",
      );
    }
    await this.connection.transaction(async (connection) => {
      await this.staff(connection, actor);
      await this.delivery.verifySender();
      const [row] = await connection.query(
        "UPDATE backer_campaigns SET live_enabled=TRUE WHERE id=$1 RETURNING id",
        [campaign],
      );
      if (!row) throw new BillingRequestError(404, "Campaign not found.");
      await this.audit(
        connection,
        campaign,
        actor,
        "campaign.activated",
        "Staff confirmed successful collection and sender readiness",
      );
    });
  }

  async previewDetail(actor: number, importId: string) {
    await this.staff(this.connection, actor);
    const [preview] = await this.connection.query(
      "SELECT campaign_id,confirmed_at,records_json FROM backer_imports WHERE id=$1",
      [importId],
    );
    if (!preview) throw new BillingRequestError(404, "Import not found.");
    const records: BackerRecord[] = JSON.parse(String(preview.records_json));
    return {
      campaign: String(preview.campaign_id),
      confirmed: Boolean(preview.confirmed_at),
      count: records.length,
      rows: records.map((record) => ({
        backer: record.backerId,
        reward: record.reward,
        collected_cents: record.collectedCents,
        refunded_cents: record.refundedCents,
        status: record.collectionStatus,
        eligible: rewardEligible(record),
        discount: record.discountPercent,
      })),
    };
  }

  async preview(actor: number, campaign: string, csv: string): Promise<string> {
    await this.staff(this.connection, actor);
    const [row] = await this.connection.query(
      "SELECT mapping_json,columns_json FROM backer_campaigns WHERE id=$1",
      [campaign],
    );
    if (!row) throw new BillingRequestError(404, "Campaign not found.");
    const records = parseBackerImport(
      csv,
      JSON.parse(String(row.mapping_json)),
      JSON.parse(String(row.columns_json)),
    );
    const serialized = JSON.stringify(records);
    const digest = await claimDigest(serialized);
    const [preview] = await this.connection.query(
      "INSERT INTO backer_imports(id,campaign_id,digest,records_json,created_by) VALUES ($1,$2,$3,$4,$5) ON CONFLICT(campaign_id,digest) DO UPDATE SET digest=EXCLUDED.digest RETURNING id",
      [crypto.randomUUID(), campaign, digest, serialized, actor],
    );
    return String(preview.id);
  }

  async confirm(actor: number, importId: string): Promise<void> {
    await this.connection.transaction(async (connection) => {
      await this.staff(connection, actor);
      const [preview] = await connection.query(
        "SELECT * FROM backer_imports WHERE id=$1 FOR UPDATE",
        [importId],
      );
      if (!preview) throw new BillingRequestError(404, "Import not found.");
      if (preview.confirmed_at) return;
      await connection.query(
        "SELECT id FROM backer_campaigns WHERE id=$1 FOR UPDATE",
        [String(preview.campaign_id)],
      );
      const [later] = await connection.query(
        "SELECT id FROM backer_imports WHERE campaign_id=$1 AND confirmed_at IS NOT NULL AND created_at>(SELECT created_at FROM backer_imports WHERE id=$2) LIMIT 1",
        [String(preview.campaign_id), importId],
      );
      if (later) {
        throw new BillingRequestError(
          409,
          "A newer import is already confirmed. Create a fresh export.",
        );
      }
      const records: BackerRecord[] = JSON.parse(String(preview.records_json));
      for (const record of records) {
        const [existing] = await connection.query(
          "SELECT id,record_json,organization_id FROM backer_records WHERE campaign_id=$1 AND backer_id=$2 FOR UPDATE",
          [String(preview.campaign_id), record.backerId],
        );
        if (existing) {
          const old: BackerRecord = JSON.parse(String(existing.record_json));
          if (old.email !== record.email || old.rewardId !== record.rewardId) {
            throw new BillingRequestError(
              409,
              `Backer ${record.backerId} identity or reward changed; manual review required.`,
            );
          }
          if (JSON.stringify(old) === JSON.stringify(record)) continue;
          if (!rewardEligible(record)) {
            await connection.query(
              "UPDATE organization_access_grants SET status='revoked' WHERE source='kickstarter' AND source_id=$1",
              [`backer:${existing.id}`],
            );
          }
          await connection.query(
            "UPDATE backer_records SET record_json=$2,import_id=$3,discount_percent=$4,fulfillment_status=CASE WHEN organization_id IS NULL THEN 'unclaimed' ELSE 'pending' END,claim_nonce=gen_random_uuid()::text,claim_hash=NULL,claim_expires_at=NULL,mail_pending=CASE WHEN organization_id IS NULL THEN $5 ELSE FALSE END,next_attempt_at=CURRENT_TIMESTAMP,last_error=NULL WHERE id=$1",
            [
              Number(existing.id),
              JSON.stringify(record),
              importId,
              record.discountPercent,
              rewardEligible(record),
            ],
          );
        } else {
          await connection.query(
            "INSERT INTO backer_records(campaign_id,backer_id,record_json,import_id,discount_percent,mail_pending) VALUES ($1,$2,$3,$4,$5,$6)",
            [
              String(preview.campaign_id),
              record.backerId,
              JSON.stringify(record),
              importId,
              record.discountPercent,
              rewardEligible(record),
            ],
          );
        }
      }
      await connection.query(
        "UPDATE backer_imports SET confirmed_at=CURRENT_TIMESTAMP,confirmed_by=$2 WHERE id=$1",
        [importId, actor],
      );
      await this.audit(
        connection,
        String(preview.campaign_id),
        actor,
        "import.confirmed",
        `${records.length} verified rows; missing backers unchanged`,
      );
    });
  }

  async resend(actor: number, backerId: number): Promise<void> {
    await this.connection.transaction(async (connection) => {
      await this.staff(connection, actor);
      const [backer] = await connection.query(
        "SELECT * FROM backer_records WHERE id=$1 FOR UPDATE",
        [backerId],
      );
      if (
        !backer || backer.organization_id ||
        !rewardEligible(JSON.parse(String(backer.record_json)))
      ) {
        throw new BillingRequestError(
          409,
          "An eligible unclaimed backer is required.",
        );
      }
      await connection.query(
        "UPDATE backer_records SET mail_pending=TRUE,claim_nonce=gen_random_uuid()::text,claim_hash=NULL,claim_expires_at=NULL,next_attempt_at=CURRENT_TIMESTAMP WHERE id=$1",
        [backerId],
      );
      await this.audit(
        connection,
        String(backer.campaign_id),
        actor,
        "claim.email.queued",
        `Backer record ${backerId}`,
      );
    });
  }

  async retry(actor: number, backerId: number): Promise<void> {
    await this.connection.transaction(async (connection) => {
      await this.staff(connection, actor);
      const [backer] = await connection.query(
        "UPDATE backer_records SET next_attempt_at=CURRENT_TIMESTAMP,fulfillment_status='pending' WHERE id=$1 AND organization_id IS NOT NULL AND fulfillment_status='failed' RETURNING campaign_id",
        [backerId],
      );
      if (!backer) {
        throw new BillingRequestError(409, "No failed fulfillment to retry.");
      }
      await this.audit(
        connection,
        String(backer.campaign_id),
        actor,
        "fulfillment.retry",
        `Backer record ${backerId}`,
      );
    });
  }

  async claim(
    actor: number,
    sessionVersion: number,
    organizationId: number,
    code: string,
  ): Promise<void> {
    const [attempt] = await this.connection.query(
      "INSERT INTO backer_claim_attempts(operator_id,attempts) VALUES ($1,1) ON CONFLICT(operator_id) DO UPDATE SET attempts=CASE WHEN backer_claim_attempts.window_start<CURRENT_TIMESTAMP-INTERVAL '15 minutes' THEN 1 ELSE backer_claim_attempts.attempts+1 END,window_start=CASE WHEN backer_claim_attempts.window_start<CURRENT_TIMESTAMP-INTERVAL '15 minutes' THEN CURRENT_TIMESTAMP ELSE backer_claim_attempts.window_start END RETURNING attempts",
      [actor],
    );
    if (Number(attempt.attempts) > 10) {
      throw new BillingRequestError(
        429,
        "Too many claim attempts. Try again in 15 minutes.",
      );
    }
    if (!/^[a-f0-9]{64}$/u.test(code)) {
      throw new BillingRequestError(400, "Invalid or expired claim code.");
    }
    const digest = await claimDigest(code);
    await this.connection.transaction(async (connection) => {
      await connection.query(
        "SELECT pg_advisory_xact_lock(hashtextextended('qbot4k:organization-billing:' || $1::text,0))",
        [organizationId],
      );
      await connection.query(
        "SELECT organization_id FROM organization_stripe_billing WHERE organization_id=$1 AND environment='live' FOR UPDATE",
        [organizationId],
      );
      const [owner] = await connection.query(
        "SELECT account.id FROM operator_accounts account JOIN organization_operator_roles membership ON membership.operator_id=account.id JOIN organizations organization ON organization.id=membership.organization_id WHERE account.id=$1 AND account.status='active' AND account.session_version=$2 AND membership.organization_id=$3 AND membership.role='owner' AND organization.status='active' FOR UPDATE OF account",
        [actor, sessionVersion, organizationId],
      );
      if (!owner) {
        throw new BillingRequestError(
          403,
          "A current organization-owner session is required.",
        );
      }
      const [backer] = await connection.query(
        "SELECT backer.* FROM backer_records backer JOIN backer_campaigns campaign ON campaign.id=backer.campaign_id WHERE backer.claim_hash=$1 AND backer.claim_expires_at>CURRENT_TIMESTAMP AND backer.organization_id IS NULL AND campaign.live_enabled FOR UPDATE OF backer",
        [digest],
      );
      if (!backer || !rewardEligible(JSON.parse(String(backer.record_json)))) {
        throw new BillingRequestError(400, "Invalid or expired claim code.");
      }
      if (!this.payments || this.payments.environment !== "live") {
        throw new BillingRequestError(
          503,
          "Live fulfillment is not configured.",
        );
      }
      const [claimed] = await connection.query(
        "SELECT id FROM backer_records WHERE organization_id=$1 LIMIT 1",
        [organizationId],
      );
      if (claimed) {
        throw new BillingRequestError(
          409,
          "This organization already has a claimed reward. Contact support before transferring another reward.",
        );
      }
      await connection.query(
        "UPDATE backer_records SET organization_id=$2,claimed_at=CURRENT_TIMESTAMP,claim_hash=NULL,claim_expires_at=NULL,mail_pending=FALSE,fulfillment_status='pending',next_attempt_at=CURRENT_TIMESTAMP WHERE id=$1",
        [Number(backer.id), organizationId],
      );
      await this.audit(
        connection,
        String(backer.campaign_id),
        actor,
        "reward.claimed",
        `Backer record ${backer.id} bound to organization ${organizationId}; payment transition pending`,
      );
    });
  }

  async benefits(actor: number): Promise<readonly DatabaseRow[]> {
    return await this.connection.query(
      "SELECT backer.id,backer.campaign_id,backer.organization_id,backer.fulfillment_status,backer.access_starts_at,backer.access_ends_at,backer.discount_percent,backer.discount_ended,backer.last_error FROM backer_records backer JOIN organization_operator_roles membership ON membership.organization_id=backer.organization_id JOIN operator_accounts account ON account.id=membership.operator_id WHERE account.id=$1 AND account.status='active' AND membership.role IN ('owner','billing_manager') ORDER BY backer.id DESC LIMIT 100",
      [actor],
    );
  }

  async process(): Promise<void> {
    if (!this.payments || this.payments.environment !== "live") return;
    const due = await this.connection.query(
      "SELECT backer.id FROM backer_records backer JOIN backer_campaigns campaign ON campaign.id=backer.campaign_id WHERE campaign.live_enabled AND backer.next_attempt_at<=CURRENT_TIMESTAMP AND (backer.fulfillment_status IN ('pending','failed') OR (backer.mail_pending AND $1)) ORDER BY backer.next_attempt_at LIMIT 10",
      [this.delivery.ready],
    );
    for (const row of due) {
      try {
        await this.processOne(Number(row.id));
      } catch {
        await this.connection.query(
          "UPDATE backer_records SET attempts=attempts+1,fulfillment_status=CASE WHEN organization_id IS NULL THEN fulfillment_status ELSE 'failed' END,last_error='Provider operation failed; retry scheduled',next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes' WHERE id=$1",
          [Number(row.id)],
        );
      }
    }
  }

  private async processOne(id: number): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const [snapshot] = await connection.query(
        "SELECT organization_id FROM backer_records WHERE id=$1",
        [id],
      );
      if (!snapshot) return;
      if (snapshot.organization_id) {
        await connection.query(
          "SELECT pg_advisory_xact_lock(hashtextextended('qbot4k:organization-billing:' || $1::text,0))",
          [Number(snapshot.organization_id)],
        );
      }
      const billing = snapshot.organization_id
        ? (await connection.query(
          "SELECT customer_id FROM organization_stripe_billing WHERE organization_id=$1 AND environment='live' FOR UPDATE",
          [Number(snapshot.organization_id)],
        ))[0]
        : null;
      const [backer] = await connection.query(
        "SELECT backer.* FROM backer_records backer JOIN backer_campaigns campaign ON campaign.id=backer.campaign_id WHERE backer.id=$1 AND campaign.live_enabled AND backer.next_attempt_at<=CURRENT_TIMESTAMP FOR UPDATE OF backer SKIP LOCKED",
        [id],
      );
      if (!backer || backer.organization_id !== snapshot.organization_id) {
        return;
      }
      const record: BackerRecord = JSON.parse(String(backer.record_json));
      if (!backer.organization_id) {
        if (
          !backer.mail_pending || !this.delivery.ready ||
          !rewardEligible(record)
        ) return;
        const code = await this.delivery.claimCode(String(backer.claim_nonce));
        const digest = await claimDigest(code);
        await this.delivery.send(record.email, code, digest);
        await connection.query(
          "UPDATE backer_records SET claim_hash=$2,claim_expires_at=CURRENT_TIMESTAMP+INTERVAL '7 days',mail_pending=FALSE,mail_sent_at=CURRENT_TIMESTAMP,last_error=NULL WHERE id=$1",
          [id, digest],
        );
        await this.audit(
          connection,
          String(backer.campaign_id),
          null,
          "claim.email.sent",
          `Backer record ${id}`,
        );
        return;
      }
      if (!["pending", "failed"].includes(String(backer.fulfillment_status))) {
        return;
      }
      const organizationId = Number(backer.organization_id);
      let startsAt = backer.access_starts_at
        ? new Date(String(backer.access_starts_at)).toISOString()
        : null;
      const eligible = rewardEligible(record);
      if (!eligible) {
        await connection.query(
          "UPDATE organization_access_grants SET status='revoked' WHERE source='kickstarter' AND source_id=$1",
          [`backer:${id}`],
        );
        if (billing?.customer_id && record.discountPercent !== 20) {
          await this.payments!.removeBackerDiscount(
            String(billing.customer_id),
          );
        }
      } else {
        if (!startsAt) {
          const paidUntil = billing?.customer_id
            ? await this.payments!.prepareReward(
              String(billing.customer_id),
              `backer:${id}`,
            )
            : null;
          startsAt = new Date(
            Math.max(
              Date.now(),
              record.reward !== "lifetime" && paidUntil
                ? Date.parse(paidUntil)
                : 0,
            ),
          ).toISOString();
        }
        const endsAt = backer.access_starts_at
          ? (backer.access_ends_at
            ? new Date(String(backer.access_ends_at)).toISOString()
            : null)
          : kickstarterAccessEnd(record.reward, startsAt);
        await connection.query(
          "INSERT INTO organization_access_grants(organization_id,source,source_id,catalog_version,kind,status,starts_at,ends_at) VALUES ($1,'kickstarter',$2,1,$3,'active',$4,$5) ON CONFLICT(source,source_id) DO UPDATE SET status='active'",
          [
            organizationId,
            `backer:${id}`,
            record.reward === "lifetime" ? "lifetime" : "fixed_term",
            startsAt,
            endsAt,
          ],
        );
        await connection.query(
          "UPDATE backer_records SET access_starts_at=$2,access_ends_at=$3 WHERE id=$1",
          [id, startsAt, endsAt],
        );
      }
      if (
        eligible && billing?.customer_id && record.discountPercent !== 20 &&
        backer.discount_subscription_id
      ) await this.payments!.removeBackerDiscount(String(billing.customer_id));
      await connection.query(
        "UPDATE backer_records SET fulfillment_status=$2,attempts=attempts+1,last_error=NULL WHERE id=$1",
        [id, eligible ? "fulfilled" : "revoked"],
      );
      await this.audit(
        connection,
        String(backer.campaign_id),
        null,
        eligible ? "reward.fulfilled" : "reward.revoked",
        `Backer record ${id}; organization ${organizationId}`,
      );
    });
  }

  private async audit(
    connection: DatabaseConnection,
    campaign: string,
    actor: number | null,
    action: string,
    reason: string,
  ): Promise<void> {
    await connection.query(
      "INSERT INTO backer_audit(campaign_id,operator_id,action,reason) VALUES ($1,$2,$3,$4)",
      [campaign, actor, action, reason],
    );
  }
}
