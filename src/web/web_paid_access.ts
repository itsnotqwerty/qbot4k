import type { WebAuthController } from "./web_auth.ts";

export function createPaidAccessHandler(
  handler: (request: Request) => Promise<Response>,
  auth: Pick<WebAuthController, "accountSession" | "platformSession">,
  allowed: (communityId: number) => Promise<boolean>,
) {
  return async (request: Request): Promise<Response> => {
    const path = new URL(request.url).pathname;
    if (
      /^\/(?:billing|account|admin|login|logout|auth|oauth|health|status|privacy|terms|system-health|webhooks|_fresh|styles\.css)(?:\/|$)/u
        .test(path) ||
      /\/export\.(?:csv|json)$/u.test(path) ||
      ["/", "/community/switch", "/api/fresh-health"].includes(path)
    ) return handler(request);
    const session = await auth.accountSession(request);
    if (!session || session.communityId === null) return handler(request);
    if (
      await allowed(session.communityId) || await auth.platformSession(request)
    ) return handler(request);
    return new Response(
      "Organization access has expired. Billing, reward claims and permitted exports remain available.",
      {
        status: 402,
        headers: {
          "content-type": "text/plain; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  };
}
