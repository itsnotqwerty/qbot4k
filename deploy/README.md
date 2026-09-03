# QBot4K Deploy

This directory provides a Python-based systemd and nginx deployment flow modeled after `donut-deploy`. It uses a Python virtual environment and `requirements.txt`; Deno is not installed or required.

## Requirements

- Linux with systemd
- Python 3.11 or newer with `venv`
- nginx unless `--skip-nginx` is used
- An existing service user, or the owner of the project directory
- A configured QBot4K environment file supplied with `--env`

Web deployments must set `QBOT_LEGAL_ORGANIZATION_NAME`,
`QBOT_LEGAL_CONTACT_EMAIL`, `QBOT_LEGAL_JURISDICTION`, and
`QBOT_LEGAL_EFFECTIVE_DATE`. These values appear on the public privacy policy
and terms of service; configuration validation rejects a web deployment while
any value is unresolved.

## Preview

Dry-run does not require root and does not modify the host:

```bash
python deploy/install.py \
  --domain intelligence.example.com \
  --env .env.production \
  --http-only \
  --dry-run
```

## Install

```bash
sudo python deploy/install.py \
  --name qbot4k \
  --domain intelligence.example.com \
  --env .env.production \
  --user qbot4k \
  --group qbot4k
```

The installer creates or refreshes `.venv`, installs `requirements.txt`, renders the systemd unit, copies the environment to `/etc/qbot4k/qbot4k.env` with mode `0600`, runs `check-config` and `init-db` as the service user, validates nginx, and restarts only QBot4K and nginx. Use `--skip-deps` when the virtual environment was prepared from the same release and `--skip-nginx` when another ingress owns the domain.

## TLS

When both certificate files exist, HTTPS is enabled and HTTP redirects to HTTPS. Defaults use `/etc/letsencrypt/live/<domain>/fullchain.pem` and `privkey.pem`. The installer does not obtain certificates.

A typical bootstrap is:

1. Install with `--http-only`.
2. Obtain a certificate with an ACME client.
3. Rerun without `--http-only`.

Use `--cert` and `--key` for custom paths. If either explicit path is missing, installation fails before changing system configuration.

## Installed files

For the default `qbot4k` name:

- `/etc/systemd/system/qbot4k.service`
- `/etc/qbot4k/qbot4k.env`
- `/etc/nginx/conf.d/qbot4k.conf` unless `--skip-nginx` is used

Rerunning updates the same project-owned files. nginx configuration is staged and tested; failed validation restores the previous file. The environment source is copied rather than referenced from the repository.

Run `python deploy/install.py --help` for path, identity, port, TLS, and dry-run options. The root [install.sh](../install.sh) remains the release-directory installer for `/opt/qbot4k`; this deploy kit is intended for an already checked-out application directory.
