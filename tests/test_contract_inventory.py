from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from src.contract_inventory import compatibility_inventory, dashboard_route_inventory
from src.surface_policy import DASHBOARD_SURFACE_POLICIES


class ContractInventoryTests(unittest.TestCase):
    fixtures = Path(__file__).parent / "fixtures" / "contracts"

    def test_dashboard_route_inventory_covers_every_dispatch_handler(self) -> None:
        routes = dashboard_route_inventory()

        self.assertTrue(routes)
        self.assertEqual(
            {route["handler"] for route in routes},
            set(DASHBOARD_SURFACE_POLICIES),
        )
        for route in routes:
            with self.subTest(handler=route["handler"], predicate=route["predicate"]):
                self.assertTrue(route["methods"])
                self.assertTrue(route["paths"])
                self.assertTrue(route["response"]["statuses"])

    def test_compatibility_inventory_covers_non_http_contracts(self) -> None:
        inventory = compatibility_inventory()

        self.assertEqual(inventory["format_version"], 1)
        self.assertIn("QBOT_DATABASE_PATH", inventory["configuration"]["variables"])
        self.assertIn("QBOT_DASHBOARD_SESSION_SECRET", inventory["configuration"]["sensitive_variables"])
        self.assertIn("run", inventory["cli_commands"])
        self.assertIn("observations", inventory["schema_objects"])
        self.assertIn("job:maintenance", inventory["non_http_surfaces"])
        self.assertEqual(
            inventory["signed_payloads"]["session"],
            ["community_id", "expires_at", "role", "session_version", "user_id", "username"],
        )
        self.assertEqual(
            inventory["cryptographic_contracts"]["twitch_eventsub"]["maximum_age_seconds"],
            600,
        )

    def test_generated_manifest_has_not_drifted(self) -> None:
        committed = json.loads((self.fixtures / "manifest.json").read_text())
        self.assertEqual(committed, compatibility_inventory())

    def test_fixtures_have_provenance_and_are_redacted(self) -> None:
        provenance = json.loads((self.fixtures / "provenance.json").read_text())
        fixture_names = {
            path.name for path in self.fixtures.glob("*.json")
            if path.name != "provenance.json"
        }
        self.assertEqual(set(provenance["fixtures"]), fixture_names)
        for name, metadata in provenance["fixtures"].items():
            self.assertFalse(metadata["contains_production_data"], name)

        forbidden_keys = re.compile(
            r'"(?:access_token|authorization|client_secret|cookie|password|refresh_token)"\s*:',
            re.IGNORECASE,
        )
        personal_email = re.compile(r"\b[\w.+-]+@(?![\w.-]+\.invalid\b)[\w.-]+\.[A-Za-z]{2,}\b")
        for fixture_name in fixture_names:
            fixture_text = (self.fixtures / fixture_name).read_text()
            self.assertIsNone(forbidden_keys.search(fixture_text), fixture_name)
            self.assertIsNone(personal_email.search(fixture_text), fixture_name)

        golden = json.loads((self.fixtures / "golden.json").read_text())
        categories = {scenario["category"] for scenario in golden["scenarios"]}
        self.assertEqual(categories, {
            "api_json", "backup_metadata", "commands", "eventsub", "exports", "html",
            "jobs", "moderation", "observations", "provider_normalization", "sessions",
            "tenant_isolation",
        })

    def test_high_risk_boundaries_have_authorized_and_denied_tenant_cases(self) -> None:
        golden = json.loads((self.fixtures / "golden.json").read_text())
        authorization = json.loads((self.fixtures / "authorization.json").read_text())
        required_boundaries = {scenario["category"] for scenario in golden["scenarios"]}
        covered_boundaries = {scenario["category"] for scenario in authorization["scenarios"]}
        self.assertEqual(covered_boundaries, required_boundaries)

        for scenario in authorization["scenarios"]:
            with self.subTest(boundary=scenario["category"]):
                outcomes = {result["authorized"] for result in scenario["expected"]}
                self.assertEqual(outcomes, {True, False})
                denied_cases = [
                    case for case, result in zip(scenario["input"], scenario["expected"])
                    if not result["authorized"]
                ]
                self.assertTrue(denied_cases)
                self.assertTrue(all(
                    case["actor_community_id"] != case["requested_community_id"]
                    for case in denied_cases
                ))


if __name__ == "__main__":
    unittest.main()