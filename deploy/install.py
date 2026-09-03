#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pwd
import grp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


NAME_PATTERN = "letters, numbers, dot, underscore, and hyphen"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install QBot4K behind systemd and optionally nginx."
    )
    parser.add_argument("--name", default="qbot4k", help="Stable service/config name")
    parser.add_argument("-d", "--dir", dest="project_dir", type=Path)
    parser.add_argument("-u", "--user", dest="app_user")
    parser.add_argument("-g", "--group", dest="app_group")
    parser.add_argument("--description", default="QBot4K intelligence platform")
    parser.add_argument("-p", "--port", type=int, default=8080)
    parser.add_argument("-e", "--env", dest="env_source", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, default=Path("/etc"))
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--skip-deps", action="store_true")
    parser.add_argument("-n", "--domain", default="localhost")
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--http-only", action="store_true")
    parser.add_argument("--skip-nginx", action="store_true")
    parser.add_argument("--nginx-dir", type=Path, default=Path("/etc/nginx/conf.d"))
    parser.add_argument("--client-max-body", default="1m")
    parser.add_argument("--acme-root", type=Path, default=Path("/var/lib/letsencrypt"))
    parser.add_argument("--systemd-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def fail(message: str) -> None:
    raise SystemExit(message)


def validate_name(name: str) -> None:
    if not name or not name[0].isalnum() or any(
        not (character.isalnum() or character in "._-") for character in name
    ):
        fail(f"--name may contain only {NAME_PATTERN}.")


def reject_newlines(label: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        fail(f"{label} must not contain newlines.")


def systemd_quote(value: str) -> str:
    reject_newlines("systemd value", value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_template(path: Path, values: dict[str, str]) -> str:
    content = path.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace(f"__{key}__", value)
    if "__" in content:
        unresolved = sorted({part.split("__", 1)[0] for part in content.split("__")[1::2]})
        fail(f"Unresolved placeholder in {path.name}: {', '.join(unresolved)}")
    return content


def run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def service_identity(project_dir: Path, user: str | None, group: str | None) -> tuple[str, str]:
    owner = pwd.getpwuid(project_dir.stat().st_uid).pw_name
    app_user = user or owner
    try:
        user_record = pwd.getpwnam(app_user)
    except KeyError:
        fail(f"Service user does not exist: {app_user}")
    app_group = group or grp.getgrgid(user_record.pw_gid).gr_name
    try:
        grp.getgrnam(app_group)
    except KeyError:
        fail(f"Service group does not exist: {app_group}")
    return app_user, app_group


def resolve_tls(args: argparse.Namespace) -> tuple[bool, Path, Path]:
    certificate = args.cert or Path(f"/etc/letsencrypt/live/{args.domain}/fullchain.pem")
    key = args.key or Path(f"/etc/letsencrypt/live/{args.domain}/privkey.pem")
    if args.http_only:
        return False, certificate, key
    if certificate.is_file() and key.is_file():
        return True, certificate, key
    if args.cert is not None or args.key is not None:
        fail("Both TLS certificate files must exist, or use --http-only.")
    return False, certificate, key


def ensure_python(executable: str) -> Path:
    resolved = shutil.which(executable) if not os.path.isabs(executable) else executable
    if not resolved or not Path(resolved).is_file():
        fail(f"Python executable not found: {executable}")
    result = subprocess.run(
        [resolved, "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"],
        check=False,
    )
    if result.returncode != 0:
        fail("Python 3.11 or newer is required.")
    return Path(resolved).resolve()


def install_dependencies(python: Path, project_dir: Path, venv_dir: Path) -> None:
    run([str(python), "-m", "venv", str(venv_dir)])
    run([
        str(venv_dir / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--requirement",
        str(project_dir / "requirements.txt"),
    ])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_name(args.name)
    if not 1 <= args.port <= 65535:
        fail("--port must be an integer between 1 and 65535.")
    for label, value in (
        ("description", args.description),
        ("domain", args.domain),
        ("client body size", args.client_max_body),
    ):
        reject_newlines(label, value)

    deploy_dir = Path(__file__).resolve().parent
    project_dir = (args.project_dir or deploy_dir.parent).expanduser().resolve()
    if not project_dir.is_dir():
        fail(f"Project directory not found: {project_dir}")
    if not (project_dir / "requirements.txt").is_file() or not (project_dir / "src").is_dir():
        fail(f"QBot4K source and requirements.txt were not found in {project_dir}")
    args.env_source = args.env_source.expanduser().resolve()
    if not args.env_source.is_file():
        fail(f"Environment source not found: {args.env_source}")

    python = ensure_python(args.python_executable)
    app_user, app_group = service_identity(project_dir, args.app_user, args.app_group)
    use_tls, certificate, certificate_key = resolve_tls(args)
    config_dir = args.config_root / args.name
    env_dest = config_dir / f"{args.name}.env"
    service_dest = args.systemd_dir / f"{args.name}.service"
    nginx_dest = args.nginx_dir / f"{args.name}.conf"
    venv_dir = project_dir / ".venv"
    app_python = venv_dir / "bin" / "python"

    service = render_template(deploy_dir / "systemd.service.template", {
        "DESCRIPTION": args.description,
        "APP_USER": app_user,
        "APP_GROUP": app_group,
        "APP_DIR": systemd_quote(str(project_dir)),
        "CONFIG_DIR": systemd_quote(str(config_dir)),
        "PORT": str(args.port),
        "ENV_FILE": systemd_quote(str(env_dest)),
        "PYTHON": systemd_quote(str(app_python)),
    })
    nginx_template = deploy_dir / (
        "nginx.https.conf.template" if use_tls else "nginx.http.conf.template"
    )
    nginx = render_template(nginx_template, {
        "SERVER_NAME": args.domain,
        "PORT": str(args.port),
        "CLIENT_MAX_BODY_SIZE": args.client_max_body,
        "ACME_ROOT": str(args.acme_root),
        "SSL_CERTIFICATE": str(certificate),
        "SSL_CERTIFICATE_KEY": str(certificate_key),
    })

    if args.dry_run:
        print(f"--- create/update Python virtualenv: {venv_dir}")
        if not args.skip_deps:
            print(f"--- install dependencies: {project_dir / 'requirements.txt'}")
        print(f"--- {service_dest}\n{service}", end="")
        if not args.skip_nginx:
            print(f"--- {nginx_dest}\n{nginx}", end="")
        print(f"--- copy {args.env_source} -> {env_dest} (mode 0600)")
        return 0

    if os.geteuid() != 0:
        fail("Run this installer as root, or use --dry-run.")
    if shutil.which("systemctl") is None:
        fail("systemctl is required.")
    if not args.skip_nginx and shutil.which("nginx") is None:
        fail("nginx is required unless --skip-nginx is used.")

    if not args.skip_deps:
        install_dependencies(python, project_dir, venv_dir)
    if not app_python.is_file():
        fail(f"Virtualenv Python not found: {app_python}; rerun without --skip-deps.")
    run(["runuser", "-u", app_user, "--", "test", "-x", str(app_python)])

    args.systemd_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.env_source, env_dest)
    os.chmod(env_dest, 0o600)
    shutil.chown(env_dest, user=app_user, group=app_group)
    for command_name in ("check-config", "init-db"):
        run([
            "runuser", "-u", app_user, "--",
            str(app_python), "-m", "src",
            "--env-file", str(env_dest), command_name,
        ], cwd=project_dir)
    service_dest.write_text(service, encoding="utf-8")
    os.chmod(service_dest, 0o644)

    with tempfile.TemporaryDirectory(prefix=f"{args.name}-deploy-") as work_dir:
        backup = Path(work_dir) / "nginx.conf"
        had_nginx_config = nginx_dest.is_file()
        if not args.skip_nginx:
            args.nginx_dir.mkdir(parents=True, exist_ok=True)
            if had_nginx_config:
                shutil.copy2(nginx_dest, backup)
            nginx_dest.write_text(nginx, encoding="utf-8")
            os.chmod(nginx_dest, 0o644)
            try:
                run(["nginx", "-t"])
            except subprocess.CalledProcessError:
                if had_nginx_config:
                    shutil.copy2(backup, nginx_dest)
                else:
                    nginx_dest.unlink(missing_ok=True)
                fail("nginx validation failed; restored the previous configuration.")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", f"{args.name}.service"])
    run(["systemctl", "restart", f"{args.name}.service"])
    if not args.skip_nginx:
        run(["systemctl", "enable", "nginx"])
        run(["systemctl", "reload", "nginx"])

    print(f"Installed systemd unit: {service_dest}")
    print(f"Installed environment file: {env_dest}")
    if not args.skip_nginx:
        print(f"Installed nginx config: {nginx_dest}")
        if not use_tls:
            print("TLS is not enabled. Obtain certificates and rerun without --http-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
