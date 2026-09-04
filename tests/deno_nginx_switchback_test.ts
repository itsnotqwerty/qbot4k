import { assertEquals, assertStringIncludes } from "jsr:@std/assert@1.0.14";

const script = new URL(
  "../deploy/switch-nginx-upstream.sh",
  import.meta.url,
).pathname;

Deno.test("nginx switchback validates and activates a healthy upstream", async () => {
  const fixture = await createFixture();
  try {
    const result = await runSwitch(fixture, false);
    assertEquals(result.code, 0);
    assertStringIncludes(
      new TextDecoder().decode(result.stdout),
      '"result":"switched","previous_port":8080,"active_port":8081',
    );
    assertStringIncludes(
      new TextDecoder().decode(result.stdout),
      '"duration_ms":',
    );
    const config = await Deno.readTextFile(fixture.config);
    assertEquals(config.includes("127.0.0.1:8080"), false);
    assertEquals(config.match(/127\.0\.0\.1:8081/g)?.length, 2);
    const calls = await Deno.readTextFile(fixture.log);
    assertStringIncludes(calls, "curl http://127.0.0.1:8081/health/ready");
    assertStringIncludes(calls, "curl https://qbot.example/health/ready");
    assertStringIncludes(calls, "nginx -t");
    assertStringIncludes(calls, "systemctl reload nginx");
  } finally {
    await Deno.remove(fixture.directory, { recursive: true });
  }
});

Deno.test("nginx switchback restores the prior upstream on failed public health", async () => {
  const fixture = await createFixture();
  try {
    const result = await runSwitch(fixture, true);
    assertEquals(result.code, 1);
    assertStringIncludes(
      new TextDecoder().decode(result.stderr),
      "public readiness failed; restored port 8080",
    );
    assertStringIncludes(
      new TextDecoder().decode(result.stderr),
      '"result":"rolled_back","previous_port":8080,"attempted_port":8081,"duration_ms":',
    );
    const config = await Deno.readTextFile(fixture.config);
    assertEquals(config.match(/127\.0\.0\.1:8080/g)?.length, 2);
    assertEquals(config.includes("127.0.0.1:8081"), false);
    const calls = await Deno.readTextFile(fixture.log);
    assertEquals(calls.match(/systemctl reload nginx/g)?.length, 2);
  } finally {
    await Deno.remove(fixture.directory, { recursive: true });
  }
});

interface Fixture {
  readonly directory: string;
  readonly config: string;
  readonly log: string;
  readonly nginx: string;
  readonly systemctl: string;
  readonly curl: string;
}

async function createFixture(): Promise<Fixture> {
  const directory = await Deno.makeTempDir();
  const config = `${directory}/qbot4k.conf`;
  const log = `${directory}/calls.log`;
  await Deno.writeTextFile(
    config,
    `server {
  location / { proxy_pass http://127.0.0.1:8080; }
  location /health/ready { proxy_pass http://127.0.0.1:8080; }
}\n`,
  );
  const nginx = await executable(
    directory,
    "nginx",
    `
    printf 'nginx %s\\n' "$*" >>"$LOG_PATH"
    exit 0
  `,
  );
  const systemctl = await executable(
    directory,
    "systemctl",
    `
    printf 'systemctl %s\\n' "$*" >>"$LOG_PATH"
    exit 0
  `,
  );
  const curl = await executable(
    directory,
    "curl",
    `
    url=''
    for argument do url=$argument; done
    printf 'curl %s\\n' "$url" >>"$LOG_PATH"
    if [ "\${FAIL_PUBLIC-}" = 1 ] && [ "$url" = 'https://qbot.example/health/ready' ]; then
      exit 22
    fi
    exit 0
  `,
  );
  return { directory, config, log, nginx, systemctl, curl };
}

async function executable(
  directory: string,
  name: string,
  body: string,
): Promise<string> {
  const path = `${directory}/${name}`;
  await Deno.writeTextFile(path, `#!/bin/sh\nset -eu\n${body}`);
  await Deno.chmod(path, 0o755);
  return path;
}

function runSwitch(
  fixture: Fixture,
  failPublic: boolean,
): Promise<Deno.CommandOutput> {
  return new Deno.Command("sh", {
    args: [
      script,
      "--config",
      fixture.config,
      "--target-port",
      "8081",
      "--public-health-url",
      "https://qbot.example/health/ready",
      "--nginx-bin",
      fixture.nginx,
      "--systemctl-bin",
      fixture.systemctl,
      "--curl-bin",
      fixture.curl,
    ],
    env: {
      LOG_PATH: fixture.log,
      FAIL_PUBLIC: failPublic ? "1" : "0",
    },
    stdout: "piped",
    stderr: "piped",
  }).output();
}
