import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { RewardsWorkspace } from "../../components/RewardsWorkspace.tsx";
import type { BackerService } from "../data/backer_repository.ts";
import type { StripeBillingService } from "../data/stripe_billing_repository.ts";
import { BillingRequestError } from "../data/stripe_billing_repository.ts";
import type { KickstarterRewardKey } from "../domain/billing.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import type { WebAuthController } from "./web_auth.ts";
import { dashboardDocument } from "./web_document.ts";
import { stringify } from "jsr:@std/csv@1.0.6";

const headers = {
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
};
export class WebRewardsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: BackerService,
    private readonly billing: StripeBillingService | null,
    private readonly emailReady: boolean,
  ) {}

  async export(request: Request): Promise<Response> {
    const session = await this.auth.platformSession(request);
    if (!session) {
      return new Response("Access denied.", { status: 403, headers });
    }
    try {
      const query = new URL(request.url).searchParams;
      const { backers } = await this.service.overview(
        Number(session.userId),
        query.get("campaign") ?? "",
        Number(query.get("page") ?? 1),
      );
      const columns = [
        "id",
        "backer_id",
        "organization_id",
        "fulfillment_status",
        "discount_percent",
        "access_starts_at",
        "access_ends_at",
        "mail_pending",
        "mail_sent_at",
        "attempts",
        "last_error",
      ];
      const records = backers.slice(0, 50).map((row) =>
        columns.map((column) => {
          const value = String(row[column] ?? "");
          return /^[\s]*[=+@-]/u.test(value) || /^[\t\r\n]/u.test(value)
            ? `'${value}`
            : value;
        })
      );
      return new Response(stringify([columns, ...records]), {
        headers: {
          ...headers,
          "content-type": "text/csv; charset=utf-8",
          "content-disposition":
            "attachment; filename=reward-reconciliation-page.csv",
        },
      });
    } catch (error) {
      return this.failure(error);
    }
  }

  async page(request: Request, admin = false): Promise<Response> {
    const session = admin
      ? await this.auth.platformSession(request)
      : await this.auth.accountSession(request);
    if (!session) {
      return new Response("Authentication and appropriate access required.", {
        status: 403,
        headers,
      });
    }
    try {
      const query = new URL(request.url).searchParams;
      const campaign = query.get("campaign") ?? "";
      const page = Number(query.get("page") ?? 1);
      const previewId = query.get("preview") ?? "";
      const content = admin
        ? {
          overview: await this.service.overview(
            Number(session.userId),
            campaign,
            page,
          ),
          preview: previewId
            ? await this.service.previewDetail(
              Number(session.userId),
              previewId,
            )
            : undefined,
          campaign,
          page,
          previewId,
          emailReady: this.emailReady,
        }
        : {
          benefits: await this.service.benefits(Number(session.userId)),
          organizations:
            await this.billing?.organizations(Number(session.userId)) ?? [],
        };
      return new Response(
        dashboardDocument(
          render(h(RewardsWorkspace, { admin, ...content })),
          admin ? "Backer fulfillment | QBot4K" : "Rewards | QBot4K",
        ),
        { headers: { ...headers, "content-type": "text/html; charset=utf-8" } },
      );
    } catch (error) {
      return this.failure(error);
    }
  }

  async action(request: Request, action: string): Promise<Response> {
    const session = action === "claim"
      ? await this.auth.accountSession(request)
      : await this.auth.platformSession(request);
    if (!session) {
      return new Response("Access denied.", { status: 403, headers });
    }
    if (!isAllowedSameSiteOrigin(request)) {
      return new Response("Forbidden", { status: 403, headers });
    }
    try {
      const reader = request.body?.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      if (reader) {
        try {
          while (true) {
            const item = await reader.read();
            if (item.done) break;
            size += item.value.length;
            if (size > 2_200_000) {
              await reader.cancel();
              throw new BillingRequestError(413, "Upload exceeds size limit.");
            }
            chunks.push(item.value);
          }
        } finally {
          reader.releaseLock();
        }
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
      const form = await new Response(bytes, {
        headers: { "content-type": request.headers.get("content-type") ?? "" },
      }).formData();
      const value = (key: string) => {
        const entry = form.get(key);
        return typeof entry === "string" ? entry : "";
      };
      const actor = Number(session.userId);
      let location = "/admin/rewards?campaign=" +
        encodeURIComponent(value("campaign_id"));
      if (action === "campaign") {
        const rewards: Record<string, KickstarterRewardKey> = {};
        for (
          const reward of [
            "threeMonths",
            "sixMonths",
            "oneYear",
            "lifetime",
          ] as const
        ) {
          const id = value(reward).trim();
          if (id) {
            if (Object.hasOwn(rewards, id)) {
              throw new BillingRequestError(400, "Duplicate reward ID.");
            }
            rewards[id] = reward;
          }
        }
        await this.service.campaign(
          actor,
          { campaignId: value("campaign_id"), rewards },
          JSON.parse(value("columns")),
          false,
        );
      } else if (action === "preview") {
        const file = form.get("csv");
        if (!(file instanceof File)) {
          throw new BillingRequestError(400, "CSV file required.");
        }
        const id = await this.service.preview(
          actor,
          value("campaign_id"),
          await file.text(),
        );
        location += "&preview=" + encodeURIComponent(id);
      } else if (
        action === "confirm" || action === "activate" || action === "claim"
      ) {
        if (value("consent") !== "confirmed") {
          throw new BillingRequestError(400, "Explicit confirmation required.");
        }
        if (action === "confirm") {
          await this.service.confirm(actor, value("import_id"));
        } else if (action === "activate") {
          await this.service.activate(actor, value("campaign_id"));
        } else {
          const organizationId = Number(value("organization_id"));
          if (!Number.isSafeInteger(organizationId) || organizationId < 1) {
            throw new BillingRequestError(400, "Invalid organization.");
          }
          await this.service.claim(
            actor,
            session.sessionVersion,
            organizationId,
            value("code").trim(),
          );
          location = "/billing/rewards";
        }
      } else if (action === "recovery") {
        const id = Number(value("backer_id"));
        if (!Number.isSafeInteger(id) || id < 1) {
          throw new BillingRequestError(400, "Invalid backer record ID.");
        }
        if (value("action") === "resend") await this.service.resend(actor, id);
        else if (value("action") === "retry") {
          await this.service.retry(actor, id);
        } else throw new BillingRequestError(400, "Invalid recovery action.");
      } else throw new BillingRequestError(404, "Action not found.");
      return new Response(null, {
        status: 303,
        headers: { ...headers, location },
      });
    } catch (error) {
      return this.failure(error);
    }
  }

  private failure(error: unknown): Response {
    const message = error instanceof BillingRequestError
      ? error.message
      : error instanceof TypeError || error instanceof SyntaxError
      ? "Invalid import or form. Check the mapped columns and verified row values."
      : "Operation unavailable. No unconfirmed reward has been granted.";
    return new Response(message, {
      status: error instanceof BillingRequestError
        ? error.status
        : error instanceof TypeError || error instanceof SyntaxError
        ? 400
        : 503,
      headers,
    });
  }
}
