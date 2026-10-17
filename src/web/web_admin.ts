import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { AdminWorkspace } from "../../components/AdminWorkspace.tsx";
import type { AdminView } from "../data/billing_repository.ts";
import type { DatabaseRow } from "../data/database.ts";
import type { WebAuthController } from "./web_auth.ts";
import { dashboardDocument } from "./web_document.ts";

export interface PlatformAdminService {
  rows(
    operatorId: number,
    view: AdminView,
    page: number,
  ): Promise<readonly DatabaseRow[] | null>;
}

export class WebAdminController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: PlatformAdminService,
  ) {}

  async page(request: Request): Promise<Response> {
    const headers = {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
    };
    const session = await this.auth.platformSession(request);
    if (!session) {
      return new Response("Platform admin access required.", {
        status: 403,
        headers,
      });
    }
    const query = new URL(request.url).searchParams;
    const view = query.get("view") ?? "organizations";
    const page = Number(query.get("page") ?? 1);
    if (
      (view !== "organizations" && view !== "accounts" && view !== "audit") ||
      !Number.isSafeInteger(page) || page < 1 || page > 100_000
    ) {
      return new Response("Invalid admin view or page.", {
        status: 400,
        headers,
      });
    }
    const rows = await this.service.rows(Number(session.userId), view, page);
    if (!rows) {
      return new Response("Platform admin access required.", {
        status: 403,
        headers,
      });
    }
    return new Response(
      dashboardDocument(
        render(h(AdminWorkspace, { view, page, rows })),
        "Platform admin | QBot4K",
      ),
      {
        headers: { ...headers, "content-type": "text/html; charset=utf-8" },
      },
    );
  }
}
