import {
  AppSettings,
  ConfigError,
  safeDatabasePath,
} from "./src/core/config.ts";
import { withOperationalService } from "./src/data/operations.ts";

interface Arguments {
  readonly command: string;
  readonly envFile?: string;
  readonly communityId?: number;
  readonly expiresHours: number;
  readonly operatorId?: number;
  readonly jobType?: string;
  readonly installationId?: number;
  readonly ownerRuntime?: "python" | "deno" | "none";
  readonly shadowRuntime?: "python" | "deno" | null;
}

export function parseArguments(args: readonly string[]): Arguments {
  const values = [...args];
  const envIndex = values.findIndex((value) => value.startsWith("--env-file="));
  const envFile = envIndex >= 0
    ? values.splice(envIndex, 1)[0].slice("--env-file=".length)
    : undefined;
  const command = values.shift() ?? "";
  if (
    ![
      "check-config",
      "init-db",
      "migrate",
      "platform-audit",
      "issue-pilot-invite",
      "transfer-job-ownership",
      "transfer-installation-ownership",
    ].includes(command)
  ) {
    throw new TypeError(
      "command must be check-config, init-db, migrate, platform-audit, issue-pilot-invite, transfer-job-ownership, or transfer-installation-ownership",
    );
  }
  let communityId: number | undefined;
  let expiresHours = 72;
  let operatorId: number | undefined;
  let jobType: string | undefined;
  let installationId: number | undefined;
  let ownerRuntime: "python" | "deno" | "none" | undefined;
  let shadowRuntime: "python" | "deno" | null | undefined;
  if (command === "transfer-installation-ownership") {
    installationId = integer(values.shift(), "installation_id");
    const owner = values.shift();
    if (owner !== "python" && owner !== "deno" && owner !== "none") {
      throw new TypeError("owner_runtime must be python, deno, or none");
    }
    ownerRuntime = owner;
    for (const value of values) {
      if (value.startsWith("--operator-id=")) {
        operatorId = integer(
          value.slice("--operator-id=".length),
          "operator_id",
        );
      } else throw new TypeError(`unknown argument: ${value}`);
    }
  } else if (command === "transfer-job-ownership") {
    jobType = values.shift()?.trim();
    const owner = values.shift();
    if (!jobType) throw new TypeError("job_type must not be empty");
    if (owner !== "python" && owner !== "deno" && owner !== "none") {
      throw new TypeError("owner_runtime must be python, deno, or none");
    }
    ownerRuntime = owner;
    for (const value of values) {
      if (value.startsWith("--shadow-runtime=")) {
        const shadow = value.slice("--shadow-runtime=".length);
        if (shadow !== "python" && shadow !== "deno" && shadow !== "none") {
          throw new TypeError("shadow_runtime must be python, deno, or none");
        }
        shadowRuntime = shadow === "none" ? null : shadow;
      } else if (value.startsWith("--operator-id=")) {
        operatorId = integer(
          value.slice("--operator-id=".length),
          "operator_id",
        );
      } else throw new TypeError(`unknown argument: ${value}`);
    }
  } else if (command === "issue-pilot-invite") {
    communityId = integer(values.shift(), "community_id");
    for (const value of values) {
      if (value.startsWith("--expires-hours=")) {
        expiresHours = integer(
          value.slice("--expires-hours=".length),
          "expires_hours",
        );
      } else if (value.startsWith("--operator-id=")) {
        operatorId = integer(
          value.slice("--operator-id=".length),
          "operator_id",
        );
      } else throw new TypeError(`unknown argument: ${value}`);
    }
  } else if (values.length) {
    throw new TypeError(`unknown argument: ${values[0]}`);
  }
  return {
    command,
    expiresHours,
    ...(envFile ? { envFile } : {}),
    ...(communityId ? { communityId } : {}),
    ...(operatorId ? { operatorId } : {}),
    ...(jobType ? { jobType } : {}),
    ...(installationId ? { installationId } : {}),
    ...(ownerRuntime ? { ownerRuntime } : {}),
    ...(shadowRuntime !== undefined ? { shadowRuntime } : {}),
  };
}

export async function main(args = Deno.args): Promise<number> {
  const parsed = parseArguments(args);
  const operationalRole = parsed.command === "check-config"
    ? undefined
    : "jobs" as const;
  const settings = AppSettings.fromEnv(undefined, {
    envFile: parsed.envFile,
    ...(operationalRole ? { role: operationalRole } : {}),
  });
  if (parsed.command === "check-config") {
    console.log(JSON.stringify(settings.safeSummary(), null, 2));
    return 0;
  }
  if (!settings.databasePath.startsWith("postgres")) {
    throw new ConfigError("Deno operational commands require PostgreSQL");
  }
  return await withOperationalService(
    settings.databasePath,
    async (service) => {
      if (parsed.command === "init-db" || parsed.command === "migrate") {
        const tables = await service.migrate();
        console.log(
          JSON.stringify(
            { database_path: safeDatabasePath(settings.databasePath), tables },
            null,
            2,
          ),
        );
        return 0;
      }
      if (parsed.command === "platform-audit") {
        await service.migrate();
        const result = await service.audit(settings);
        console.log(JSON.stringify(result, null, 2));
        return result.status === "fail" ? 1 : 0;
      }
      if (parsed.command === "transfer-job-ownership") {
        await service.migrate();
        await service.transferJobOwnership(
          parsed.jobType!,
          parsed.ownerRuntime!,
          parsed.shadowRuntime ?? null,
          parsed.operatorId,
        );
        console.log(JSON.stringify({
          job_type: parsed.jobType,
          owner_runtime: parsed.ownerRuntime,
          shadow_runtime: parsed.shadowRuntime ?? null,
        }));
        return 0;
      }
      if (parsed.command === "transfer-installation-ownership") {
        await service.migrate();
        await service.transferInstallationOwnership(
          parsed.installationId!,
          parsed.ownerRuntime!,
          parsed.operatorId,
        );
        console.log(JSON.stringify({
          installation_id: parsed.installationId,
          owner_runtime: parsed.ownerRuntime,
        }));
        return 0;
      }
      await service.migrate();
      const code = await service.issuePilotInvite(
        parsed.communityId!,
        parsed.expiresHours,
        parsed.operatorId,
      );
      console.log(JSON.stringify({
        community_id: parsed.communityId,
        expires_hours: parsed.expiresHours,
        pilot_invite_code: code,
      }));
      return 0;
    },
  );
}

function integer(value: string | undefined, name: string): number {
  if (!value || !/^\d+$/u.test(value)) {
    throw new TypeError(`${name} must be an integer`);
  }
  return Number(value);
}

if (import.meta.main) {
  try {
    Deno.exit(await main());
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    Deno.exit(error instanceof ConfigError ? 2 : 1);
  }
}
