import { caseFold } from "unicode-case-folding";
import type { NormalizedMessage } from "../core/models.ts";

const COMMANDS = new Set(["/bump", "/boop"]);
const SUCCESS_SIGNALS: Readonly<Record<string, readonly string[]>> = Object
  .freeze({
    "/bump": Object.freeze([
      "bump done",
      "bumped successfully",
      "bump successful",
      "server bumped",
      "bump complete",
      "bump completed",
    ]),
    "/boop": Object.freeze([
      "boop done",
      "booped successfully",
      "boop successful",
      "server booped",
      "boop complete",
      "boop completed",
    ]),
  });

export function serverBoostCommandName(
  content: string,
  interactionCommandName = "",
): string | null {
  const normalized = caseFold(content).trim();
  if (normalized) {
    const firstToken = normalized.split(/\s+/u, 1)[0];
    if (COMMANDS.has(firstToken)) return firstToken;
  }
  const interactionName = caseFold(interactionCommandName).trim().replace(
    /^\/+/,
    "",
  );
  return COMMANDS.has(`/${interactionName}`) ? `/${interactionName}` : null;
}

export function detectServerBoostSuccess(
  content: string,
  interactionCommandName = "",
  embedText = "",
): string | null {
  const normalized = caseFold(`${content}\n${embedText}`);
  for (const [commandName, phrases] of Object.entries(SUCCESS_SIGNALS)) {
    if (phrases.some((phrase) => normalized.includes(phrase))) {
      return commandName;
    }
  }
  const inferredCommand = serverBoostCommandName("", interactionCommandName);
  if (
    inferredCommand !== null &&
    ["done", "success", "successful", "completed", "complete"].some((keyword) =>
      normalized.includes(keyword)
    )
  ) return inferredCommand;
  return null;
}

export function isServerBoostConfirmation(
  message: Pick<NormalizedMessage, "contentRaw" | "metadata">,
): boolean {
  if (!pythonTruthiness(message.metadata.author_is_bot)) return false;
  return detectServerBoostSuccess(
    message.contentRaw,
    String(message.metadata.interaction_command_name ?? ""),
    String(message.metadata.embed_text ?? ""),
  ) !== null;
}

function pythonTruthiness(value: unknown): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string" || Array.isArray(value)) {
    return value.length > 0;
  }
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}
