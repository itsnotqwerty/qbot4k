from __future__ import annotations

import json
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
) -> dict[str, object]:
    states = {service: "disabled" for service in ("web", "jobs", "twitch", "discord")}
    for service in settings.enabled_services:
        states[service] = "ready"

    if service_states:
        states.update(service_states)

    database_state = database_health(settings.database_path)
    overall_status = "ready" if database_state["status"] == "ready" else "degraded"

    return {
        "status": overall_status,
        "database": database_state,
        "services": states,
    }


class HealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        settings: AppSettings,
        service_states: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(server_address, HealthRequestHandler)
        self.settings = settings
        self.service_states = dict(service_states or {})
        self.dashboard_app = DashboardApp(settings, self.service_states)


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
) -> HealthServer:
    return HealthServer(
        (settings.dashboard_host, settings.dashboard_port),
        settings,
        service_states,
    )