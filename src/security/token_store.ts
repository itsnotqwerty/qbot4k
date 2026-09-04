import { basename, dirname, join, resolve } from "@std/path";

function escapeRegularExpression(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function updateEnvironmentValue(
  contents: string,
  key: string,
  value: string,
): string {
  const replacement = `${key}=${value}`;
  const pattern = new RegExp(
    `^(?:export\\s+)?${escapeRegularExpression(key)}=.*$`,
    "mu",
  );
  if (pattern.test(contents)) return contents.replace(pattern, replacement);
  const separator = !contents || contents.endsWith("\n") ? "" : "\n";
  return `${contents}${separator}${replacement}\n`;
}

export class TokenStore {
  private pending: Promise<void> = Promise.resolve();
  readonly environmentPath: string;

  constructor(environmentPath: string) {
    this.environmentPath = resolve(environmentPath);
  }

  persistRefreshedTwitchTokens(
    accessToken: string,
    refreshToken?: string | null,
  ): Promise<void> {
    const operation = this.pending.then(() =>
      this.writeTokens(accessToken, refreshToken)
    );
    this.pending = operation.catch(() => undefined);
    return operation;
  }

  private async writeTokens(
    accessToken: string,
    refreshToken?: string | null,
  ): Promise<void> {
    let contents = "";
    try {
      contents = await Deno.readTextFile(this.environmentPath);
    } catch (error) {
      if (!(error instanceof Deno.errors.NotFound)) throw error;
    }
    contents = updateEnvironmentValue(
      contents,
      "QBOT_TWITCH_BOT_TOKEN",
      accessToken,
    );
    if (refreshToken) {
      contents = updateEnvironmentValue(
        contents,
        "QBOT_TWITCH_REFRESH_TOKEN",
        refreshToken,
      );
    }
    const directory = dirname(this.environmentPath);
    await Deno.mkdir(directory, { recursive: true });
    const temporaryPath = join(
      directory,
      `.${basename(this.environmentPath)}.${crypto.randomUUID()}`,
    );
    try {
      const file = await Deno.open(temporaryPath, {
        createNew: true,
        write: true,
        mode: 0o640,
      });
      try {
        await file.write(new TextEncoder().encode(contents));
        await file.sync();
      } finally {
        file.close();
      }
      await Deno.chmod(temporaryPath, 0o640);
      await Deno.rename(temporaryPath, this.environmentPath);
    } finally {
      try {
        await Deno.remove(temporaryPath);
      } catch {
        // The rename normally removes this path; cleanup must not mask a write error.
      }
    }
  }
}
