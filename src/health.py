from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib.parse import urlparse

from .config import AppSettings
from .db import database_health
from .dashboard.server import DashboardApp


def build_health_snapshot(
    settings: AppSettings,
    service_states: Mapping[str, str] | None = None,
    service_started_at: Mapping[str, str] | None = None,
    app_started_at: str | None = None,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    states = {service: "disabled" for service in ("web", "jobs", "twitch", "discord")}
    for service in settings.enabled_services:
        states[service] = "ready"

    if service_states:
        states.update(service_states)

    service_uptime = dict(service_started_at or {})
    service_details: dict[str, dict[str, object]] = {}
    for service_name, status in states.items():
        started_at = service_uptime.get(service_name)
        service_details[service_name] = {
            "status": status,
            "started_at": started_at,
            "uptime_seconds": _uptime_seconds(started_at, now),
        }

    database_state = database_health(settings.database_path)
    services_healthy = all(status in {"ready", "disabled"} for status in states.values())
    overall_status = "ready" if database_state["status"] == "ready" and services_healthy else "degraded"
    resolved_app_started_at = app_started_at or service_uptime.get("web")

    return {
        "status": overall_status,
        "database": database_state,
        "services": states,
        "services_detail": service_details,
        "uptime": {
            "app_started_at": resolved_app_started_at,
            "app_uptime_seconds": _uptime_seconds(resolved_app_started_at, now),
        },
    }


def _uptime_seconds(started_at: str | None, now: datetime) -> int | None:
    if not started_at:
        return None
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = int((now - parsed.astimezone(timezone.utc)).total_seconds())
    return max(seconds, 0)


class HealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        settings: AppSettings,
        service_states: Mapping[str, str] | None = None,
        service_started_at: Mapping[str, str] | None = None,
        app_started_at: str | None = None,
    ) -> None:
        super().__init__(server_address, HealthRequestHandler)
        self.settings = settings
        self.service_states = service_states if service_states is not None else {}
        self.service_started_at = dict(service_started_at or {})
        self.app_started_at = app_started_at
        self.dashboard_app = DashboardApp(
            settings,
            self.service_states,
            service_started_at=self.service_started_at,
            app_started_at=self.app_started_at,
        )


class HealthRequestHandler(BaseHTTPRequestHandler):
    server: HealthServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/health", "/health/live", "/health/ready"}:
            if self.server.dashboard_app.dispatch(self):
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        snapshot = build_health_snapshot(
            self.server.settings,
            self.server.service_states,
            service_started_at=self.server.service_started_at,
            app_started_at=self.server.app_started_at,
        )
        status_code = HTTPStatus.OK
        if parsed.path == "/health/ready" and snapshot["status"] != "ready":
            status_code = HTTPStatus.SERVICE_UNAVAILABLE

        response = json.dumps(snapshot, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_POST(self) -> None:
        if self.server.dashboard_app.dispatch(self):
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_health_server(
    settings: AppSettings,
    service_states: Mapping[str, str] | None = None,
    service_started_at: Mapping[str, str] | None = None,
    app_started_at: str | None = None,
) -> HealthServer:
    return HealthServer(
        (settings.dashboard_host, settings.dashboard_port),
        settings,
        service_states,
        service_started_at,
        app_started_at,
    )