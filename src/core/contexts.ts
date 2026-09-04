export type ActorType = "system" | "operator" | "provider";

function positiveInteger(value: unknown, field: string): number {
  const parsed = typeof value === "number"
    ? value
    : typeof value === "string" && value.trim()
    ? Number(value)
    : Number.NaN;
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new TypeError(`${field} must be a positive integer`);
  }
  return parsed;
}

export class TenantContext {
  readonly communityId: number;
  readonly installationId: number | null;

  constructor(communityId: unknown, installationId: unknown = null) {
    this.communityId = positiveInteger(communityId, "community_id");
    this.installationId = installationId === null || installationId === ""
      ? null
      : positiveInteger(installationId, "installation_id");
    Object.freeze(this);
  }

  static require(
    communityId: unknown,
    options: { installationId?: unknown } = {},
  ): TenantContext {
    if (
      communityId === null || communityId === undefined || communityId === ""
    ) {
      throw new TypeError("community_id is required");
    }
    return new TenantContext(communityId, options.installationId ?? null);
  }
}

export class ActorAttribution {
  readonly actorType: ActorType;
  readonly actorId: number | null;

  constructor(actorType: string, actorId: unknown = null) {
    const normalizedType = actorType.trim().toLocaleLowerCase();
    if (
      normalizedType !== "system" && normalizedType !== "operator" &&
      normalizedType !== "provider"
    ) {
      throw new TypeError("actor_type must be system, operator, or provider");
    }
    if (normalizedType === "operator" && actorId === null) {
      throw new TypeError("operator actor attribution requires actor_id");
    }
    this.actorType = normalizedType;
    this.actorId = actorId === null
      ? null
      : positiveInteger(actorId, "actor_id");
    Object.freeze(this);
  }
}
