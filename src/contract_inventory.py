from __future__ import annotations

import ast
import json
import re
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from .surface_policy import DASHBOARD_SURFACE_POLICIES, NON_HTTP_SURFACE_POLICIES


def _dashboard_ast() -> tuple[ast.ClassDef, ast.FunctionDef]:
    source_path = Path(__file__).parent / "dashboard" / "server.py"
    module = ast.parse(source_path.read_text())
    dashboard = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "DashboardApp"
    )
    dispatch = next(
        node
        for node in dashboard.body
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch"
    )
    return dashboard, dispatch


def _string_constants(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _methods(predicate: ast.AST) -> list[str]:
    methods: set[str] = set()
    for comparison in (
        node for node in ast.walk(predicate) if isinstance(node, ast.Compare)
    ):
        if not (
            isinstance(comparison.left, ast.Attribute)
            and isinstance(comparison.left.value, ast.Name)
            and comparison.left.value.id == "handler"
            and comparison.left.attr == "command"
        ):
            continue
        methods.update(
            value
            for comparator in comparison.comparators
            for value in _string_constants(comparator)
        )
    return sorted(methods)


def _paths(predicate: ast.AST) -> dict[str, list[str]]:
    paths: dict[str, set[str]] = {"exact": set(), "prefix": set(), "suffix": set()}
    for node in ast.walk(predicate):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
            paths["exact"].update(value for value in _string_constants(node) if value.startswith("/"))
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "path"):
            continue
        if node.func.attr in {"startswith", "endswith"}:
            key = "prefix" if node.func.attr == "startswith" else "suffix"
            paths[key].update(value for value in _string_constants(node) if value.startswith("/"))
    return {key: sorted(values) for key, values in paths.items() if values}


def _mapping_keys(node: ast.AST) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    return sorted({
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    })


def _handler_contract(
    method: ast.FunctionDef,
    methods: dict[str, ast.FunctionDef],
    visited: set[str] | None = None,
) -> dict[str, Any]:
    visited = set(visited or ())
    if method.name in visited:
        return {}
    visited.add(method.name)
    statuses = {
        node.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "HTTPStatus"
    }
    content_types: set[str] = set()
    inputs: dict[str, set[str]] = {"form": set(), "json": set(), "query": set()}
    response_keys: set[str] = set()
    json_shapes: set[str] = set()
    redirects: set[str] = set()
    errors: set[str] = set()
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr == "get":
            if isinstance(call.func.value, ast.Name) and call.func.value.id in inputs and call.args:
                inputs[call.func.value.id].update(_string_constants(call.args[0]))
        if not isinstance(call.func, ast.Attribute):
            continue
        name = call.func.attr
        content_types.update({
            "_send_json": {"application/json"},
            "_send_html": {"text/html; charset=utf-8"},
            "_send_text": {"text/plain; charset=utf-8"},
            "_redirect": set(),
        }.get(name, set()))
        if name == "_send_json" and len(call.args) >= 3:
            json_shapes.add(ast.unparse(call.args[2]))
            keys = _mapping_keys(call.args[2])
            response_keys.update(keys)
            if "error" in keys and isinstance(call.args[2], ast.Dict):
                for key, value in zip(call.args[2].keys, call.args[2].values):
                    if isinstance(key, ast.Constant) and key.value == "error":
                        errors.add(ast.unparse(value))
        if name == "_send_text" and len(call.args) >= 3:
            status_source = ast.unparse(call.args[1])
            if status_source != "HTTPStatus.OK":
                errors.add(ast.unparse(call.args[2]))
        if name == "_redirect" and len(call.args) >= 2:
            redirects.add(ast.unparse(call.args[1]))
        if name == "send_header" and len(call.args) >= 2:
            if isinstance(call.args[0], ast.Constant) and call.args[0].value == "Content-Type":
                content_types.update(_string_constants(call.args[1]))

    call_names = {
        call.func.attr
        for call in calls
        if isinstance(call.func, ast.Attribute)
    }
    if "_read_json_body" in call_names:
        statuses.update({"BAD_REQUEST", "REQUEST_ENTITY_TOO_LARGE"})
        content_types.add("application/json")
        errors.update({repr(value) for value in (
            "invalid_content_length", "missing_body", "body_too_large",
            "invalid_json", "invalid_payload",
        )})
    if "_read_form_body" in call_names:
        statuses.update({"BAD_REQUEST", "REQUEST_ENTITY_TOO_LARGE"})
        content_types.add("text/plain; charset=utf-8")
        errors.update({repr(value) for value in (
            "Invalid Content-Length", "Missing form body", "Form body too large",
            "Invalid form body",
        )})
    if redirects:
        statuses.add("FOUND")
    contract = {
        "statuses": sorted(statuses),
        "content_types": sorted(content_types),
        "inputs": {key: sorted(values) for key, values in inputs.items() if values},
        "json_response_keys": sorted(response_keys),
        "json_shapes": sorted(json_shapes),
        "redirects": sorted(redirects),
        "errors": sorted(errors),
    }
    response_dependencies = {
        "_read_form_body", "_read_json_body", "_read_session", "_require_ingest_auth",
        "_require_session", "_send_quota_exceeded",
    }
    delegated = {
        call.func.attr
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and (call.func.attr.startswith("_serve_") or call.func.attr in response_dependencies)
        and call.func.attr in methods
    }
    for handler_name in delegated:
        child = _handler_contract(methods[handler_name], methods, visited)
        for key in (
            "statuses", "content_types", "json_response_keys", "json_shapes", "redirects", "errors",
        ):
            contract[key] = sorted(set(contract[key]) | set(child.get(key, [])))
        for input_type, fields in child.get("inputs", {}).items():
            contract["inputs"][input_type] = sorted(
                set(contract["inputs"].get(input_type, [])) | set(fields)
            )
    return contract


def dashboard_route_inventory() -> list[dict[str, Any]]:
    """Return the stable, machine-readable routes declared by DashboardApp.dispatch."""
    dashboard, dispatch = _dashboard_ast()
    methods = {
        node.name: node
        for node in dashboard.body
        if isinstance(node, ast.FunctionDef)
    }
    routes: list[dict[str, Any]] = []
    for branch in (node for node in dispatch.body if isinstance(node, ast.If)):
        handlers = sorted({
            call.func.attr
            for call in ast.walk(branch)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr.startswith("_serve_")
        })
        if not handlers:
            continue
        handler = handlers[0]
        response = _handler_contract(methods[handler], methods)
        route_methods = _methods(branch.test)
        if set(route_methods) & {"POST", "PUT", "PATCH", "DELETE"} and handler != "_serve_twitch_eventsub":
            response["statuses"] = sorted(set(response["statuses"]) | {"FORBIDDEN"})
            response["content_types"] = sorted(set(response["content_types"]) | {"application/json"})
            response["json_response_keys"] = sorted(set(response["json_response_keys"]) | {"error"})
            response["json_shapes"] = sorted(
                set(response["json_shapes"]) | {"{'error': 'origin_mismatch'}"}
            )
            response["errors"] = sorted(set(response["errors"]) | {"'origin_mismatch'"})
        routes.append({
            "methods": route_methods,
            "paths": _paths(branch.test),
            "predicate": ast.unparse(branch.test),
            "handler": handler,
            "policy": vars(DASHBOARD_SURFACE_POLICIES[handler]),
            "response": response,
        })
    return routes


def _source_strings(path: Path, pattern: str) -> list[str]:
    return sorted(set(re.findall(pattern, path.read_text())))


def compatibility_inventory() -> dict[str, Any]:
    """Return frozen Python compatibility surfaces used by the Deno port."""
    source_root = Path(__file__).parent
    config_variables = _source_strings(source_root / "config.py", r'["\'](QBOT_[A-Z0-9_]+)["\']')
    schema_objects = _source_strings(
        source_root / "db.py",
        r"(?i)CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)",
    )
    cli_commands = _source_strings(source_root / "__main__.py", r'add_parser\(\s*["\']([^"\']+)["\']')
    return {
        "format_version": 1,
        "http_routes": dashboard_route_inventory(),
        "non_http_surfaces": {
            name: vars(policy) for name, policy in sorted(NON_HTTP_SURFACE_POLICIES.items())
        },
        "configuration": {
            "variables": config_variables,
            "sensitive_variables": [
                name for name in config_variables
                if any(marker in name for marker in ("SECRET", "TOKEN", "ENCRYPTION_KEY"))
            ],
        },
        "cli_commands": cli_commands,
        "schema_objects": schema_objects,
        "signed_payloads": {
            "session": ["community_id", "expires_at", "role", "session_version", "user_id", "username"],
            "discord_install_state": ["community_id", "expires_at", "guild_id", "nonce", "operator_id"],
            "twitch_install_state": [
                "broadcaster_login", "community_id", "expires_at", "nonce", "operator_id", "scopes",
            ],
        },
        "cryptographic_contracts": {
            "session_cookie": {
                "name": "qbot4k_session",
                "payload_encoding": "base64url(json-utf8)",
                "signature": "lowercase-hex(hmac-sha256(secret, encoded-payload-ascii))",
                "wire_format": "encoded-payload.signature",
                "attributes": ["Path=/", "HttpOnly", "SameSite=Lax", "Secure when HTTPS"],
            },
            "login_oauth_state": {
                "name": "qbot4k_oauth_state",
                "generation": "token_urlsafe(24)",
                "comparison": "constant-time",
            },
            "installation_oauth_state": {
                "payload_encoding": "base64url(json-utf8)",
                "signature": "lowercase-hex(hmac-sha256(secret, encoded-payload-ascii))",
                "wire_format": "encoded-payload.signature",
            },
            "twitch_eventsub": {
                "message": "message-id-utf8 + timestamp-utf8 + raw-body",
                "signature": "sha256=lowercase-hex(hmac-sha256(secret, message))",
                "comparison": "constant-time",
                "maximum_age_seconds": 600,
            },
        },
        "webhook_signature_headers": [
            "Twitch-Eventsub-Message-Id", "Twitch-Eventsub-Message-Signature",
            "Twitch-Eventsub-Message-Timestamp", "Twitch-Eventsub-Message-Type",
        ],
    }


def main() -> int:
    parser = ArgumentParser(description="Generate QBot4K compatibility contracts")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(compatibility_inventory(), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())