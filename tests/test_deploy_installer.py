from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install.py"


class DeployInstallerTests(unittest.TestCase):
    def run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INSTALLER), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_dry_run_renders_python_systemd_and_http_nginx(self) -> None:
        result = self.run_installer(
            "--env", ".env.example",
            "--domain", "intelligence.example.com",
            "--http-only",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/.venv/bin/python\" -m src", result.stdout)
        self.assertIn("EnvironmentFile=-\"/etc/qbot4k/qbot4k.env\"", result.stdout)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", result.stdout)
        self.assertIn("location /api/live-ops/stream", result.stdout)
        self.assertNotIn("deno", result.stdout.casefold())

    def test_dry_run_selects_https_when_explicit_certificates_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            certificate = Path(tmpdir) / "fullchain.pem"
            key = Path(tmpdir) / "privkey.pem"
            certificate.write_text("certificate", encoding="utf-8")
            key.write_text("key", encoding="utf-8")
            result = self.run_installer(
                "--env", ".env.example",
                "--domain", "intelligence.example.com",
                "--cert", str(certificate),
                "--key", str(key),
                "--dry-run",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("listen 443 ssl;", result.stdout)
        self.assertIn(f"ssl_certificate {certificate};", result.stdout)
        self.assertIn("return 301 https://$host$request_uri;", result.stdout)

    def test_invalid_name_and_missing_certificate_fail_before_rendering(self) -> None:
        invalid_name = self.run_installer(
            "--name", "bad name",
            "--env", ".env.example",
            "--dry-run",
        )
        missing_certificate = self.run_installer(
            "--env", ".env.example",
            "--cert", "/missing/fullchain.pem",
            "--key", "/missing/privkey.pem",
            "--dry-run",
        )

        self.assertNotEqual(invalid_name.returncode, 0)
        self.assertIn("--name may contain only", invalid_name.stderr)
        self.assertNotEqual(missing_certificate.returncode, 0)
        self.assertIn("Both TLS certificate files must exist", missing_certificate.stderr)


if __name__ == "__main__":
    unittest.main()
