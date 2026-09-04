import type { AppSettings } from "../core/config.ts";
import type { RoleHealthMonitor } from "../core/health.ts";
import {
  createOauthState,
  createSessionCookie,
  type DashboardSession,
  parseSessionCookie,
  verifyOauthState,
} from "../security/security.ts";

export interface DiscordIdentity {
  readonly userId: string;
  readonly username: string;
  readonly guildIds: readonly string[];
  readonly permissions: Readonly<Record<string, string>>;
}

export interface OperatorMembership {
  readonly id: number;
  readonly name: string;
  readonly slug: string;
  readonly role: string;
}

export interface OperatorLogin {
  readonly operatorId: number;
  readonly status: string;
  readonly sessionVersion: number;
  readonly memberships: readonly OperatorMembership[];
}

export interface OperatorAuthStore {
  completeLogin(
    identity: DiscordIdentity,
    role: string,
  ): Promise<OperatorLogin>;
  switchCommunity(
    operatorId: number,
    communityId: number,
    previousCommunityId: number | null,
  ): Promise<string | null>;
  auditLogout(operatorId: number): Promise<void>;
  resolveSession(operatorId: number): Promise<
    {
      readonly status: string;
      readonly sessionVersion: number;
      readonly memberships: readonly OperatorMembership[];
    } | null
  >;
}

export interface DiscordOAuthProvider {
  authenticate(code: string, redirectUri: string): Promise<DiscordIdentity>;
}

type AuthSettings = Pick<
  AppSettings,
  | "dashboardSessionSecret"
  | "discordOauthClientId"
  | "discordOauthClientSecret"
  | "discordOauthRedirectUri"
  | "operatorGuildIds"
>;

const text = (body: string, status: number): Response =>
  new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });

function cookieValue(request: Request, name: string): string | null {
  for (const part of (request.headers.get("cookie") ?? "").split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) return value.join("=");
  }
  return null;
}

function requestOrigin(request: Request): string {
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]
    ?.trim();
  const forwardedHost = request.headers.get("x-forwarded-host")?.split(",")[0]
    ?.trim();
  const url = new URL(request.url);
  return `${forwardedProto || url.protocol.slice(0, -1)}://${
    forwardedHost || url.host
  }`;
}

function redirect(location: string, cookies: readonly string[] = []): Response {
  const headers = new Headers({ location });
  for (const cookie of cookies) headers.append("set-cookie", cookie);
  return new Response(null, { status: 302, headers });
}

function isSecure(request: Request, redirectUri: string | null): boolean {
  return request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim()
        .toLocaleLowerCase() === "https" ||
    redirectUri?.toLocaleLowerCase().startsWith("https://") === true;
}

function sessionCookie(value: string, secure: boolean): string {
  return `qbot4k_session=${value}; Path=/; HttpOnly; SameSite=Lax${
    secure ? "; Secure" : ""
  }`;
}

function clearedCookie(name: string, secure: boolean): string {
  return `${name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax${
    secure ? "; Secure" : ""
  }`;
}

export function determineOperatorRole(
  identity: DiscordIdentity,
  operatorGuildIds: readonly string[],
): string | null {
  const allowed = new Set(
    operatorGuildIds.map((id) => id.trim()).filter(Boolean),
  );
  for (const guildId of identity.guildIds) {
    if (!allowed.has(guildId)) continue;
    try {
      return (BigInt(identity.permissions[guildId] ?? "0") & 8n) !== 0n
        ? "admin"
        : "moderator";
    } catch {
      return "moderator";
    }
  }
  return null;
}

export class WebAuthController {
  constructor(
    private readonly settings: AuthSettings,
    private readonly provider: DiscordOAuthProvider,
    private readonly store: OperatorAuthStore,
    private readonly now: () => Date = () => new Date(),
  ) {}

  async login(request: Request): Promise<Response> {
    const { dashboardSessionSecret: secret, discordOauthClientId: clientId } =
      this.settings;
    if (!secret || !clientId) {
      return text("Discord OAuth is not configured", 503);
    }
    const state = await createOauthState(secret);
    const redirectUri = this.redirectUri(request);
    const query = new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: "identify guilds",
      state,
      prompt: "consent",
    });
    const secure = isSecure(request, this.settings.discordOauthRedirectUri);
    return redirect(
      `https://discord.com/oauth2/authorize?${query}`,
      [`qbot4k_oauth_state=${state}; Path=/; HttpOnly; SameSite=Lax${
        secure ? "; Secure" : ""
      }`],
    );
  }

  async callback(request: Request): Promise<Response> {
    const secret = this.settings.dashboardSessionSecret;
    if (!secret) return text("Session secret is not configured", 503);
    const url = new URL(request.url);
    const code = url.searchParams.get("code")?.trim() ?? "";
    const state = url.searchParams.get("state")?.trim() ?? "";
    const cookieState = cookieValue(request, "qbot4k_oauth_state") ?? "";
    if (
      !state ||
      (state !== cookieState && !await verifyOauthState(secret, state))
    ) {
      return text("Invalid OAuth state", 400);
    }
    if (!code) return text("Missing OAuth code", 400);
    if (
      !this.settings.discordOauthClientId ||
      !this.settings.discordOauthClientSecret
    ) {
      return text("Discord OAuth is not configured", 503);
    }
    try {
      const identity = await this.provider.authenticate(
        code,
        this.redirectUri(request),
      );
      const role = determineOperatorRole(
        identity,
        this.settings.operatorGuildIds,
      );
      if (!role) {
        return text("You are not authorized to access the dashboard", 403);
      }
      const login = await this.store.completeLogin(identity, role);
      if (login.status !== "active") {
        return text("Operator access is disabled", 403);
      }
      const active = login.memberships[0];
      const session: DashboardSession = {
        userId: String(login.operatorId),
        username: identity.username,
        role: active?.role ?? role,
        expiresAt: new Date(this.now().valueOf() + 12 * 60 * 60_000)
          .toISOString(),
        communityId: active?.id ?? null,
        sessionVersion: login.sessionVersion,
      };
      const secure = isSecure(request, this.settings.discordOauthRedirectUri);
      return redirect("/dashboard", [
        sessionCookie(await createSessionCookie(secret, session), secure),
        clearedCookie("qbot4k_oauth_state", secure),
      ]);
    } catch {
      return text("Discord OAuth failed", 502);
    }
  }

  async logout(request: Request): Promise<Response> {
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    const session = await this.readSession(request);
    if (session && /^\d+$/u.test(session.userId)) {
      await this.store.auditLogout(Number(session.userId));
    }
    return redirect("/login", [
      clearedCookie(
        "qbot4k_session",
        isSecure(request, this.settings.discordOauthRedirectUri),
      ),
    ]);
  }

  async dashboard(request: Request): Promise<Response> {
    const session = await this.authorizedSession(request);
    if (!session) return redirect("/login");
    const state = await this.store.resolveSession(Number(session.userId));
    const memberships = state?.memberships ?? [];
    const options = memberships.map((membership) =>
      `<option value="${membership.id}"${
        membership.id === session.communityId ? " selected" : ""
      }>${escapeHtml(membership.name)}</option>`
    ).join("");
    return new Response(
      `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>QBot4K dashboard</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/dashboard">Overview</a><a href="/system-health">System status</a></nav></header><main class="page-content"><section class="hero"><p class="eyebrow">Overview</p><h1>QBot4K dashboard</h1><p class="lede">Signed in as ${
        escapeHtml(session.username)
      }.</p></section><section class="runtime-panel"><div><p class="section-label">Active community</p><h2>Community workspace</h2></div><form method="post" action="/community/switch"><label for="community_id">Community</label><select id="community_id" name="community_id">${options}</select><button type="submit">Switch</button></form></section><form class="logout-form" method="post" action="/logout"><button type="submit">Logout</button></form></main></div></body></html>`,
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }

  async systemHealth(
    request: Request,
    monitor?: RoleHealthMonitor,
  ): Promise<Response> {
    const session = await this.authorizedSession(request);
    if (!session) return redirect("/login");
    const snapshot = monitor
      ? await monitor.snapshot()
      : { status: "ready", dependencies: { configuration: "ready" } };
    const dependencies = Object.entries(snapshot.dependencies).map(
      ([name, status]) =>
        `<tr><th scope="row">${escapeHtml(name)}</th><td>${
          escapeHtml(String(status))
        }</td></tr>`,
    ).join("");
    return new Response(
      `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>System health | QBot4K</title><link rel="stylesheet" href="/styles.css"></head><body><div class="app-shell"><header class="site-header"><a class="brand" href="/">QBot4K</a><nav aria-label="Primary navigation"><a href="/dashboard">Overview</a><a href="/system-health">System status</a></nav></header><main class="page-content"><section class="hero"><p class="eyebrow">Operations</p><h1>System health</h1><p class="lede">Current runtime status: ${
        escapeHtml(snapshot.status)
      }.</p></section><table><caption>Runtime dependencies</caption><tbody>${dependencies}</tbody></table></main></div></body></html>`,
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }

  async switchCommunity(request: Request): Promise<Response> {
    if (!this.validOrigin(request)) {
      return Response.json({ error: "origin_mismatch" }, { status: 403 });
    }
    const session = await this.readSession(request);
    if (!session || !/^\d+$/u.test(session.userId)) return redirect("/login");
    const form = await request.formData();
    const communityId = Number(form.get("community_id"));
    if (!Number.isInteger(communityId) || communityId <= 0) {
      return text("Invalid community", 400);
    }
    const role = await this.store.switchCommunity(
      Number(session.userId),
      communityId,
      session.communityId,
    );
    if (!role) return text("Community access is not available", 403);
    const secure = isSecure(request, this.settings.discordOauthRedirectUri);
    const cookie = await createSessionCookie(secretRequired(this.settings), {
      ...session,
      role,
      communityId,
    });
    return redirect("/dashboard", [sessionCookie(cookie, secure)]);
  }

  async readSession(request: Request): Promise<DashboardSession | null> {
    const secret = this.settings.dashboardSessionSecret;
    return secret
      ? await parseSessionCookie(
        secret,
        cookieValue(request, "qbot4k_session"),
        this.now(),
      )
      : null;
  }

  async authorizedSession(
    request: Request,
  ): Promise<DashboardSession | null> {
    const session = await this.readSession(request);
    if (!session || !/^\d+$/u.test(session.userId)) return null;
    const state = await this.store.resolveSession(Number(session.userId));
    if (
      !state || state.status !== "active" ||
      state.sessionVersion !== session.sessionVersion
    ) {
      return null;
    }
    if (
      session.communityId !== null &&
      !state.memberships.some((membership) =>
        membership.id === session.communityId &&
        membership.role === session.role
      )
    ) return null;
    return session;
  }

  private redirectUri(request: Request): string {
    return this.settings.discordOauthRedirectUri ||
      `${requestOrigin(request)}/oauth/discord/callback`;
  }

  private validOrigin(request: Request): boolean {
    const origin = request.headers.get("origin")?.replace(/\/$/u, "");
    return !origin || origin === requestOrigin(request);
  }
}

function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function secretRequired(settings: AuthSettings): string {
  if (!settings.dashboardSessionSecret) {
    throw new TypeError("Session secret is not configured");
  }
  return settings.dashboardSessionSecret;
}

export function createDiscordOAuthProvider(
  settings: Pick<
    AppSettings,
    "discordOauthClientId" | "discordOauthClientSecret"
  >,
  fetcher: typeof fetch = fetch,
): DiscordOAuthProvider {
  return {
    async authenticate(code, redirectUri) {
      const tokenResponse = await fetcher(
        "https://discord.com/api/oauth2/token",
        {
          method: "POST",
          headers: {
            accept: "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "qbot4k/1.0",
          },
          body: new URLSearchParams({
            client_id: settings.discordOauthClientId ?? "",
            client_secret: settings.discordOauthClientSecret ?? "",
            grant_type: "authorization_code",
            code,
            redirect_uri: redirectUri,
          }),
        },
      );
      if (!tokenResponse.ok) {
        throw new Error(
          `Discord token exchange failed: HTTP ${tokenResponse.status}`,
        );
      }
      const tokenPayload = await tokenResponse.json() as {
        access_token?: unknown;
      };
      const token = String(tokenPayload.access_token ?? "").trim();
      if (!token) {
        throw new Error(
          "Discord OAuth token response did not include access_token",
        );
      }
      const headers = {
        authorization: `Bearer ${token}`,
        accept: "application/json",
        "user-agent": "qbot4k/1.0",
      };
      const [userResponse, guildResponse] = await Promise.all([
        fetcher("https://discord.com/api/v10/users/@me", { headers }),
        fetcher("https://discord.com/api/v10/users/@me/guilds", { headers }),
      ]);
      if (!userResponse.ok || !guildResponse.ok) {
        throw new Error("Discord identity request failed");
      }
      const user = await userResponse.json() as Record<string, unknown>;
      const guilds = await guildResponse.json() as readonly Record<
        string,
        unknown
      >[];
      const userId = String(user.id ?? "").trim();
      const username = String(user.global_name ?? user.username ?? "").trim();
      if (!userId || !username || !Array.isArray(guilds)) {
        throw new Error("Discord identity response was incomplete");
      }
      return {
        userId,
        username,
        guildIds: guilds.map((guild) => String(guild.id ?? "").trim()).filter(
          Boolean,
        ),
        permissions: Object.fromEntries(
          guilds.map((
            guild,
          ) => [
            String(guild.id ?? "").trim(),
            String(guild.permissions ?? ""),
          ]),
        ),
      };
    },
  };
}
