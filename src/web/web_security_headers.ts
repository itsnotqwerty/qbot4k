const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "script-src 'self' 'unsafe-inline'",
  "img-src 'self' data: https:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
].join("; ");

export function createSecurityHeadersHandler(
  primary: (request: Request) => Response | Promise<Response>,
): (request: Request) => Promise<Response> {
  return async (request) => {
    const response = await primary(request);
    response.headers.set("Content-Security-Policy", CONTENT_SECURITY_POLICY);
    response.headers.set("X-Content-Type-Options", "nosniff");
    response.headers.set("X-Frame-Options", "DENY");
    response.headers.set("Referrer-Policy", "no-referrer");
    response.headers.set(
      "Permissions-Policy",
      "camera=(), geolocation=(), microphone=()",
    );
    if (
      request.headers.get("x-forwarded-proto")?.toLocaleLowerCase() ===
        "https" || new URL(request.url).protocol === "https:"
    ) {
      response.headers.set(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
      );
    }
    return response;
  };
}
