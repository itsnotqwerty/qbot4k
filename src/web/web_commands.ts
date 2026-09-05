import { h } from "preact";
import { render } from "npm:preact-render-to-string@6.7.0";
import { CommandsWorkspace } from "../../components/CommandsWorkspace.tsx";
import type { DatabaseConnection, DatabaseRow } from "../data/database.ts";
import type { DashboardSession } from "../security/security.ts";
import { isAllowedSameSiteOrigin } from "../security/security.ts";
import { WebAuthController } from "./web_auth.ts";
import { roleAllows } from "./web_dashboard.ts";
import { dashboardDocument } from "./web_document.ts";

export interface CommandRegistry {
  list(): Promise<
    {
      readonly builtins: readonly DatabaseRow[];
      readonly simple: readonly DatabaseRow[];
    }
  >;
  update(input: Readonly<Record<string, string | boolean>>): Promise<string>;
}

const reserved = new Set(["addcom", "delcom", "editcom", "alias"]);

export class PostgresCommandRegistry implements CommandRegistry {
  constructor(private readonly connection: DatabaseConnection) {}

  async list() {
    const builtins = await this.connection.query(
      "SELECT command_name,title,description_template,footer_template,enabled FROM command_definitions WHERE command_name NOT IN ('addcom','delcom','editcom') ORDER BY command_name",
    );
    const simple = await this.connection.query(
      "SELECT command_name,response_template,enabled FROM simple_command_definitions WHERE command_name NOT IN ('addcom','delcom','editcom','alias') ORDER BY command_name",
    );
    return Object.freeze({ builtins, simple });
  }

  async update(
    input: Readonly<Record<string, string | boolean>>,
  ): Promise<string> {
    const name = String(input.command_name ?? "").trim().toLocaleLowerCase()
      .replace(/^!/u, "");
    const type = String(input.record_type ?? "builtin").trim()
      .toLocaleLowerCase();
    const action = String(input.action ?? "save").trim().toLocaleLowerCase();
    if (!name) throw new TypeError("Missing command name");
    if (reserved.has(name)) throw new TypeError(`${name} is reserved`);
    if (type === "simple" && action === "delete") {
      await this.connection.query(
        "DELETE FROM simple_command_definitions WHERE command_name=$1",
        [name],
      );
      return `Deleted simple command ${name}`;
    }
    if (type === "simple") {
      const response = String(input.response_template ?? "").trim();
      if (!response) throw new TypeError("response_template must not be empty");
      await this.connection.query(
        `INSERT INTO simple_command_definitions(command_name,response_template,enabled) VALUES ($1,$2,$3) ON CONFLICT(command_name) DO UPDATE SET response_template=EXCLUDED.response_template,enabled=EXCLUDED.enabled,updated_at=CURRENT_TIMESTAMP`,
        [name, response, input.enabled ? 1 : 0],
      );
      return `Saved simple command ${name}`;
    }
    const title = String(input.title ?? "").trim();
    const description = String(input.description_template ?? "").trim();
    if (!title) throw new TypeError("title must not be empty");
    if (!description) {
      throw new TypeError("description_template must not be empty");
    }
    const footer = String(input.footer_template ?? "").trim() || null;
    await this.connection.query(
      `INSERT INTO command_definitions(command_name,title,description_template,footer_template,enabled) VALUES ($1,$2,$3,$4,$5) ON CONFLICT(command_name) DO UPDATE SET title=EXCLUDED.title,description_template=EXCLUDED.description_template,footer_template=EXCLUDED.footer_template,enabled=EXCLUDED.enabled,updated_at=CURRENT_TIMESTAMP`,
      [name, title, description, footer, input.enabled ? 1 : 0],
    );
    return `Saved builtin command ${name}`;
  }
}

export class WebCommandsController {
  constructor(
    private readonly auth: WebAuthController,
    private readonly registry: CommandRegistry,
  ) {}
  async page(request: Request): Promise<Response> {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    return new Response(
      dashboardDocument(
        render(
          h(CommandsWorkspace, {
            ...await this.registry.list(),
            status: new URL(request.url).searchParams.get("status") ?? "",
          }),
        ),
        "Commands | QBot4K",
      ),
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  }
  async update(request: Request): Promise<Response> {
    const session = await this.authorize(request);
    if (session instanceof Response) return session;
    if (!isAllowedSameSiteOrigin(request)) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const form = await request.formData();
      const status = await this.registry.update({
        ...Object.fromEntries(form),
        enabled: form.get("enabled") === "1",
      });
      return new Response(null, {
        status: 302,
        headers: { location: `/commands?status=${encodeURIComponent(status)}` },
      });
    } catch (error) {
      return new Response(
        error instanceof Error ? error.message : String(error),
        { status: 400 },
      );
    }
  }
  private async authorize(
    request: Request,
  ): Promise<DashboardSession | Response> {
    const session = await this.auth.authorizedSession(request);
    if (!session) {
      return new Response(null, {
        status: 302,
        headers: { location: "/login" },
      });
    }
    if (!roleAllows(session.role, "admin.manage")) {
      return new Response("Forbidden", { status: 403 });
    }
    return session;
  }
}
