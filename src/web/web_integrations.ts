import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { IntegrationsWorkspace } from "../../components/IntegrationsWorkspace.tsx";
import type { AppSettingsValues } from "../core/config.ts";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import { FernetCipher } from "../security/fernet.ts";
import {
  constantTimeEqual,
  isAllowedSameSiteOrigin,
} from "../security/security.ts";
import type { DashboardSession } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";

const TWITCH_SCOPES = new Set([
  "moderator:read:followers",
  "channel:read:subscriptions",
  "moderator:manage:banned_users",
  "moderator:manage:chat_settings",
  "moderator:manage:shield_mode",
  "chat:read",
  "chat:edit",
]);
type InstallState = {
  readonly operator_id: string;
  readonly community_id: number;
  readonly nonce: string;
  readonly expires_at: string;
  readonly guild_id?: string;
  readonly broadcaster_login?: string;
  readonly scopes?: readonly string[];
};
export interface IntegrationSnapshot {
  readonly community: DatabaseRow;
  readonly guilds: readonly DatabaseRow[];
  readonly installations: readonly DatabaseRow[];
}
export interface TwitchGrant {
  readonly accessToken: string;
  readonly refreshToken: string | null;
  readonly scopes: readonly string[];
  readonly broadcasterId: string;
  readonly broadcasterLogin: string;
}
export interface IntegrationOAuthGateway {
  exchangeDiscord(code: string, redirectUri: string): Promise<void>;
  exchangeTwitch(code: string, redirectUri: string): Promise<TwitchGrant>;
}
export interface TwitchEventSubGateway {
  reconcile(input: {
    communityId: number;
    installationId: number;
    grant: TwitchGrant;
    callbackUrl: string;
    secret: string;
  }): Promise<void>;
}
export interface IntegrationService {
  snapshot(
    communityId: number,
    operatorId: number,
  ): Promise<IntegrationSnapshot>;
  createDiscordIntent(
    input: {
      communityId: number;
      operatorId: number;
      guildId: string;
      pilotInviteCode: string;
      state: InstallState;
    },
  ): Promise<void>;
  completeDiscordIntent(state: InstallState): Promise<void>;
  createTwitchIntent(
    input: {
      communityId: number;
      operatorId: number;
      broadcasterLogin: string;
      scopes: readonly string[];
      state: InstallState;
    },
  ): Promise<void>;
  completeTwitchIntent(
    state: InstallState,
    grant: TwitchGrant,
    encryptionKey: string,
    eventsubConfigured: boolean,
  ): Promise<number>;
  revoke(
    communityId: number,
    operatorId: number,
    installationId: number,
  ): Promise<void>;
}

export class FetchIntegrationOAuthGateway implements IntegrationOAuthGateway {
  constructor(
    private readonly settings: Pick<
      AppSettingsValues,
      | "discordOauthClientId"
      | "discordOauthClientSecret"
      | "twitchClientId"
      | "twitchClientSecret"
    >,
  ) {}
  async exchangeDiscord(code: string, redirectUri: string): Promise<void> {
    const response = await fetch("https://discord.com/api/oauth2/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: this.settings.discordOauthClientId ?? "",
        client_secret: this.settings.discordOauthClientSecret ?? "",
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri,
      }),
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json() as {
          error?: string;
          error_description?: string;
        };
        if (body.error) {
          detail = `${body.error}: ${body.error_description ?? ""}`.trim();
        }
      } catch { /* non-JSON error body */ }
      throw new TypeError(`Discord installation failed (${detail})`);
    }
  }
  async exchangeTwitch(
    code: string,
    redirectUri: string,
  ): Promise<TwitchGrant> {
    const response = await fetch("https://id.twitch.tv/oauth2/token", {
      method: "POST",
      body: new URLSearchParams({
        client_id: this.settings.twitchClientId ?? "",
        client_secret: this.settings.twitchClientSecret ?? "",
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri,
      }),
    });
    if (!response.ok) throw new TypeError("Twitch authorization failed");
    const token = await response.json() as {
      access_token?: string;
      refresh_token?: string;
      scope?: string[];
    };
    const validationResponse = await fetch(
      "https://id.twitch.tv/oauth2/validate",
      { headers: { authorization: `OAuth ${token.access_token ?? ""}` } },
    );
    if (!validationResponse.ok) {
      throw new TypeError("Twitch token validation failed");
    }
    const validation = await validationResponse.json() as {
      user_id?: string;
      login?: string;
      scopes?: string[];
    };
    return {
      accessToken: token.access_token ?? "",
      refreshToken: token.refresh_token ?? null,
      scopes: validation.scopes ?? token.scope ?? [],
      broadcasterId: validation.user_id ?? "",
      broadcasterLogin: validation.login ?? "",
    };
  }
}

export class PostgresIntegrationRepository implements IntegrationService {
  constructor(private readonly connection: DatabaseConnection) {}
  async snapshot(
    communityId: number,
    operatorId: number,
  ): Promise<IntegrationSnapshot> {
    const community = (await this.connection.query(
      "SELECT id,name FROM communities WHERE id=$1 AND status='active'",
      [communityId],
    ))[0];
    if (!community) throw new TypeError("Community not found");
    const guilds = await this.connection.query(
      `SELECT permissions.guild_id,
              COALESCE(
                NULLIF(permissions.guild_name, ''),
                NULLIF(installation.display_name, permissions.guild_id),
                permissions.guild_id
              ) AS guild_name
         FROM operator_discord_guild_permissions AS permissions
         LEFT JOIN community_installations AS installation
           ON installation.platform='discord'
          AND installation.external_community_id=permissions.guild_id
        WHERE permissions.operator_id=$1
          AND (permissions.permissions & 40::bigint) != 0
        ORDER BY LOWER(COALESCE(
          NULLIF(permissions.guild_name, ''),
          NULLIF(installation.display_name, permissions.guild_id),
          permissions.guild_id
        )),permissions.guild_id`,
      [operatorId],
    );
    const installations = await this.connection.query(
      "SELECT id,platform,external_community_id,display_name,status,health_status,last_error,last_health_check_at FROM community_installations WHERE community_id=$1 ORDER BY platform,LOWER(display_name)",
      [communityId],
    );
    return { community, guilds, installations };
  }
  async createDiscordIntent(
    input: {
      communityId: number;
      operatorId: number;
      guildId: string;
      pilotInviteCode: string;
      state: InstallState;
    },
  ): Promise<void> {
    const guildId = input.guildId.trim();
    if (!guildId) {
      throw new TypeError("Discord installation guild is required");
    }
    await this.connection.transaction(async (connection) => {
      const allowed = (await connection.query(
        "SELECT 1 FROM operator_community_roles r JOIN operator_discord_guild_permissions g ON g.operator_id=r.operator_id WHERE r.operator_id=$1 AND r.community_id=$2 AND r.role IN ('admin','owner') AND g.guild_id=$3 AND (g.permissions & 40::bigint) != 0",
        [input.operatorId, input.communityId, guildId],
      ))[0];
      if (!allowed) {
        throw new TypeError("Discord installation is not authorized");
      }
      const existing = (await connection.query(
        "SELECT community_id FROM community_installations WHERE platform='discord' AND external_community_id=$1",
        [guildId],
      ))[0];
      if (existing && Number(existing.community_id) !== input.communityId) {
        throw new TypeError(
          "Discord guild is already linked to another community",
        );
      }
      await connection.query(
        "INSERT INTO discord_install_intents(nonce,operator_id,community_id,guild_id,expires_at) VALUES ($1,$2,$3,$4,$5)",
        [
          input.state.nonce,
          input.operatorId,
          input.communityId,
          guildId,
          input.state.expires_at,
        ],
      );
      await this.audit(
        connection,
        input.operatorId,
        "integration.discord_link_intent_created",
        "community",
        input.communityId,
        { guild_id: guildId, nonce: input.state.nonce },
      );
    });
  }
  async completeDiscordIntent(state: InstallState): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const consumed = (await connection.query(
        "UPDATE discord_install_intents SET consumed_at=CURRENT_TIMESTAMP WHERE nonce=$1 AND operator_id=$2 AND community_id=$3 AND guild_id=$4 AND consumed_at IS NULL AND expires_at::timestamptz>CURRENT_TIMESTAMP RETURNING nonce",
        [
          state.nonce,
          Number(state.operator_id),
          state.community_id,
          state.guild_id ?? "",
        ],
      ))[0];
      if (!consumed) {
        throw new TypeError("Discord installation state was already used");
      }
      const knownName = (await connection.query(
        "SELECT guild_name FROM operator_discord_guild_permissions WHERE guild_id=$1 AND guild_name IS NOT NULL AND guild_name<>'' ORDER BY updated_at DESC LIMIT 1",
        [state.guild_id ?? ""],
      ))[0];
      const displayName = String(knownName?.guild_name ?? "") ||
        (state.guild_id ?? "");
      const installation = (await connection.query(
        `INSERT INTO community_installations(community_id,platform,external_community_id,display_name,status,scopes_json,capabilities_json) VALUES ($1,'discord',$2,$3,'pending',$4,$5) ON CONFLICT(platform,external_community_id) DO UPDATE SET display_name=EXCLUDED.display_name,status=CASE WHEN community_installations.status='active' THEN 'active' ELSE 'pending' END,scopes_json=EXCLUDED.scopes_json,capabilities_json=EXCLUDED.capabilities_json,updated_at=CURRENT_TIMESTAMP RETURNING id`,
        [
          state.community_id,
          state.guild_id ?? "",
          displayName,
          JSON.stringify(["applications.commands", "bot"]),
          JSON.stringify([
            "announcements",
            "events",
            "member_lifecycle",
            "moderation_actions",
          ]),
        ],
      ))[0];
      await this.audit(
        connection,
        Number(state.operator_id),
        "integration.discord_link_pending",
        "community_installation",
        Number(installation.id),
        { community_id: state.community_id, guild_id: state.guild_id },
      );
    });
  }
  async createTwitchIntent(
    input: {
      communityId: number;
      operatorId: number;
      broadcasterLogin: string;
      scopes: readonly string[];
      state: InstallState;
    },
  ): Promise<void> {
    const login = input.broadcasterLogin.trim().toLocaleLowerCase();
    const scopes = [
      ...new Set(input.scopes.map((scope) => scope.trim()).filter(Boolean)),
    ].sort();
    if (
      !login || !scopes.length ||
      scopes.some((scope) => !TWITCH_SCOPES.has(scope))
    ) throw new TypeError("Twitch installation requested unsupported scopes");
    await this.connection.transaction(async (connection) => {
      const allowed = (await connection.query(
        "SELECT 1 FROM operator_community_roles WHERE operator_id=$1 AND community_id=$2 AND role IN ('admin','owner')",
        [input.operatorId, input.communityId],
      ))[0];
      if (!allowed) {
        throw new TypeError("Twitch installation is not authorized");
      }
      const existing = (await connection.query(
        "SELECT community_id FROM community_installations WHERE platform='twitch' AND (external_community_id=$1 OR metadata_json::jsonb->>'broadcaster_login'=$1)",
        [login],
      ))[0];
      if (existing && Number(existing.community_id) !== input.communityId) {
        throw new TypeError(
          "Twitch broadcaster is already linked to another community",
        );
      }
      await connection.query(
        "INSERT INTO twitch_install_intents(nonce,operator_id,community_id,broadcaster_login,scopes_json,expires_at) VALUES ($1,$2,$3,$4,$5,$6)",
        [
          input.state.nonce,
          input.operatorId,
          input.communityId,
          login,
          JSON.stringify(scopes),
          input.state.expires_at,
        ],
      );
      await this.audit(
        connection,
        input.operatorId,
        "integration.twitch_link_intent_created",
        "community",
        input.communityId,
        { broadcaster_login: login, nonce: input.state.nonce },
      );
    });
  }
  async completeTwitchIntent(
    state: InstallState,
    grant: TwitchGrant,
    encryptionKey: string,
    eventsubConfigured: boolean,
  ): Promise<number> {
    if (
      grant.broadcasterLogin.toLocaleLowerCase() !== state.broadcaster_login
    ) {
      throw new TypeError(
        "Twitch authorization belongs to a different broadcaster",
      );
    }
    if (!(state.scopes ?? []).every((scope) => grant.scopes.includes(scope))) {
      throw new TypeError("Twitch did not grant all reviewed scopes");
    }
    const cipher = await FernetCipher.fromKey(encryptionKey);
    return await this.connection.transaction(async (connection) => {
      const intent = (await connection.query(
        "UPDATE twitch_install_intents SET consumed_at=CURRENT_TIMESTAMP WHERE nonce=$1 AND operator_id=$2 AND community_id=$3 AND broadcaster_login=$4 AND consumed_at IS NULL AND expires_at::timestamptz>CURRENT_TIMESTAMP RETURNING nonce",
        [
          state.nonce,
          Number(state.operator_id),
          state.community_id,
          state.broadcaster_login ?? "",
        ],
      ))[0];
      if (!intent) {
        throw new TypeError(
          "Twitch installation intent is invalid, expired, or already consumed",
        );
      }
      const metadata = {
        broadcaster_id: grant.broadcasterId,
        broadcaster_login: grant.broadcasterLogin.toLocaleLowerCase(),
        moderation_mode: "shadow",
      };
      const installation = (await connection.query(
        `INSERT INTO community_installations(community_id,platform,external_community_id,display_name,status,scopes_json,metadata_json,capabilities_json,health_status,last_error) VALUES ($1,'twitch',$2,$3,'pending',$4,$5,$6,$7,$8) ON CONFLICT(platform,external_community_id) DO UPDATE SET display_name=EXCLUDED.display_name,status='pending',scopes_json=EXCLUDED.scopes_json,metadata_json=EXCLUDED.metadata_json,capabilities_json=EXCLUDED.capabilities_json,health_status=EXCLUDED.health_status,last_error=EXCLUDED.last_error,updated_at=CURRENT_TIMESTAMP RETURNING id`,
        [
          state.community_id,
          grant.broadcasterId,
          grant.broadcasterLogin.toLocaleLowerCase(),
          JSON.stringify([...grant.scopes].sort()),
          JSON.stringify(metadata),
          JSON.stringify([
            "announcements",
            "events",
            "live_controls",
            "moderation_actions",
          ]),
          eventsubConfigured ? "unknown" : "degraded",
          eventsubConfigured
            ? null
            : "EventSub callback is not configured; configure it and reconnect Twitch.",
        ],
      ))[0];
      const installationId = Number(installation.id);
      await connection.query(
        `INSERT INTO installation_runtime_leases(installation_id,owner_runtime)
         VALUES ($1,'deno') ON CONFLICT(installation_id) DO NOTHING`,
        [installationId],
      );
      const access = new TextEncoder().encode(
        await cipher.encrypt(grant.accessToken),
      );
      const refresh = grant.refreshToken
        ? new TextEncoder().encode(await cipher.encrypt(grant.refreshToken))
        : null;
      const credential = (await connection.query(
        `INSERT INTO installation_credentials(installation_id,access_token_ciphertext,refresh_token_ciphertext,scopes_json,key_version,rotation_count) VALUES ($1,$2,$3,$4,1,1) ON CONFLICT(installation_id) DO UPDATE SET access_token_ciphertext=EXCLUDED.access_token_ciphertext,refresh_token_ciphertext=EXCLUDED.refresh_token_ciphertext,scopes_json=EXCLUDED.scopes_json,rotation_count=installation_credentials.rotation_count+1,rotated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP RETURNING id`,
        [
          installationId,
          access,
          refresh,
          JSON.stringify([...grant.scopes].sort()),
        ],
      ))[0];
      await connection.query(
        "UPDATE community_installations SET token_reference=$1 WHERE id=$2 AND community_id=$3",
        [
          `installation-credential:${credential.id}`,
          installationId,
          state.community_id,
        ],
      );
      await this.audit(
        connection,
        Number(state.operator_id),
        "integration.twitch_link_pending",
        "community_installation",
        installationId,
        {
          community_id: state.community_id,
          broadcaster_login: grant.broadcasterLogin.toLocaleLowerCase(),
        },
      );
      return installationId;
    });
  }
  async revoke(
    communityId: number,
    operatorId: number,
    installationId: number,
  ): Promise<void> {
    await this.connection.transaction(async (connection) => {
      const row = (await connection.query(
        "SELECT platform,external_community_id,status FROM community_installations WHERE id=$1 AND community_id=$2 FOR UPDATE",
        [installationId, communityId],
      ))[0];
      if (!row) throw new TypeError("installation not found");
      if (row.status === "revoked") {
        throw new TypeError("installation is already revoked");
      }
      await connection.query(
        "UPDATE community_installations SET status='revoked',token_reference=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND community_id=$2",
        [installationId, communityId],
      );
      await connection.query(
        "DELETE FROM installation_credentials WHERE installation_id=$1",
        [installationId],
      );
      await this.audit(
        connection,
        operatorId,
        "integration.revoked",
        "community_installation",
        installationId,
        {
          installation_id: installationId,
          platform: row.platform,
          external_community_id: row.external_community_id,
        },
      );
    });
  }
  private async audit(
    connection: DatabaseConnection,
    operatorId: number,
    action: string,
    entityType: string,
    entityId: number,
    payload: Readonly<Record<string, unknown>>,
  ) {
    await connection.query(
      "INSERT INTO audit_log(actor_type,actor_id,action_type,entity_type,entity_id,payload_json) VALUES ('operator',$1,$2,$3,$4,$5)",
      [operatorId, action, entityType, entityId, JSON.stringify(payload)],
    );
  }
}

export class WebIntegrationsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly integrations: IntegrationService,
    private readonly gateway: IntegrationOAuthGateway,
    private readonly settings: Pick<
      AppSettingsValues,
      | "dashboardSessionSecret"
      | "discordOauthClientId"
      | "discordOauthClientSecret"
      | "discordOauthRedirectUri"
      | "twitchClientId"
      | "twitchClientSecret"
      | "twitchOauthRedirectUri"
      | "credentialEncryptionKey"
      | "twitchEventsubSecret"
      | "twitchEventsubCallbackUrl"
    >,
    private readonly eventsub: TwitchEventSubGateway | null = null,
  ) {}
  async page(request: Request) {
    const session = await this.authorize(request, false);
    if (session instanceof Response) return session;
    const data = await this.integrations.snapshot(
      session.communityId!,
      Number(session.userId),
    );
    const url = new URL(request.url);
    return new Response(
      dashboardDocument(
        render(
          h(IntegrationsWorkspace, {
            ...data,
            canManage: roleAllows(session.role, "integrations.manage"),
            status: url.searchParams.get("status") ?? "",
            error: url.searchParams.get("error") ?? "",
          }),
        ),
        "Integrations | QBot4K",
      ),
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  async discordLink(request: Request) {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.origin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    const form = await request.formData();
    const communityId = Number(form.get("community_id"));
    const guildId = String(form.get("guild_id") ?? "").trim();
    if (
      communityId !== session.communityId ||
      !this.settings.dashboardSessionSecret ||
      !this.settings.discordOauthClientId || !guildId
    ) {
      return new Response("Discord installation is not configured", {
        status: 503,
      });
    }
    try {
      const state = this.state(session, communityId, { guild_id: guildId }, 15);
      await this.integrations.createDiscordIntent({
        communityId,
        operatorId: Number(session.userId),
        guildId,
        pilotInviteCode: "",
        state,
      });
      const url = new URL("https://discord.com/oauth2/authorize");
      url.search = new URLSearchParams({
        client_id: this.settings.discordOauthClientId,
        redirect_uri: this.discordRedirectUri(request.url),
        response_type: "code",
        scope: "bot applications.commands",
        permissions: "8",
        guild_id: guildId,
        disable_guild_select: "true",
        state: await signState(this.settings.dashboardSessionSecret, state),
      }).toString();
      return this.redirect(url.toString());
    } catch (error) {
      return new Response(this.message(error), { status: 403 });
    }
  }
  async discordCallback(request: Request) {
    const session = await this.authorize(request, false);
    if (session instanceof Response) return session;
    const url = new URL(request.url);
    const state = await parseState(
      this.settings.dashboardSessionSecret ?? "",
      url.searchParams.get("state") ?? "",
    );
    if (
      !state || state.operator_id !== session.userId ||
      state.community_id !== session.communityId ||
      state.guild_id !== url.searchParams.get("guild_id") ||
      !url.searchParams.get("code")
    ) {
      return new Response("Invalid Discord installation state", {
        status: 400,
      });
    }
    if (
      !this.settings.discordOauthClientId ||
      !this.settings.discordOauthClientSecret
    ) return new Response("Discord OAuth is not configured", { status: 503 });
    try {
      await this.gateway.exchangeDiscord(
        url.searchParams.get("code")!,
        this.discordRedirectUri(request.url),
      );
      await this.integrations.completeDiscordIntent(state);
      return this.redirect(
        "/integrations?status=Discord%20installation%20pending",
      );
    } catch (error) {
      return new Response(this.message(error), { status: 502 });
    }
  }
  async twitchLink(request: Request) {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.origin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    const form = await request.formData();
    const login = String(form.get("broadcaster_login") ?? "").trim()
      .toLocaleLowerCase();
    const scopes = form.getAll("scope").map(String).sort();
    if (
      !this.settings.twitchClientId || !this.settings.twitchClientSecret ||
      !this.settings.dashboardSessionSecret
    ) return new Response("Twitch OAuth is not configured", { status: 503 });
    try {
      const state = this.state(session, session.communityId!, {
        broadcaster_login: login,
        scopes,
      }, 20);
      await this.integrations.createTwitchIntent({
        communityId: session.communityId!,
        operatorId: Number(session.userId),
        broadcasterLogin: login,
        scopes,
        state,
      });
      const url = new URL("https://id.twitch.tv/oauth2/authorize");
      url.search = new URLSearchParams({
        client_id: this.settings.twitchClientId,
        redirect_uri: this.twitchRedirectUri(request.url),
        response_type: "code",
        scope: scopes.join(" "),
        state: await signState(this.settings.dashboardSessionSecret, state),
        force_verify: "true",
      }).toString();
      return this.redirect(url.toString());
    } catch (error) {
      return new Response(this.message(error), { status: 400 });
    }
  }
  async twitchCallback(request: Request) {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    const url = new URL(request.url);
    const token = url.searchParams.get("state") ?? "";
    const providerError = url.searchParams.get("error_description") ??
      url.searchParams.get("error");
    if (providerError) {
      return this.redirect(
        `/integrations?error=${encodeURIComponent(providerError)}&resume=${
          encodeURIComponent(token)
        }`,
      );
    }
    const state = await parseState(
      this.settings.dashboardSessionSecret ?? "",
      token,
    );
    if (
      !state || state.operator_id !== session.userId ||
      state.community_id !== session.communityId || !state.broadcaster_login ||
      !url.searchParams.get("code")
    ) return new Response("Invalid Twitch installation state", { status: 400 });
    if (
      !this.settings.twitchClientId || !this.settings.twitchClientSecret ||
      !this.settings.credentialEncryptionKey
    ) return new Response("Twitch OAuth is not configured", { status: 503 });
    try {
      const grant = await this.gateway.exchangeTwitch(
        url.searchParams.get("code")!,
        this.twitchRedirectUri(request.url),
      );
      const installationId = await this.integrations.completeTwitchIntent(
        state,
        grant,
        this.settings.credentialEncryptionKey,
        Boolean(
          this.settings.twitchEventsubSecret &&
            this.settings.twitchEventsubCallbackUrl,
        ),
      );
      if (
        this.settings.twitchEventsubSecret &&
        this.settings.twitchEventsubCallbackUrl
      ) {
        if (!this.eventsub) {
          throw new TypeError("Twitch EventSub reconciliation is unavailable");
        }
        await this.eventsub.reconcile({
          communityId: state.community_id,
          installationId,
          grant,
          callbackUrl: this.settings.twitchEventsubCallbackUrl,
          secret: this.settings.twitchEventsubSecret,
        });
      }
      return this.redirect(
        "/integrations?status=Twitch%20installation%20pending",
      );
    } catch (error) {
      return this.redirect(
        `/integrations?error=${
          encodeURIComponent(this.message(error))
        }&resume=${encodeURIComponent(token)}`,
      );
    }
  }
  async revoke(request: Request, installationId: number) {
    const session = await this.authorize(request, true);
    if (session instanceof Response) return session;
    if (!this.origin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const payload = await request.json() as Record<string, unknown>;
      const expected = `REVOKE INTEGRATION ${installationId}`;
      if (!constantTimeEqual(String(payload.confirmation ?? ""), expected)) {
        throw new TypeError(`confirmation must be ${expected}`);
      }
      await this.integrations.revoke(
        session.communityId!,
        Number(session.userId),
        installationId,
      );
      return Response.json({
        status: "revoked",
        installation_id: installationId,
      });
    } catch (error) {
      return Response.json({ error: this.message(error) }, { status: 409 });
    }
  }
  private state(
    session: DashboardSession,
    communityId: number,
    extra: Partial<InstallState>,
    minutes: number,
  ): InstallState {
    return {
      operator_id: session.userId,
      community_id: communityId,
      nonce: crypto.randomUUID(),
      expires_at: new Date(Date.now() + minutes * 60_000).toISOString(),
      ...extra,
    };
  }
  private async authorize(
    request: Request,
    manage: boolean,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) return this.redirect("/login");
    if (session.communityId === null) {
      return new Response("Select a community", { status: 403 });
    }
    if (manage && !roleAllows(session.role, "integrations.manage")) {
      return new Response("Integration management is not authorized", {
        status: 403,
      });
    }
    return session;
  }
  private origin(request: Request) {
    return isAllowedSameSiteOrigin(request);
  }
  private discordRedirectUri(requestUrl: string) {
    const redirect = new URL(
      this.settings.discordOauthRedirectUri ?? requestUrl,
    );
    if (!this.settings.discordOauthRedirectUri) {
      redirect.pathname = "/integrations/discord/callback";
      redirect.search = "";
      redirect.hash = "";
    } else if (redirect.pathname === "/oauth/discord/callback") {
      redirect.pathname = "/integrations/discord/callback";
    }
    return redirect.toString();
  }
  private twitchRedirectUri(requestUrl: string) {
    const requestOrigin = new URL(requestUrl).origin;
    const configured = this.settings.twitchOauthRedirectUri;
    if (!configured) return `${requestOrigin}/integrations/twitch/callback`;
    const redirect = new URL(configured);
    if (["localhost", "127.0.0.1"].includes(redirect.hostname)) {
      return `${requestOrigin}/integrations/twitch/callback`;
    }
    redirect.search = "";
    redirect.hash = "";
    return redirect.toString();
  }
  private redirect(location: string) {
    return new Response(null, { status: 302, headers: { location } });
  }
  private message(error: unknown) {
    return error instanceof Error ? error.message : String(error);
  }
}

async function signState(secret: string, state: InstallState): Promise<string> {
  const payload = btoa(JSON.stringify(state, Object.keys(state).sort()))
    .replaceAll("+", "-").replaceAll("/", "_");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(payload),
  );
  return `${payload}.${
    [...new Uint8Array(signature)].map((byte) =>
      byte.toString(16).padStart(2, "0")
    ).join("")
  }`;
}
async function parseState(
  secret: string,
  token: string,
): Promise<InstallState | null> {
  const separator = token.lastIndexOf(".");
  if (!secret || separator < 1) return null;
  const payload = token.slice(0, separator);
  const expected = await signPayload(secret, payload);
  if (!constantTimeEqual(token.slice(separator + 1), expected)) return null;
  try {
    const state = JSON.parse(
      atob(payload.replaceAll("-", "+").replaceAll("_", "/")),
    ) as InstallState;
    if (
      !state.nonce || !state.operator_id ||
      !Number.isInteger(state.community_id) ||
      Date.parse(state.expires_at) <= Date.now()
    ) return null;
    return state;
  } catch {
    return null;
  }
}
async function signPayload(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(payload),
  );
  return [...new Uint8Array(signature)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}
