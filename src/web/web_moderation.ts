import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { ModerationWorkspace } from "../../components/ModerationWorkspace.tsx";
import type { ModerationService, ReviewResolution } from "../domain/moderation.ts";
import type { ModerationWorkQuery } from "../domain/moderation.ts";
import { constantTimeEqual, type DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";

type ListKind = "reviews" | "actions" | "rules";

export class WebModerationController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly service: ModerationService,
  ) {}

  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request, "moderation.queues.read");
    if (session instanceof Response) return session;
    const params = new URL(request.url).searchParams;
    const query: ModerationWorkQuery = {
      queue: params.get("queue") ?? "unassigned",
      search: params.get("search") ?? "",
      severity: params.get("severity") ?? "",
      rule: params.get("rule") ?? "",
      platform: params.get("platform") ?? "",
      startAt: params.get("start_at") ?? "",
      endAt: params.get("end_at") ?? "",
      assignment: params.get("assignment") ?? "",
      page: Number(params.get("page") ?? 1),
    };
    const [snapshot, work] = await Promise.all([
      this.service.snapshot(session.communityId!, Number(session.userId)),
      this.service.listWork(
        session.communityId!,
        Number(session.userId),
        query,
      ),
    ]);
    return new Response(
      `<!doctype html>${
        render(h(ModerationWorkspace, { snapshot, work, query }))
      }`,
      {
        headers: { "content-type": "text/html; charset=utf-8" },
      },
    );
  }

  async list(request: Request, kind: ListKind): Promise<Response> {
    const session = await this.authorize(request, "moderation.queues.read");
    if (session instanceof Response) return session;
    return Response.json({
      items: (await this.service.snapshot(
        session.communityId!,
        Number(session.userId),
      ))[kind],
    });
  }

  async resolveReview(
    request: Request,
    reviewId: number,
    json: boolean,
  ): Promise<Response> {
    const session = await this.authorize(request, "moderation.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const payload = json
        ? await request.json() as Record<string, unknown>
        : Object.fromEntries(await request.formData());
      const actionType =
        String(payload.action_type ?? "").trim().toLocaleLowerCase() || null;
      if (
        actionType === "ban" &&
        !constantTimeEqual(
          String(payload.confirmation ?? "").trim(),
          "PERMANENT BAN",
        )
      ) {
        throw new TypeError("Permanent ban confirmation required");
      }
      const input: ReviewResolution = {
        communityId: session.communityId!,
        operatorId: Number(session.userId),
        reviewId,
        resolution: String(payload.resolution ?? ""),
        note: String(payload.note ?? ""),
        actionType,
        durationSeconds: Number(payload.duration_seconds ?? 600),
      };
      const actionId = await this.service.resolveReview(input);
      return json
        ? Response.json({
          status: "resolved",
          review_id: reviewId,
          action_id: actionId,
        })
        : new Response(null, {
          status: 302,
          headers: { location: "/moderation?status=Review+resolved" },
        });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return json
        ? Response.json({ error: message }, { status: 400 })
        : new Response(null, {
          status: 302,
          headers: {
            location: `/moderation?status=${encodeURIComponent(message)}`,
          },
        });
    }
  }

  async bulk(request: Request): Promise<Response> {
    const session = await this.authorize(request, "moderation.bulk");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const payload = await request.json() as Record<string, unknown>;
      const targets = payload.target_platform_account_ids;
      if (!Array.isArray(targets)) {
        throw new TypeError("target_platform_account_ids must be a list");
      }
      const actionType = String(payload.action_type ?? "").trim()
        .toLocaleLowerCase();
      const dryRun = payload.dry_run === undefined
        ? true
        : Boolean(payload.dry_run);
      if (!dryRun) {
        const label = actionType === "ban"
          ? "PERMANENT BAN"
          : actionType.toLocaleUpperCase();
        const expected = `BULK ${label} ${new Set(targets.map(Number)).size}`;
        if (!constantTimeEqual(String(payload.confirmation ?? ""), expected)) {
          throw new TypeError(`confirmation must be ${expected}`);
        }
      }
      return Response.json(
        await this.service.bulk({
          communityId: session.communityId!,
          operatorId: Number(session.userId),
          targetPlatformAccountIds: targets.map(Number),
          actionType,
          reason: String(payload.reason ?? ""),
          durationSeconds: Number(payload.duration_seconds ?? 600),
          dryRun,
        }),
      );
    } catch (error) {
      return Response.json({
        error: error instanceof Error ? error.message : String(error),
      }, { status: 409 });
    }
  }

  async bulkForm(request: Request): Promise<Response> {
    const session = await this.authorize(request, "moderation.bulk");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      const targets = [
        ...new Set(
          String(form.get("target_platform_account_ids") ?? "").split(",").map((
            value,
          ) => Number(value.trim())).filter(Number.isInteger),
        ),
      ];
      const actionType = String(form.get("action_type") ?? "").trim()
        .toLocaleLowerCase();
      const label = actionType === "ban"
        ? "PERMANENT BAN"
        : actionType.toLocaleUpperCase();
      const expected = `BULK ${label} ${targets.length}`;
      if (
        !constantTimeEqual(String(form.get("confirmation") ?? ""), expected)
      ) throw new TypeError(`confirmation must be ${expected}`);
      await this.service.bulk({
        communityId: session.communityId!,
        operatorId: Number(session.userId),
        targetPlatformAccountIds: targets,
        actionType,
        reason: String(form.get("reason") ?? ""),
        durationSeconds: Number(form.get("duration_seconds") ?? 600),
        dryRun: false,
      });
      return this.redirect("/moderation?status=Bulk+action+queued");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async userAction(request: Request, userId: number): Promise<Response> {
    const session = await this.authorize(request, "moderation.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    const form = await request.formData();
    const actionType = String(form.get("action_type") ?? "").trim()
      .toLocaleLowerCase();
    const reason = String(form.get("reason") ?? "").trim();
    const targetPlatformAccountId = Number(
      form.get("target_platform_account_id"),
    );
    let status: string;
    try {
      if (
        actionType === "ban" && !constantTimeEqual(
          String(form.get("confirmation") ?? "").trim(),
          "PERMANENT BAN",
        )
      ) throw new TypeError("Permanent ban confirmation required");
      if (!Number.isSafeInteger(targetPlatformAccountId)) {
        throw new TypeError("Invalid platform account");
      }
      const recorded = await this.service.recordUserAction({
        communityId: session.communityId!,
        operatorId: Number(session.userId),
        userId,
        targetPlatformAccountId,
        actionType,
        reason,
      });
      status = recorded
        ? `Moderation action ${actionType} recorded`
        : "Platform account does not belong to this user";
    } catch (error) {
      status = error instanceof Error ? error.message : String(error);
    }
    return new Response(null, {
      status: 302,
      headers: {
        location: `/users/${userId}?mod_status=${encodeURIComponent(status)}`,
      },
    });
  }

  async assign(
    request: Request,
    workType: string,
    itemId: number,
  ): Promise<Response> {
    const session = await this.authorize(request, "moderation.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      await this.service.assign(
        session.communityId!,
        Number(session.userId),
        workType,
        itemId,
      );
      return this.redirect("/moderation?queue=mine&status=Work+assigned");
    } catch (error) {
      return this.redirect(
        `/moderation?status=${
          encodeURIComponent(
            error instanceof Error ? error.message : String(error),
          )
        }`,
      );
    }
  }

  async resolveMember(
    request: Request,
    queueType: string,
    itemId: number,
  ): Promise<Response> {
    const session = await this.authorize(request, "appeals.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.service.resolveMember(
        session.communityId!,
        Number(session.userId),
        queueType,
        itemId,
        String(form.get("resolution") ?? ""),
        String(form.get("note") ?? ""),
      );
      return this.redirect(
        `/moderation?status=${
          queueType === "appeal" ? "Appeal" : "Report"
        }+resolved`,
      );
    } catch (error) {
      return this.redirect(
        `/moderation?status=${
          encodeURIComponent(
            error instanceof Error ? error.message : String(error),
          )
        }`,
      );
    }
  }

  async ruleDraft(request: Request): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.service.createRuleDraft(
        session.communityId!,
        Number(session.userId),
        {
          name: String(form.get("name") ?? ""),
          rule_type: String(form.get("rule_type") ?? ""),
          pattern: String(form.get("pattern") ?? ""),
          severity: String(form.get("severity") ?? ""),
          auto_enforce_action: String(form.get("auto_enforce_action") ?? "") ||
            null,
          action_duration_seconds: Number(
            form.get("action_duration_seconds") ?? 600,
          ),
          platform_scope: form.getAll("platform_scope").map(String),
        },
      );
      return this.redirect("/moderation?status=Rule+draft+created");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async ruleSave(request: Request, json: boolean): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    try {
      const payload = json
        ? await request.json() as Record<string, unknown>
        : Object.fromEntries(await request.formData());
      const scopes = Array.isArray(payload.platform_scope)
        ? payload.platform_scope.map(String)
        : ["discord", "twitch"];
      const ruleId = await this.service.saveRule(
        session.communityId!,
        Number(session.userId),
        {
          name: String(payload.name ?? ""),
          rule_type: String(payload.rule_type ?? ""),
          pattern: String(payload.pattern ?? ""),
          severity: String(payload.severity ?? ""),
          auto_enforce_action: String(payload.auto_enforce_action ?? "") ||
            null,
          action_duration_seconds: Number(
            payload.action_duration_seconds ?? 600,
          ),
          platform_scope: scopes,
        },
        payload.enabled === undefined ? true : Boolean(payload.enabled),
        String(payload.enforcement_mode ?? "shadow"),
      );
      return json
        ? Response.json({ status: "saved", rule_id: ruleId })
        : this.redirect("/moderation?status=Rule+saved");
    } catch (error) {
      if (!json) return this.errorRedirect(error);
      return Response.json({
        error: error instanceof Error ? error.message : String(error),
      }, { status: 400 });
    }
  }

  async rulePreview(request: Request, versionId: number): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      const impact = await this.service.previewRule(
        session.communityId!,
        versionId,
        String(form.get("samples") ?? "").split(/\r?\n/u),
      );
      return this.redirect(
        `/moderation?status=${
          encodeURIComponent(
            `Sample impact: ${impact.match_count} of ${impact.sample_count} matched`,
          )
        }`,
      );
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async rulePublish(request: Request, versionId: number): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      await this.service.publishRule(
        session.communityId!,
        Number(session.userId),
        versionId,
        String(form.get("lifecycle_state") ?? "shadow"),
      );
      return this.redirect("/moderation?status=Rule+published");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async ruleRollback(request: Request, versionId: number): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      await this.service.rollbackRule(
        session.communityId!,
        Number(session.userId),
        versionId,
      );
      return this.redirect("/moderation?status=Rule+rolled+back");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async ruleExemption(request: Request, ruleId: number): Promise<Response> {
    const session = await this.authorize(request, "rules.manage");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      const effectiveRuleId = ruleId > 0 ? ruleId : Number(form.get("rule_id"));
      await this.service.addRuleExemption(
        session.communityId!,
        Number(session.userId),
        effectiveRuleId,
        String(form.get("exemption_type") ?? ""),
        String(form.get("exemption_value") ?? ""),
        String(form.get("reason") ?? ""),
      );
      return this.redirect("/moderation?status=Rule+exemption+added");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  async saveFilter(request: Request): Promise<Response> {
    const session = await this.authorize(request, "moderation.queues.read");
    if (session instanceof Response) return session;
    if (!this.validOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      const parsed = JSON.parse(String(form.get("filters") ?? "{}"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new TypeError("filters must be an object");
      }
      await this.service.saveFilter(
        session.communityId!,
        Number(session.userId),
        String(form.get("name") ?? ""),
        parsed as Readonly<Record<string, unknown>>,
      );
      return this.redirect("/moderation?status=Filter+saved");
    } catch (error) {
      return this.errorRedirect(error);
    }
  }

  private errorRedirect(error: unknown): Response {
    return this.redirect(
      `/moderation?status=${
        encodeURIComponent(
          error instanceof Error ? error.message : String(error),
        )
      }`,
    );
  }

  private redirect(location: string): Response {
    return new Response(null, { status: 302, headers: { location } });
  }

  private async authorize(
    request: Request,
    capability: string,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (session.communityId === null || !roleAllows(session.role, capability)) {
      return new Response("Forbidden", { status: 403 });
    }
    return session;
  }

  private validOrigin(request: Request): boolean {
    const origin = request.headers.get("origin")?.replace(/\/$/u, "");
    return !origin || origin === new URL(request.url).origin;
  }
}
