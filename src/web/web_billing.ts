import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { BillingWorkspace } from "../../components/BillingWorkspace.tsx";
import {
  BillingRequestError,
  type StripeBillingService,
} from "../data/stripe_billing_repository.ts";
import type { StripeBillingGateway } from "../providers/stripe_billing.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import type { WebAuthController } from "./web_auth.ts";
import { dashboardDocument } from "./web_document.ts";

async function boundedBody(request: Request, limit: number): Promise<string> {
  const reader = request.body?.getReader();
  if (!reader) return "";
  let size = 0;
  let result = "";
  const decoder = new TextDecoder("utf-8", { fatal: true });
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new BillingRequestError(413, "Request body too large.");
      }
      result += decoder.decode(chunk.value, { stream: true });
    }
    return result + decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

function response(message: string, status: number): Response {
  return new Response(message, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
    },
  });
}

export class WebBillingController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: StripeBillingService | null,
    private readonly provider: StripeBillingGateway | null,
    private readonly origin: string | null,
  ) {}

  async page(request: Request): Promise<Response> {
    const session = await this.auth.accountSession(request);
    if (!session) {
      return new Response(null, {
        status: 303,
        headers: { location: "/login", "cache-control": "no-store" },
      });
    }
    if (!this.service || !this.provider) {
      return response("Billing is not configured.", 503);
    }
    const organizations = await this.service.organizations(
      Number(session.userId),
    );
    return new Response(
      dashboardDocument(
        render(
          h(BillingWorkspace, {
            organizations,
            sandbox: this.provider.environment === "test",
            platformAdmin: Boolean(await this.auth.platformSession(request)),
          }),
        ),
        "Billing | QBot4K",
      ),
      {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  async checkout(request: Request, portal = false): Promise<Response> {
    const session = await this.auth.accountSession(request);
    if (!session) return response("Authentication required.", 401);
    if (!isAllowedSameSiteOrigin(request)) return response("Forbidden", 403);
    if (!this.service || !this.origin) {
      return response("Billing is not configured.", 503);
    }
    if (
      request.headers.get("content-type")?.split(";")[0].trim() !==
        "application/x-www-form-urlencoded"
    ) return response("Unsupported form format.", 415);
    try {
      const form = new URLSearchParams(await boundedBody(request, 4096));
      const organizationId = Number(form.get("organization_id"));
      const cadence = form.get("cadence");
      if (
        !Number.isSafeInteger(organizationId) || organizationId < 1 ||
        (!portal && cadence !== "monthly" && cadence !== "annual")
      ) return response("Invalid organization or subscription interval.", 400);
      if (portal && !this.service.portal) {
        return response("Customer portal is not configured.", 503);
      }
      const url = portal
        ? await this.service.portal!(
          Number(session.userId),
          organizationId,
          this.origin,
        )
        : await this.service.checkout(
          Number(session.userId),
          organizationId,
          cadence as "monthly" | "annual",
          this.origin,
        );
      return new Response(null, {
        status: 303,
        headers: { location: url, "cache-control": "no-store" },
      });
    } catch (error) {
      return error instanceof BillingRequestError
        ? response(error.message, error.status)
        : response(
          "Checkout is unavailable. Retry later or contact support.",
          503,
        );
    }
  }

  async webhook(request: Request): Promise<Response> {
    if (!this.service || !this.provider) {
      return response("Billing is not configured.", 503);
    }
    const signature = request.headers.get("stripe-signature");
    if (!signature) return response("Missing Stripe signature.", 400);
    let event;
    try {
      event = await this.provider.verify(
        await boundedBody(request, 1_048_576),
        signature,
      );
    } catch (error) {
      return response(
        "Invalid Stripe webhook.",
        error instanceof BillingRequestError ? error.status : 400,
      );
    }
    try {
      await this.service.receive(event);
      return response("Accepted", 200);
    } catch (error) {
      return response(
        "Webhook could not be stored.",
        error instanceof BillingRequestError ? error.status : 503,
      );
    }
  }
}
