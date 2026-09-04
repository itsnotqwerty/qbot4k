export class TwitchAuthError extends Error {}
export class TwitchReauthorizationRequired extends TwitchAuthError {}
export class TwitchTemporaryAuthError extends TwitchAuthError {}

export interface TwitchTokenValidation {
  readonly accessToken: string;
  readonly login: string;
  readonly clientId: string;
  readonly userId: string;
}

export interface TwitchTokenManagerOptions {
  readonly initialAccessToken: string;
  readonly refreshToken?: string | null;
  readonly clientId?: string | null;
  readonly clientSecret?: string | null;
  readonly fetcher?: typeof fetch;
  readonly onTokenRefresh?: (
    accessToken: string,
    refreshToken: string | null,
  ) => Promise<void> | void;
}

export class TwitchTokenManager {
  private accessTokenValue: string;
  private refreshTokenValue: string;
  private readonly clientId: string;
  private readonly clientSecret: string;
  private readonly fetcher: typeof fetch;
  private refreshInFlight: Promise<string> | null = null;

  constructor(private readonly options: TwitchTokenManagerOptions) {
    this.accessTokenValue = normalizeToken(options.initialAccessToken);
    this.refreshTokenValue = options.refreshToken?.trim() ?? "";
    this.clientId = options.clientId?.trim() ?? "";
    this.clientSecret = options.clientSecret?.trim() ?? "";
    this.fetcher = options.fetcher ?? fetch;
  }

  canRefresh(): boolean {
    return Boolean(
      this.refreshTokenValue && this.clientId && this.clientSecret,
    );
  }

  accessToken(): string {
    return this.accessTokenValue;
  }

  async validateToken(): Promise<TwitchTokenValidation> {
    let payload = await this.validateAccessToken(this.accessTokenValue);
    if (!payload && this.canRefresh()) {
      await this.refreshAccessToken();
      payload = await this.validateAccessToken(this.accessTokenValue);
    }
    if (!payload) {
      throw new TwitchReauthorizationRequired(
        "Twitch authorization is invalid",
      );
    }
    const login = text(payload.login);
    if (!login) {
      throw new TwitchAuthError(
        "Twitch token validation response did not include login",
      );
    }
    const clientId = text(payload.client_id) || this.clientId;
    if (!clientId) {
      throw new TwitchAuthError(
        "Twitch token validation response did not include client_id",
      );
    }
    return Object.freeze({
      accessToken: this.accessTokenValue,
      login,
      clientId,
      userId: text(payload.user_id),
    });
  }

  refreshAccessToken(): Promise<string> {
    if (!this.refreshInFlight) {
      this.refreshInFlight = this.refresh().finally(() => {
        this.refreshInFlight = null;
      });
    }
    return this.refreshInFlight;
  }

  private async validateAccessToken(
    accessToken: string,
  ): Promise<Record<string, unknown> | null> {
    let response: Response;
    try {
      response = await this.fetcher("https://id.twitch.tv/oauth2/validate", {
        headers: { Authorization: `OAuth ${accessToken}` },
      });
    } catch (error) {
      throw new TwitchTemporaryAuthError(
        `Twitch token validation is temporarily unavailable: ${
          errorMessage(error)
        }`,
      );
    }
    if (response.status === 400 || response.status === 401) return null;
    if (!response.ok) {
      throw new TwitchTemporaryAuthError(
        `Twitch token validation is temporarily unavailable: HTTP ${response.status}`,
      );
    }
    return record(await response.json());
  }

  private async refresh(): Promise<string> {
    if (!this.canRefresh()) {
      throw new TwitchAuthError("Twitch refresh configuration is incomplete");
    }
    let response: Response;
    try {
      response = await this.fetcher("https://id.twitch.tv/oauth2/token", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: this.refreshTokenValue,
          client_id: this.clientId,
          client_secret: this.clientSecret,
        }),
      });
    } catch (error) {
      throw new TwitchTemporaryAuthError(
        `Failed to refresh Twitch token: ${errorMessage(error)}`,
      );
    }
    if (response.status === 400 || response.status === 401) {
      throw new TwitchReauthorizationRequired(
        `Twitch refresh authorization was rejected: HTTP ${response.status}`,
      );
    }
    if (!response.ok) {
      throw new TwitchTemporaryAuthError(
        `Failed to refresh Twitch token: HTTP ${response.status}`,
      );
    }
    const payload = record(await response.json());
    if (!payload) {
      throw new TwitchAuthError(
        "Failed to refresh Twitch token: invalid response payload",
      );
    }
    const accessToken = normalizeToken(text(payload.access_token));
    if (!accessToken) {
      throw new TwitchAuthError(
        "Failed to refresh Twitch token: missing access_token",
      );
    }
    this.accessTokenValue = accessToken;
    const refreshToken = text(payload.refresh_token);
    if (refreshToken) this.refreshTokenValue = refreshToken;
    try {
      await this.options.onTokenRefresh?.(
        this.accessTokenValue,
        this.refreshTokenValue || null,
      );
    } catch {
      // A valid live token remains usable when persistence is temporarily down.
    }
    return this.accessTokenValue;
  }
}

function normalizeToken(value: string): string {
  return value.replace(/^oauth:/u, "").trim();
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value).trim();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
