import { assertEquals } from "jsr:@std/assert@1.0.14";

type FrozenRoute = {
  readonly handler: string;
  readonly methods: readonly string[];
  readonly paths: {
    readonly exact?: readonly string[];
    readonly prefix?: readonly string[];
    readonly suffix?: readonly string[];
  };
};

const dynamicPaths: Readonly<Record<string, readonly string[]>> = {
  _serve_intelligence_case: ["/intelligence/cases/:caseId"],
  _serve_intelligence_case_action: ["/intelligence/cases/:caseId/action"],
  _serve_intelligence_alert_case: ["/intelligence/alerts/:alertId/case"],
  _serve_intelligence_alert_disposition: [
    "/intelligence/alerts/:alertId/disposition",
  ],
  _serve_intelligence_alert_workflow: [
    "/intelligence/alerts/:alertId/workflow",
  ],
  _serve_user_moderation_action: ["/users/:userId/moderation"],
  _serve_user_lifecycle_export: ["/users/:userId/lifecycle.csv"],
  _serve_user_messages: ["/users/:userId"],
  _serve_moderation_work_assign: ["/moderation/work/:workType/:itemId/assign"],
  _serve_moderation_review_resolve: ["/moderation/reviews/:reviewId/resolve"],
  _serve_member_queue_resolve: [
    "/moderation/reports/:itemId/resolve",
    "/moderation/appeals/:itemId/resolve",
  ],
  _serve_moderation_rule_preview: [
    "/moderation/rule-versions/:versionId/preview",
  ],
  _serve_moderation_rule_publish: [
    "/moderation/rule-versions/:versionId/publish",
  ],
  _serve_moderation_rule_rollback: [
    "/moderation/rule-versions/:versionId/rollback",
  ],
  _serve_moderation_rule_exemption: ["/moderation/rules/:ruleId/exemptions"],
  _serve_onboarding_resource_delete: [
    "/onboarding/resources/:resourceId/delete",
  ],
  _serve_announcement_approve: ["/announcements/:announcementId/:action"],
  _serve_announcement_cancel: ["/announcements/:announcementId/:action"],
  _serve_announcement_retry: ["/announcements/:announcementId/:action"],
  _serve_api_conversation_context: ["/api/observations/:observationId/context"],
  _serve_live_ops_incident_action: [
    "/api/live-ops/incidents/:incidentId/:action",
  ],
  _serve_live_ops_playbook: ["/api/live-ops/playbooks/:playbookKey/activate"],
  _serve_api_observation_pivots: ["/api/observations/:observationId/pivots"],
  _serve_api_identity_review: ["/api/identity-suggestions/:suggestionId"],
  _serve_operator_access_action: ["/api/operators/:entityId/:action"],
  _serve_api_integration_revoke: ["/api/integrations/:installationId/revoke"],
  _serve_api_intelligence_report: ["/api/intelligence/reports/:reportId"],
  _serve_api_case_export: ["/api/intelligence/cases/:caseId/export"],
  _serve_api_case: ["/api/intelligence/cases/:caseId"],
  _serve_api_alert_workflow: ["/api/intelligence/alerts/:alertId"],
  _serve_api_user_detail: ["/api/users/:userId"],
  _serve_api_add_note: ["/api/users/:userId/notes"],
  _serve_api_review_resolve: ["/api/moderation/reviews/:reviewId/resolve"],
};

Deno.test("every frozen route has a Fresh method and path registration", async () => {
  const manifest = JSON.parse(
    await Deno.readTextFile(
      new URL("./fixtures/contracts/manifest.json", import.meta.url),
    ),
  ) as { readonly http_routes: readonly FrozenRoute[] };
  const source = await Deno.readTextFile(
    new URL("../main.ts", import.meta.url),
  );
  const registrations = new Set(
    [...source.matchAll(/\.(get|post|put|patch|delete)\(\s*"([^"]+)"/gs)]
      .map((match) => `${match[1].toUpperCase()} ${match[2]}`),
  );
  registrations.add("GET /");

  const missing: string[] = [];
  for (const route of manifest.http_routes) {
    const paths = route.paths.exact ?? dynamicPaths[route.handler];
    if (!paths?.length) {
      missing.push(`${route.handler}: no Fresh path mapping`);
      continue;
    }
    for (const method of route.methods) {
      if (!paths.some((path) => registrations.has(`${method} ${path}`))) {
        missing.push(`${method} ${route.handler}: ${paths.join(" or ")}`);
      }
    }
  }
  assertEquals(missing, []);
});
