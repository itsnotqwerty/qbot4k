export const DASHBOARD_CAPABILITIES = Object.freeze(
  [
    "dashboard.access",
    "community.read",
    "members.read",
    "moderation.queues.read",
    "moderation.manage",
    "moderation.bulk",
    "rules.manage",
    "appeals.manage",
    "evidence.sensitive.read",
    "alerts.read",
    "alerts.manage",
    "cases.manage",
    "intelligence.read",
    "analytics.read",
    "analytics.export",
    "exports.create",
    "announcements.manage",
    "integrations.manage",
    "settings.manage",
    "operators.manage",
    "audit.read",
    "live_ops.read",
    "live_ops.manage",
  ] as const,
);

const ROLE_PERMISSIONS: Readonly<Record<string, ReadonlySet<string>>> = {
  viewer: new Set([
    "dashboard.access",
    "community.read",
    "members.read",
    "alerts.read",
    "analytics.read",
    "live_ops.read",
  ]),
  analyst: new Set([
    "dashboard.access",
    "community.read",
    "members.read",
    "moderation.queues.read",
    "alerts.read",
    "alerts.manage",
    "cases.manage",
    "intelligence.read",
    "analytics.read",
    "exports.create",
    "live_ops.read",
  ]),
  moderator: new Set([
    "dashboard.access",
    "community.read",
    "members.read",
    "moderation.queues.read",
    "moderation.manage",
    "rules.manage",
    "appeals.manage",
    "evidence.sensitive.read",
    "alerts.read",
    "alerts.manage",
    "cases.manage",
    "intelligence.read",
    "analytics.read",
    "exports.create",
    "live_ops.read",
    "live_ops.manage",
  ]),
  admin: new Set(["*"]),
  owner: new Set(["*"]),
};

const PLATFORM_CAPABILITIES: Readonly<Record<string, ReadonlySet<string>>> = {
  discord: new Set([
    "events",
    "moderation_actions",
    "member_lifecycle",
    "announcements",
  ]),
  twitch: new Set([
    "events",
    "moderation_actions",
    "member_lifecycle",
    "announcements",
    "live_controls",
  ]),
};

export function roleAllows(role: string, capability: string): boolean {
  const permissions = ROLE_PERMISSIONS[role.toLocaleLowerCase()];
  return permissions?.has("*") === true ||
    permissions?.has(capability) === true;
}

export function permissionDecision(
  role: string | null,
  capability: string,
  override?: "grant" | "deny" | null,
): boolean {
  if (override !== undefined && override !== null) return override === "grant";
  return role !== null && roleAllows(role, capability);
}

export function platformCapabilities(platform: string): readonly string[] {
  return Object.freeze([
    ...(PLATFORM_CAPABILITIES[platform.trim().toLocaleLowerCase()] ?? []),
  ]);
}
