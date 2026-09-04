# QBot4K Deploy

This directory provides the systemd, nginx, authorization, cutover, and rollback
artifacts used by the Deno/Fresh release installer at `../install.sh`.

## Requirements

- Linux with systemd
- Deno 2.9.4
- nginx
- A configured QBot4K environment file at `/etc/qbot4k/qbot4k.env`

Web deployments must set `QBOT_LEGAL_ORGANIZATION_NAME`,
`QBOT_LEGAL_CONTACT_EMAIL`, `QBOT_LEGAL_JURISDICTION`, and
`QBOT_LEGAL_EFFECTIVE_DATE`. These values appear on the public privacy policy
and terms of service; configuration validation rejects a web deployment while
any value is unresolved.

## Install

```bash
sudo ./install.sh
```

Run this command from the root of an extracted release. The installer provisions
the pinned Deno runtime, release directory, role-specific systemd units, nginx
configuration, persistent state, and permission-bounded service account.

## TLS

When both certificate files exist, HTTPS is enabled and HTTP redirects to HTTPS.
Defaults use `/etc/letsencrypt/live/<domain>/fullchain.pem` and `privkey.pem`.
The installer does not obtain certificates.

A typical bootstrap is:

1. Install with `--http-only`.
2. Obtain a certificate with an ACME client.
3. Rerun without `--http-only`.

Use `--cert` and `--key` for custom paths. If either explicit path is missing,
installation fails before changing system configuration.

## Installed files

For the default `qbot4k` name:

- `/etc/systemd/system/qbot4k-web.service`
- `/etc/systemd/system/qbot4k-jobs.service`
- `/etc/systemd/system/qbot4k-analysis.service`
- `/etc/qbot4k/qbot4k.env`
- `/etc/nginx/conf.d/qbot4k.conf` unless `--skip-nginx` is used

Rerunning updates the same project-owned files. nginx configuration is staged
and tested; failed validation restores the previous file. The environment source
is copied rather than referenced from the repository. Units invoke the
permission-bounded role tasks in `deno.json`, stop through SIGTERM, and serve
Fresh static assets through nginx with immutable caching for hashed assets.

## Blue/green upstream switch

Stage the release with `sudo ./install.sh --no-start`, configure the inactive
web unit's loopback port and `QBOT_WEB_READ_ONLY=true`, then start it without
changing nginx. The read-only profile permits health and observational requests
but returns `503` before controllers run for HTTP mutations and OAuth callbacks.
Remove the setting only as part of the audited ownership cutover.

After the inactive web role reports ready on its loopback port, switch nginx
with the project-owned helper:

```bash
sudo deploy/switch-nginx-upstream.sh \
  --config /etc/nginx/conf.d/qbot4k.conf \
  --target-port 8081 \
  --public-health-url https://intelligence.example.com/health/ready
```

The helper checks the target directly, atomically changes only generated
loopback `proxy_pass` entries, validates nginx, reloads it, and checks public
readiness. A validation, reload, or public-health failure restores the previous
upstream and reloads nginx. Success and rollback paths emit a JSON record with
`duration_ms`; retain it with the cutover evidence. Run
`deno task test:nginx-switchback` to validate both paths with isolated command
fixtures.

The root [install.sh](../install.sh) is the sole production installer for
`/opt/qbot4k`. This directory contains the templates and operational helpers it
packages.
