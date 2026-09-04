const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const SIDE_EFFECTING_GET_PATHS = new Set([
  "/auth/discord/callback",
  "/oauth/discord/callback",
  "/integrations/discord/callback",
  "/integrations/twitch/callback",
]);

export function createReadOnlyWebHandler(
  handler: (request: Request) => Response | Promise<Response>,
): (request: Request) => Promise<Response> {
  return async (request) => {
    const path = new URL(request.url).pathname;
    if (
      !SAFE_METHODS.has(request.method) ||
      SIDE_EFFECTING_GET_PATHS.has(path)
    ) {
      return Response.json(
        { error: "read_only", message: "Web writes are disabled" },
        {
          status: 503,
          headers: { "cache-control": "no-store", "retry-after": "60" },
        },
      );
    }
    return await handler(request);
  };
}
