import type { DatabaseConnection } from "../data/database.ts";
import {
  type CommandReply,
  formatCommandTemplate,
  normalizeCustomCommandName,
  parseCommand,
  renderCommandReply,
} from "./command_domain.ts";

export interface CommandExecutionResult {
  readonly commandName: string | null;
  readonly actionJobType: string | null;
}

export interface CommandMessageInput {
  readonly observationId: number;
  readonly communityId: number;
  readonly platform: string;
  readonly channelId: string;
  readonly contentRaw: string;
  readonly username: string;
  readonly isModerator: boolean;
  readonly roleNames: readonly string[];
  readonly httpResponse?: (
    method: string,
    url: string,
  ) => string | null;
}

export class PostgresCommandExecutionRepository {
  private readonly httpCache = new Map<string, string | null>();

  constructor(private readonly connection: DatabaseConnection) {}

  private async prefetchHttpMacros(
    templates: readonly string[],
    values: Readonly<Record<string, unknown>>,
  ): Promise<void> {
    const pattern = /\$\{(GET|POST|PUT|DELETE)\}\((https?:\/\/[^\s)]+)\)/giu;
    const tasks: Promise<void>[] = [];
    for (const template of templates) {
      for (const match of template.matchAll(pattern)) {
        const method = match[1].toUpperCase();
        const url = match[2].replaceAll(
          "${query}",
          encodeURIComponent(String(values.query ?? "")).replaceAll(
            "%20",
            "+",
          ),
        );
        const key = `${method} ${url}`;
        if (this.httpCache.has(key)) continue;
        this.httpCache.set(key, null);
        tasks.push(
          fetch(url, { method: method === "GET" ? "GET" : method })
            .then(async (response) => {
              this.httpCache.set(
                key,
                response.ok ? await response.text() : null,
              );
            })
            .catch(() => {
              this.httpCache.set(key, null);
            }),
        );
      }
    }
    await Promise.all(tasks);
  }

  private httpResolver = (method: string, url: string): string | null =>
    this.httpCache.get(`${method.toUpperCase()} ${url}`) ?? null;

  async execute(input: CommandMessageInput): Promise<CommandExecutionResult> {
    const parsed = parseCommand(input.contentRaw);
    if (!parsed) {
      return Object.freeze({ commandName: null, actionJobType: null });
    }
    const [rawName, arguments_] = parsed;
    const managed = normalizeCustomCommandName(rawName) === "" &&
      ["addcom", "delcom", "editcom", "alias", "credit", "verify"].includes(
        rawName,
      );
    const reply = managed
      ? await this.manageCommand(rawName, arguments_, input)
      : await this.resolveCommand(rawName, arguments_, input);
    if (!reply) {
      return Object.freeze({ commandName: rawName, actionJobType: null });
    }
    const rendered = renderCommandReply(reply, input.platform);
    const jobType = input.platform === "discord"
      ? "discord.message.send"
      : "twitch.message.send";
    const payload = input.platform === "discord"
      ? {
        channel_id: input.channelId,
        rendered_reply: rendered,
      }
      : {
        channel_id: input.channelId,
        message: String(rendered),
      };
    await this.connection.query(
      `INSERT INTO processing_jobs(
         community_id,stage,job_type,observation_id,payload_json,priority,idempotency_key
       ) VALUES ($1,'action',$2,$3,$4,25,$5)
       ON CONFLICT(idempotency_key) DO NOTHING`,
      [
        input.communityId,
        jobType,
        input.observationId,
        JSON.stringify(payload),
        `command:${input.platform}:${input.observationId}:v2`,
      ],
    );
    return Object.freeze({ commandName: rawName, actionJobType: jobType });
  }

  private async manageCommand(
    name: string,
    arguments_: readonly string[],
    input: CommandMessageInput,
  ): Promise<CommandReply | null> {
    if (!input.isModerator && !hasOperatorRole(input.roleNames)) {
      return card("Commands", "Command management requires moderator access.");
    }
    if (name === "addcom" || name === "editcom") {
      const commandName = normalizeCustomCommandName(arguments_[0] ?? "");
      const response = arguments_.slice(1).join(" ").trim();
      if (!commandName || !response) {
        return card(
          "Commands",
          `Usage: !${name} <command> <response>`,
        );
      }
      const existing = await this.simpleCommand(commandName);
      if (name === "addcom" && existing) {
        return card("Commands", `!${commandName} already exists.`);
      }
      if (name === "editcom" && !existing) {
        return card("Commands", `!${commandName} does not exist.`);
      }
      await this.connection.query(
        `INSERT INTO simple_command_definitions(
           command_name,response_template,enabled
         ) VALUES ($1,$2,1)
         ON CONFLICT(command_name) DO UPDATE SET
           response_template=EXCLUDED.response_template,enabled=1,
           updated_at=CURRENT_TIMESTAMP`,
        [commandName, response],
      );
      return card(
        "Commands",
        `${name === "addcom" ? "Created" : "Updated"} !${commandName}.`,
      );
    }
    if (name === "delcom") {
      const commandName = normalizeCustomCommandName(arguments_[0] ?? "");
      if (!commandName) return card("Commands", "Usage: !delcom <command>");
      const deleted = await this.connection.query(
        "DELETE FROM simple_command_definitions WHERE command_name=$1 RETURNING command_name",
        [commandName],
      );
      return card(
        "Commands",
        deleted.length
          ? `Deleted !${commandName}.`
          : `!${commandName} does not exist.`,
      );
    }
    if (name === "alias") {
      const aliasName = normalizeCustomCommandName(arguments_[0] ?? "");
      const targetName = normalizeCustomCommandName(arguments_[1] ?? "");
      if (!aliasName || !targetName) {
        return card("Commands", "Usage: !alias <newcommand> <existingcommand>");
      }
      if (aliasName === targetName) {
        return card("Commands", "A command cannot alias itself.");
      }
      const existingAlias = await this.simpleCommand(aliasName);
      if (existingAlias) {
        return card("Commands", `!${aliasName} already exists.`);
      }
      const target = await this.resolveAnyCommand(targetName);
      if (!target) {
        return card("Commands", `!${targetName} does not exist.`);
      }
      if (target.startsWith(ALIAS_PREFIX)) {
        return card(
          "Commands",
          `!${targetName} is itself an alias; alias the target directly.`,
        );
      }
      await this.connection.query(
        `INSERT INTO simple_command_definitions(
           command_name,response_template,enabled
         ) VALUES ($1,$2,1)
         ON CONFLICT(command_name) DO UPDATE SET
           response_template=EXCLUDED.response_template,enabled=1,
           updated_at=CURRENT_TIMESTAMP`,
        [aliasName, `${ALIAS_PREFIX}${targetName}`],
      );
      return card("Commands", `Aliased !${aliasName} to !${targetName}.`);
    }
    if (name === "credit") {
      const row = (await this.connection.query(
        `SELECT u.current_reputation_score,u.score_confidence
           FROM platform_accounts AS account
           JOIN users AS u ON u.id=account.user_id
          WHERE account.platform=$1 AND account.platform_user_id=$2`,
        [input.platform, input.username],
      ))[0];
      return card(
        "Social credit",
        row
          ? `Current score: ${
            Number(row.current_reputation_score)
          } (confidence ${(Number(row.score_confidence) * 100).toFixed(0)}%).`
          : "No linked profile is available yet.",
      );
    }
    return card("Commands", "Verification is handled by onboarding.");
  }

  private async resolveCommand(
    name: string,
    arguments_: readonly string[],
    input: CommandMessageInput,
  ): Promise<CommandReply | null> {
    let commandName = normalizeCustomCommandName(name);
    if (!commandName) return null;
    const seen = new Set<string>([commandName]);
    // Follow simple-command aliases (up to a few hops) to their target.
    for (let hop = 0; hop < 5; hop += 1) {
      const aliasTarget = await this.aliasTarget(commandName);
      if (!aliasTarget) break;
      if (seen.has(aliasTarget)) return null;
      seen.add(aliasTarget);
      commandName = aliasTarget;
    }
    const values = {
      query: arguments_.join(" "),
      user: input.username,
      channel: input.channelId,
    };
    const resolve = input.httpResponse ?? this.httpResolver;
    const builtin = (await this.connection.query(
      `SELECT title,description_template,footer_template
         FROM command_definitions
        WHERE command_name=$1 AND enabled=1`,
      [commandName],
    ))[0];
    if (builtin) {
      const templates = [
        String(builtin.title),
        String(builtin.description_template),
        String(builtin.footer_template ?? ""),
      ];
      if (!input.httpResponse) await this.prefetchHttpMacros(templates, values);
      return {
        card: {
          title: formatCommandTemplate(
            String(builtin.title),
            values,
            secureRandom,
            resolve,
          ),
          description: formatCommandTemplate(
            String(builtin.description_template),
            values,
            secureRandom,
            resolve,
          ),
          footer: builtin.footer_template === null
            ? null
            : formatCommandTemplate(
              String(builtin.footer_template),
              values,
              secureRandom,
              resolve,
            ),
          color: null,
        },
        textOnly: input.platform === "twitch",
      };
    }
    const simple = await this.simpleCommand(commandName);
    if (simple) {
      if (!input.httpResponse) {
        await this.prefetchHttpMacros(
          [String(simple.response_template)],
          values,
        );
      }
      return {
        card: {
          title: `!${commandName}`,
          description: formatCommandTemplate(
            String(simple.response_template),
            values,
            secureRandom,
            resolve,
          ),
          color: null,
          footer: null,
        },
        textOnly: true,
      };
    }
    return null;
  }

  private async simpleCommand(commandName: string) {
    return (await this.connection.query(
      `SELECT response_template FROM simple_command_definitions
        WHERE command_name=$1 AND enabled=1`,
      [commandName],
    ))[0];
  }

  // Returns the alias target name when the command is an alias, else null.
  private async aliasTarget(commandName: string): Promise<string | null> {
    const row = await this.simpleCommand(commandName);
    if (!row) return null;
    const template = String(row.response_template);
    if (!template.startsWith(ALIAS_PREFIX)) return null;
    const target = normalizeCustomCommandName(
      template.slice(ALIAS_PREFIX.length),
    );
    return target || null;
  }

  // Returns the response template for a builtin or simple command, else null.
  private async resolveAnyCommand(
    commandName: string,
  ): Promise<string | null> {
    const builtin = (await this.connection.query(
      `SELECT description_template FROM command_definitions
        WHERE command_name=$1 AND enabled=1`,
      [commandName],
    ))[0];
    if (builtin) return String(builtin.description_template);
    const simple = await this.simpleCommand(commandName);
    return simple ? String(simple.response_template) : null;
  }
}

const ALIAS_PREFIX = "alias:";

function hasOperatorRole(roleNames: readonly string[]): boolean {
  return roleNames.some((role) =>
    ["admin", "administrator", "moderator", "owner", "broadcaster"].includes(
      role.toLocaleLowerCase(),
    )
  );
}

function card(title: string, description: string): CommandReply {
  return { card: { title, description, color: null, footer: null } };
}

function secureRandom(lowerBound: number, upperBound: number): number {
  const lower = Math.ceil(Math.max(-1_000_000, lowerBound));
  const upper = Math.floor(Math.min(1_000_000, upperBound));
  const range = upper - lower + 1;
  const values = new Uint32Array(1);
  crypto.getRandomValues(values);
  return lower + (values[0] % range);
}
