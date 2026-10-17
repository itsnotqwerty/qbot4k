import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { AccountWorkspace } from "../../components/AccountWorkspace.tsx";
import { OrganizationSignupConflict } from "../data/billing_repository.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import type { DashboardSession } from "../security/security.ts";
import type { WebAuthController } from "./web_auth.ts";
import { dashboardDocument } from "./web_document.ts";

export interface CustomerAccountService {
  createOrganization(
    operatorId: number,
    name: string,
    slug: string,
    expectedSessionVersion: number,
  ): Promise<Readonly<{ organizationId: number; communityId: number }>>;
}

function redirect(location: string): Response {
  return new Response(null, {
    status: 303,
    headers: { location, "cache-control": "no-store" },
  });
}

async function signupForm(request: Request): Promise<URLSearchParams> {
  const reader = request.body?.getReader();
  if (!reader) return new URLSearchParams();
  const decoder = new TextDecoder();
  let size = 0;
  let body = "";
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > 4_096) {
        await reader.cancel();
        throw new RangeError("Organization form is too large.");
      }
      body += decoder.decode(chunk.value, { stream: true });
    }
    return new URLSearchParams(body + decoder.decode());
  } finally {
    reader.releaseLock();
  }
}

export class WebAccountsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: CustomerAccountService,
    private readonly signupEnabled: boolean,
  ) {}

  async page(request: Request): Promise<Response> {
    const session = await this.auth.accountSession(request);
    if (!session) return redirect("/login");
    if (session.communityId !== null) return redirect("/dashboard");
    return this.document(session);
  }

  async create(request: Request): Promise<Response> {
    const session = await this.auth.accountSession(request);
    if (!session) return redirect("/login");
    if (!isAllowedSameSiteOrigin(request)) {
      return this.document(session, 403, "The request origin does not match.");
    }
    if (!this.signupEnabled) return this.document(session, 403);
    if (session.communityId !== null) {
      return this.document(
        session,
        409,
        "This account already has community access.",
      );
    }
    if (
      request.headers.get("content-type")?.split(";")[0].trim() !==
        "application/x-www-form-urlencoded"
    ) {
      return this.document(session, 415, "Unsupported form format.");
    }
    let name = "";
    let slug = "";
    try {
      const form = await signupForm(request);
      name = (form.get("name") ?? "").trim();
      slug = (form.get("slug") ?? "").trim();
      if (
        !name || name.length > 120 || slug.length > 80 ||
        !/^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(slug)
      ) {
        return this.document(
          session,
          400,
          "Enter a name and a lowercase slug using letters, numbers, and hyphens.",
          name.slice(0, 120),
          slug.slice(0, 80),
        );
      }
      await this.service.createOrganization(
        Number(session.userId),
        name,
        slug,
        session.sessionVersion,
      );
      return redirect("/login");
    } catch (error) {
      if (error instanceof RangeError) {
        return this.document(session, 413, error.message);
      }
      if (error instanceof OrganizationSignupConflict) {
        return this.document(session, 409, error.message, name, slug);
      }
      if (
        error && typeof error === "object" && "code" in error &&
        error.code === "23505"
      ) {
        return this.document(
          session,
          409,
          "That organization slug is already in use.",
          name,
          slug,
        );
      }
      return this.document(
        session,
        503,
        "Organization creation is unavailable. Try again later.",
        name,
        slug,
      );
    }
  }

  private document(
    session: DashboardSession,
    status = 200,
    error = "",
    name = "",
    slug = "",
  ): Response {
    return new Response(
      dashboardDocument(
        render(h(AccountWorkspace, {
          username: session.username,
          enabled: this.signupEnabled,
          error,
          name,
          slug,
        })),
        "Account | QBot4K",
      ),
      {
        status,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
          "referrer-policy": "same-origin",
        },
      },
    );
  }
}
