from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


def evaluate(operation: str, value: Any) -> Any:
    if operation == "authorize_cases":
        return [
            {
                "authorized": (
                    case["actor_community_id"] == case["requested_community_id"]
                    and case["required_capability"] in case["granted_capabilities"]
                ),
                "reason": (
                    "tenant_mismatch"
                    if case["actor_community_id"] != case["requested_community_id"]
                    else "allowed" if case["required_capability"] in case["granted_capabilities"]
                    else "capability_denied"
                ),
            }
            for case in value
        ]
    if operation == "select_tenant":
        return [
            record for record in value["records"]
            if record["community_id"] == value["community_id"]
        ]
    if operation == "project":
        return {field: value["record"][field] for field in value["fields"]}
    if operation == "sort_jobs":
        return sorted(value, key=lambda job: (-job["priority"], job["id"]))
    if operation == "parse_command":
        parts = value.strip().split()
        return {"name": parts[0].removeprefix("!").casefold(), "arguments": parts[1:]}
    if operation == "normalize_provider":
        return {
            "external_event_id": str(value["external_event_id"]),
            "platform": value["platform"].strip().casefold(),
            "username": value["username"].strip(),
        }
    if operation == "normalize_html":
        return re.sub(r"\s+", " ", value).strip()
    raise ValueError(f"unsupported fixture operation: {operation}")


def run_fixtures(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text())
    return {
        scenario["id"]: evaluate(scenario["operation"], scenario["input"])
        for scenario in fixture["scenarios"]
    }


if __name__ == "__main__":
    print(json.dumps(run_fixtures(Path(sys.argv[1])), sort_keys=True))