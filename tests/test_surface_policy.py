from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src import jobs
from src import discord, twitch
from src.intelligence import announcements, community
from src.twitch_control import TwitchControlPlane
from src.dashboard.server import DashboardApp
from src.intelligence.community import DASHBOARD_CAPABILITIES
from src.surface_policy import DASHBOARD_SURFACE_POLICIES, NON_HTTP_SURFACE_POLICIES


class SurfacePolicyTests(unittest.TestCase):
    def test_every_dashboard_dispatch_target_has_an_executable_policy(self) -> None:
        module = ast.parse(Path(inspect.getsourcefile(DashboardApp) or "").read_text())
        dashboard = next(
            node for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "DashboardApp"
        )
        methods = {
            node.name: node for node in dashboard.body if isinstance(node, ast.FunctionDef)
        }
        dispatch = methods["dispatch"]
        routed = {
            call.func.attr
            for call in ast.walk(dispatch)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr.startswith("_serve_")
        }
        self.assertEqual(set(DASHBOARD_SURFACE_POLICIES), routed)

        allowed_capabilities = DASHBOARD_CAPABILITIES | {
            "events.ingest", "events.write", "public.access",
        }
        for handler_name, policy in DASHBOARD_SURFACE_POLICIES.items():
            with self.subTest(handler=handler_name):
                self.assertIn(policy.capability, allowed_capabilities)
                self.assertIn(policy.scope, {"global", "community", "installation"})
                self.assertTrue(
                    self._method_reaches_guard(methods, handler_name, policy.guard),
                    f"{handler_name} does not reach declared guard {policy.guard}",
                )

        shared_guard_source = inspect.getsource(DashboardApp._require_session)
        self.assertIn("_calling_surface_capability", shared_guard_source)
        self.assertIn("operator_has_permission", shared_guard_source)

    def test_non_http_inventory_covers_commands_jobs_lookups_and_provider_actions(self) -> None:
        expected = {
            "command:addcom", "command:editcom", "command:delcom", "command:alias",
            "command:verify", "command:credit", "command:custom",
            "job:maintenance", "job:twitch_live_announcements",
            "job:scheduled_announcements", "job:onboarding_roles",
            "job:onboarding_checkpoints", "lookup:installation", "lookup:member",
            "lookup:moderation", "provider:moderation", "provider:announcement",
            "provider:live_control",
        }
        self.assertEqual(set(NON_HTTP_SURFACE_POLICIES), expected)
        self.assertEqual(
            {policy.kind for policy in NON_HTTP_SURFACE_POLICIES.values()},
            {"bot_command", "job", "direct_lookup", "provider_action"},
        )
        self.assertTrue(all(policy.scope in {"community", "installation"}
                            for policy in NON_HTTP_SURFACE_POLICIES.values()))

    def test_every_system_job_executes_its_declared_surface_policy(self) -> None:
        entry_points = {
            "job:maintenance": jobs.run_maintenance_jobs,
            "job:twitch_live_announcements": jobs.run_twitch_live_announcement_job,
            "job:scheduled_announcements": jobs.run_scheduled_announcement_job,
            "job:onboarding_roles": jobs.run_onboarding_role_job,
            "job:onboarding_checkpoints": jobs.run_onboarding_checkpoint_job,
        }
        for surface, entry_point in entry_points.items():
            with self.subTest(surface=surface):
                source = inspect.getsource(entry_point)
                self.assertIn("require_non_http_surface", source)
                self.assertIn(f'"{surface}"', source)

    def test_connector_commands_execute_the_shared_surface_guard(self) -> None:
        for dispatcher in (
            discord.DiscordConnector._dispatch_registered_command,
            twitch.TwitchConnector._dispatch_registered_command,
        ):
            with self.subTest(dispatcher=dispatcher.__qualname__):
                self.assertIn("require_command_surface", inspect.getsource(dispatcher))

    def test_provider_actions_execute_the_shared_installation_guard(self) -> None:
        providers = {
            "provider:moderation": (
                discord.DiscordConnector._execute_pending_moderation_actions,
                twitch.TwitchConnector.execute_pending_moderation_actions,
            ),
            "provider:announcement": (announcements.dispatch_due_announcements,),
            "provider:live_control": (TwitchControlPlane._execute,),
        }
        for surface, entry_points in providers.items():
            with self.subTest(surface=surface):
                self.assertIn(surface, community.INSTALLATION_CAPABILITY_BY_SURFACE)
                for entry_point in entry_points:
                    source = inspect.getsource(entry_point)
                    self.assertIn("require_installation_surface", source)
                    self.assertIn(f'"{surface}"', source)

    def _method_reaches_guard(
        self,
        methods: dict[str, ast.FunctionDef],
        method_name: str,
        guard: str,
        visited: set[str] | None = None,
    ) -> bool:
        if guard == "public":
            return True
        expected_call = {
            "session": "_require_session",
            "optional_session": "_read_session",
            "api_client_or_session": "_require_ingest_auth",
            "webhook_signature": "verify_eventsub_signature",
        }[guard]
        visited = set(visited or ())
        if method_name in visited:
            return False
        visited.add(method_name)
        method = methods[method_name]
        calls = {
            call.func.attr
            for call in ast.walk(method)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        } | {
            call.func.id
            for call in ast.walk(method)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if expected_call in calls:
            return True
        return any(
            called in methods
            and self._method_reaches_guard(methods, called, guard, visited)
            for called in calls
        )


if __name__ == "__main__":
    unittest.main()
