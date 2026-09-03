import { App, staticFiles } from "fresh";

export const app = new App()
  .use(staticFiles())
  .get("/api/fresh-health", () => Response.json({ status: "ready" }))
  .fsRoutes();
