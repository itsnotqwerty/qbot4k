import { assertEquals, assertStringIncludes } from "jsr:@std/assert@1.0.14";

const cutoverScript = new URL(
  "../deploy/execute-cutover.sh",
  import.meta.url,
).pathname;

Deno.test("cutover runner executes every stage in dependency order", async () => {
  const fixture = await createFixture();
  try {
    const result = await runCutover(fixture);
    assertEquals(result.code, 0);
    assertEquals(
      await Deno.readTextFile(fixture.log),
      "drain\nownership\npreflight\nswitch\nverify\n",
    );
    assertStringIncludes(
      new TextDecoder().decode(result.stdout),
      '"result":"cutover_complete"',
    );
  } finally {
    await Deno.remove(fixture.directory, { recursive: true });
  }
});

Deno.test("cutover runner stops before switching when preflight fails", async () => {
  const fixture = await createFixture("preflight");
  try {
    const result = await runCutover(fixture);
    assertEquals(result.code, 1);
    assertEquals(
      await Deno.readTextFile(fixture.log),
      "drain\nownership\npreflight\n",
    );
    assertStringIncludes(
      new TextDecoder().decode(result.stderr),
      "preflight stage failed",
    );
  } finally {
    await Deno.remove(fixture.directory, { recursive: true });
  }
});

interface Fixture {
  readonly directory: string;
  readonly log: string;
  readonly commands: Readonly<Record<string, string>>;
}

async function createFixture(failingStage?: string): Promise<Fixture> {
  const directory = await Deno.makeTempDir();
  const log = `${directory}/stages.log`;
  const commands: Record<string, string> = {};
  for (const stage of ["drain", "ownership", "preflight", "switch", "verify"]) {
    const path = `${directory}/${stage}.sh`;
    const exitCode = stage === failingStage ? 1 : 0;
    await Deno.writeTextFile(
      path,
      `#!/bin/sh\nprintf '%s\\n' '${stage}' >>"$LOG_PATH"\nexit ${exitCode}\n`,
    );
    await Deno.chmod(path, 0o755);
    commands[stage] = path;
  }
  return { directory, log, commands };
}

function runCutover(fixture: Fixture): Promise<Deno.CommandOutput> {
  return new Deno.Command("sh", {
    args: [
      cutoverScript,
      "--drain-command",
      fixture.commands.drain,
      "--ownership-command",
      fixture.commands.ownership,
      "--preflight-command",
      fixture.commands.preflight,
      "--switch-command",
      fixture.commands.switch,
      "--verify-command",
      fixture.commands.verify,
    ],
    env: { LOG_PATH: fixture.log },
    stdout: "piped",
    stderr: "piped",
  }).output();
}
