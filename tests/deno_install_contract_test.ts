import { assertStringIncludes } from "jsr:@std/assert@1.0.14";

Deno.test("shell installer renders the configured blue-green web port", async () => {
  const installer = await Deno.readTextFile(
    new URL("../install.sh", import.meta.url),
  );
  assertStringIncludes(
    installer,
    "WEB_PORT=$(sed -n 's/^[[:space:]]*QBOT_DASHBOARD_PORT=//p'",
  );
  assertStringIncludes(
    installer,
    "--allow-net=127.0.0.1:${WEB_PORT}",
  );
  assertStringIncludes(
    installer,
    "http://127.0.0.1:${WEB_PORT}/health/ready",
  );
  assertStringIncludes(installer, "s|__PORT__|${WEB_PORT}|g");
  assertStringIncludes(
    installer,
    "s|__APP_DIR__|/opt/qbot4k/current|g",
  );
});

Deno.test("systemd port overrides the environment file for the web runtime", async () => {
  const template = await Deno.readTextFile(
    new URL("../deploy/systemd.service.template", import.meta.url),
  );
  assertStringIncludes(template, "Environment=PORT=__PORT__");
  assertStringIncludes(template, "Environment=QBOT_DASHBOARD_PORT=__PORT__");
  assertStringIncludes(template, "EnvironmentFile=-__ENV_FILE__");
  assertStringIncludes(
    template,
    "ExecStart=__DENO__ task --config __APP_DIR__/deno.json role:__ROLE__",
  );
});

Deno.test("systemd and installers restrict runtime secrets and writes", async () => {
  const template = await Deno.readTextFile(
    new URL("../deploy/systemd.service.template", import.meta.url),
  );
  const releaseInstaller = await Deno.readTextFile(
    new URL("../install.sh", import.meta.url),
  );
  for (
    const directive of [
      "NoNewPrivileges=true",
      "PrivateTmp=true",
      "ProtectSystem=strict",
      "ProtectHome=read-only",
      "UMask=0077",
    ]
  ) {
    assertStringIncludes(template, directive);
  }
  assertStringIncludes(releaseInstaller, 'chmod 0640 "$CONFIG_FILE"');
});

Deno.test("shell installer packages the cutover preflight command", async () => {
  const installer = await Deno.readTextFile(
    new URL("../install.sh", import.meta.url),
  );
  assertStringIncludes(installer, 'cutover_preflight.ts" ] || fail');
  assertStringIncludes(installer, "cutover_monitor.ts cutover_preflight.ts");
  assertStringIncludes(installer, 'deploy/execute-cutover.sh" ] || fail');
});
