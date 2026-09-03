from __future__ import annotations

import json
import os
import stat
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.config import AppSettings
from src.dashboard.auth import DiscordIdentity
from src.database_transfer import export_sqlite_database, import_postgresql_database
from src.contexts import ActorAttribution, TenantContext
from src.db import (
    collect_observation,
    connect_database,
    database_health,
    enqueue_processing_job,
    initialize_database,
    mark_moderation_action_completed,
)
from src.intelligence.community import create_community, create_organization, create_workspace
from src.intelligence.search import search_observations
from src.intelligence.quotas import (
    TenantQuotaExceededError,
    configure_tenant_quota,
    consume_tenant_quota,
)
from src.health import create_health_server
from src.models import Observation
from src.pipeline.handlers import (
    claim_processing_job,
    complete_processing_job,
    retry_processing_job,
)
from src.postgres_migrations import MIGRATION_NAMES


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@unittest.skipUnless(os.environ.get("QBOT_TEST_POSTGRES_URL"), "PostgreSQL test URL not configured")
class PostgreSQLIntegrationTests(unittest.TestCase):
    database_url = os.environ.get("QBOT_TEST_POSTGRES_URL", "")

    def setUp(self) -> None:
        connection = connect_database(self.database_url)
        try:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            connection.commit()
        finally:
            connection.close()

    def test_concurrent_empty_start_and_upgrade_are_ordered_and_idempotent(self) -> None:
        def initialize() -> None:
            connection = connect_database(self.database_url)
            try:
                initialize_database(connection)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: initialize(), range(2)))

        connection = connect_database(self.database_url)
        try:
            initial = [tuple(row) for row in connection.execute(
                "SELECT version,name,applied_at FROM schema_migrations ORDER BY version"
            )]
            self.assertEqual([row[0] for row in initial], list(range(1, 28)))
            self.assertEqual([row[1] for row in initial], list(MIGRATION_NAMES))

            connection.execute("DELETE FROM schema_migrations WHERE version=27")
            connection.commit()
            initialize_database(connection)
            repeated = [tuple(row) for row in connection.execute(
                "SELECT version,name,applied_at FROM schema_migrations ORDER BY version"
            )]
        finally:
            connection.close()

        self.assertEqual(len(repeated), 27)
        self.assertEqual(repeated[:26], initial[:26])

    def test_migrations_reject_incompatible_schema_and_ownership(self) -> None:
        connection = connect_database(self.database_url)
        try:
            connection.execute("CREATE TABLE unmanaged_fixture(id BIGINT PRIMARY KEY)")
            connection.commit()
            with self.assertRaisesRegex(RuntimeError, "unmanaged tables"):
                initialize_database(connection)
            connection.execute("DROP TABLE unmanaged_fixture")
            connection.commit()

            initialize_database(connection)
            connection.execute(
                "UPDATE schema_migrations SET name=? WHERE version=1", ("conflicting",),
            )
            connection.commit()
            with self.assertRaisesRegex(RuntimeError, "history is incompatible"):
                initialize_database(connection)
            connection.execute(
                "UPDATE schema_migrations SET name=? WHERE version=1", (MIGRATION_NAMES[0],),
            )
            connection.commit()

            connection.execute("ALTER SCHEMA public OWNER TO pg_database_owner")
            connection.commit()
            with self.assertRaisesRegex(RuntimeError, "must own"):
                initialize_database(connection)
        finally:
            connection.rollback()
            connection.execute("ALTER SCHEMA public OWNER TO CURRENT_USER")
            connection.commit()
            connection.close()

    def test_domain_crud_generated_ids_and_health(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            organization_id = create_organization(
                connection, name="Fixture Organization", slug="fixture-organization",
            )
            workspace_id = create_workspace(
                connection, organization_id=organization_id,
                name="Fixture Workspace", slug="fixture-workspace",
            )
            community_id = create_community(
                connection, workspace_id=workspace_id,
                name="Fixture Community", slug="fixture-community",
            )
            row = connection.execute(
                "SELECT id,slug FROM communities WHERE id=?", (community_id,),
            ).fetchone()
            timestamp = datetime.now(UTC).isoformat()
            timestamp_row = connection.execute(
                """SELECT MAX(0,(julianday(?)-julianday(created_at))*86400),
                          strftime('%Y-%m-%dT%H:%M:00+00:00',created_at)
                   FROM communities
                   WHERE id=? AND datetime(created_at)>=datetime(?,'-24 hours')""",
                (timestamp, community_id, timestamp),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual((organization_id, workspace_id, community_id), (2, 2, 2))
        self.assertEqual(row[0], community_id)
        self.assertEqual(row["slug"], "fixture-community")
        self.assertGreaterEqual(timestamp_row[0], 0)
        self.assertRegex(timestamp_row[1], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+00:00$")
        health = database_health(self.database_url)
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["backend"], "postgresql")
        self.assertEqual(health["schema_version"], 27)

    def test_health_and_authenticated_dashboard_http(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
        finally:
            connection.close()
        settings = AppSettings.from_env({
            "QBOT_DATABASE_URL": self.database_url,
            "QBOT_ENABLED_SERVICES": "web,jobs",
            "QBOT_DASHBOARD_SESSION_SECRET": "postgres-http-session-secret",
            "QBOT_DISCORD_OAUTH_CLIENT_ID": "client-id",
            "QBOT_DISCORD_OAUTH_CLIENT_SECRET": "client-secret",
            "QBOT_DISCORD_OAUTH_REDIRECT_URI": "https://example.test/oauth/discord/callback",
            "QBOT_OPERATOR_GUILD_IDS": "guild-1",
            "QBOT_LEGAL_ORGANIZATION_NAME": "QBot4K Test Operations",
            "QBOT_LEGAL_CONTACT_EMAIL": "privacy@example.test",
            "QBOT_LEGAL_JURISDICTION": "Test Jurisdiction",
            "QBOT_LEGAL_EFFECTIVE_DATE": "2026-09-02",
        })
        settings = replace(settings, dashboard_port=0)
        server = create_health_server(settings, {"web": "ready", "jobs": "ready"})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(f"{base_url}/health/ready") as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["status"], "ready")
            self.assertEqual(health["database"]["backend"], "postgresql")

            with mock.patch(
                "src.dashboard.server.exchange_discord_code_for_token",
                return_value="discord-access-token",
            ), mock.patch(
                "src.dashboard.server.fetch_discord_identity",
                return_value=DiscordIdentity(
                    user_id="postgres-operator", username="sam",
                    guild_ids=("guild-1",), permissions={"guild-1": "8"},
                ),
            ):
                callback = Request(
                    f"{base_url}/oauth/discord/callback?code=abc&state=state-1",
                    headers={"Cookie": "qbot4k_oauth_state=state-1"},
                )
                with self.assertRaises(HTTPError) as callback_error:
                    opener.open(callback)
                cookies = callback_error.exception.headers.get_all("Set-Cookie") or []
                callback_error.exception.close()
            session_cookie = next(
                cookie.split(";", 1)[0] for cookie in cookies
                if cookie.startswith("qbot4k_session=")
            )
            with opener.open(Request(
                f"{base_url}/api/overview", headers={"Cookie": session_cookie},
            )) as response:
                overview = json.loads(response.read().decode("utf-8"))
            self.assertIn("overview", overview)
            self.assertEqual(overview["services"]["web"], "ready")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    def test_full_text_phrase_search_and_ranking_use_gin_vector(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            for external_id, text, occurred_at in (
                ("fts-one", "Investigate cobalt lantern now", "2026-08-11T11:00:00+00:00"),
                (
                    "fts-two",
                    "Cobalt lantern evidence confirms cobalt lantern",
                    "2026-08-11T10:00:00+00:00",
                ),
            ):
                collect_observation(connection, Observation(
                    platform="discord",
                    event_type="message.created",
                    external_event_id=external_id,
                    community_id=1,
                    actor_platform_user_id=external_id,
                    text=text,
                    occurred_at=occurred_at,
                ))
            hits = search_observations(
                connection, community_id=1, query='"cobalt lantern"',
            )
            index_row = connection.execute(
                """SELECT indexdef FROM pg_indexes
                   WHERE schemaname='public' AND indexname='idx_observations_search_vector'"""
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual([hit["external_event_id"] for hit in hits], ["fts-two", "fts-one"])
        self.assertLess(hits[0]["rank"], hits[1]["rank"])
        self.assertIn("USING gin", index_row[0])

    def test_concurrent_claim_has_one_winner_and_recovers_expired_lease(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            job_id = enqueue_processing_job(
                connection,
                stage="analysis",
                job_type="fixture",
                idempotency_key="concurrent-fixture",
                community_id=1,
            )
            duplicate = enqueue_processing_job(
                connection,
                stage="analysis",
                job_type="fixture",
                idempotency_key="concurrent-fixture",
                community_id=1,
            )
        finally:
            connection.close()

        def claim(worker_id: str) -> int | None:
            worker_connection = connect_database(self.database_url)
            try:
                row = claim_processing_job(
                    worker_connection, stage="analysis", worker_id=worker_id,
                )
                return int(row["id"]) if row is not None else None
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, ("worker-a", "worker-b")))

        self.assertIsNotNone(job_id)
        self.assertIsNone(duplicate)
        self.assertEqual([claim for claim in claims if claim is not None], [job_id])

        connection = connect_database(self.database_url)
        try:
            connection.execute(
                """UPDATE processing_jobs
                   SET lease_expires_at='2000-01-01T00:00:00+00:00'
                   WHERE id=?""",
                (job_id,),
            )
            connection.commit()
            reclaimed = claim_processing_job(
                connection, stage="analysis", worker_id="recovery-worker",
            )
            self.assertEqual(reclaimed["id"], job_id)
            self.assertEqual(reclaimed["attempts"], 2)
            complete_processing_job(connection, job_id)
        finally:
            connection.close()

    def test_claims_preserve_tenant_fairness_priority_and_retries(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            peer_id = create_community(
                connection,
                workspace_id=1,
                name="Peer Community",
                slug="peer-community",
            )
            backlog_id = enqueue_processing_job(
                connection, stage="analysis", job_type="fixture",
                idempotency_key="fair-backlog", community_id=1, priority=100,
            )
            priority_id = enqueue_processing_job(
                connection, stage="analysis", job_type="fixture",
                idempotency_key="fair-priority", community_id=1, priority=10,
            )
            peer_job_id = enqueue_processing_job(
                connection, stage="analysis", job_type="fixture",
                idempotency_key="fair-peer", community_id=peer_id, priority=100,
            )

            first = claim_processing_job(connection, stage="analysis", worker_id="fair-1")
            self.assertEqual(first["id"], priority_id)
            retry_processing_job(connection, priority_id, "fixture retry", retry_delay_seconds=0)

            second = claim_processing_job(connection, stage="analysis", worker_id="fair-2")
            self.assertEqual(second["id"], peer_job_id)
            complete_processing_job(connection, peer_job_id)

            third = claim_processing_job(connection, stage="analysis", worker_id="fair-3")
            self.assertEqual(third["id"], priority_id)
            self.assertEqual(third["attempts"], 2)
            complete_processing_job(connection, priority_id)

            fourth = claim_processing_job(connection, stage="analysis", worker_id="fair-4")
            self.assertEqual(fourth["id"], backlog_id)
            complete_processing_job(connection, backlog_id)
        finally:
            connection.close()

    def test_sqlite_transfer_preserves_checksums_search_and_sequences(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.sqlite3"
            source = connect_database(source_path)
            try:
                initialize_database(source)
                peer_id = create_community(
                    source,
                    workspace_id=1,
                    name="Imported Peer",
                    slug="imported-peer",
                )
                for community_id, external_id in ((1, "transfer-default"), (peer_id, "transfer-peer")):
                    collect_observation(source, Observation(
                        platform="discord",
                        event_type="message.created",
                        external_event_id=external_id,
                        community_id=community_id,
                        actor_platform_user_id=external_id,
                        text="transfer cobalt lantern evidence",
                        occurred_at="2026-08-11T10:00:00+00:00",
                    ))
                source_hits = search_observations(
                    source, community_id=peer_id, query='"cobalt lantern"',
                )
            finally:
                source.close()

            manifest_path = export_sqlite_database(
                source_path,
                root / "transfer",
                mark_source_read_only=True,
            )
            result = import_postgresql_database(
                manifest_path,
                self.database_url,
                replace_target=True,
            )
            self.assertEqual(source_path.stat().st_mode & stat.S_IWUSR, 0)
            communities_path = manifest_path.parent / "communities.jsonl"
            communities_path.write_text(
                communities_path.read_text(encoding="utf-8").replace(
                    "Imported Peer", "Tampered Peer",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "checksum"):
                import_postgresql_database(
                    manifest_path,
                    self.database_url,
                    replace_target=True,
                )

        target = connect_database(self.database_url)
        try:
            target_hits = search_observations(
                target, community_id=peer_id, query='"cobalt lantern"',
            )
            next_community_id = create_community(
                target,
                workspace_id=1,
                name="Post-import Community",
                slug="post-import-community",
            )
            imported_name = target.execute(
                "SELECT name FROM communities WHERE id=?", (peer_id,),
            ).fetchone()[0]
        finally:
            target.close()

        self.assertEqual(result["schema_version"], 27)
        self.assertTrue(result["constraints_validated"])
        self.assertGreater(result["rows"], 0)
        self.assertEqual(
            [hit["external_event_id"] for hit in target_hits],
            [hit["external_event_id"] for hit in source_hits],
        )
        self.assertEqual(next_community_id, peer_id + 1)
        self.assertEqual(imported_name, "Imported Peer")

    def test_concurrent_quota_consumption_is_atomic(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            configure_tenant_quota(
                connection,
                tenant=TenantContext(1),
                actor=ActorAttribution("operator", 1),
                quota_type="jobs",
                limit_count=3,
                window_seconds=60,
            )
            connection.commit()
        finally:
            connection.close()

        def consume(_: int) -> bool:
            worker_connection = connect_database(self.database_url)
            try:
                consume_tenant_quota(
                    worker_connection,
                    tenant=TenantContext(1),
                    quota_type="jobs",
                )
                worker_connection.commit()
                return True
            except TenantQuotaExceededError:
                worker_connection.commit()
                return False
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=5) as executor:
            outcomes = list(executor.map(consume, range(5)))

        self.assertEqual(outcomes.count(True), 3)
        self.assertEqual(outcomes.count(False), 2)

    def test_concurrent_moderation_completion_is_tenant_scoped(self) -> None:
        connection = connect_database(self.database_url)
        try:
            initialize_database(connection)
            account_id = int(connection.execute(
                """INSERT INTO platform_accounts(platform,platform_user_id,username)
                   VALUES ('twitch','moderation-target','target')"""
            ).lastrowid)
            action_id = int(connection.execute(
                """INSERT INTO moderation_actions(
                       community_id,platform,target_platform_account_id,action_type,
                       actor_type,reason,status)
                   VALUES (1,'twitch',?,'timeout','operator','fixture','pending')""",
                (account_id,),
            ).lastrowid)
            connection.commit()
        finally:
            connection.close()

        def complete(worker: int) -> None:
            worker_connection = connect_database(self.database_url)
            try:
                mark_moderation_action_completed(
                    worker_connection,
                    action_id,
                    tenant=TenantContext(1),
                    provider_status="204",
                    provider_response={"worker": worker},
                )
            finally:
                worker_connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(complete, range(2)))

        connection = connect_database(self.database_url)
        try:
            row = connection.execute(
                """SELECT status,provider_status,provider_confirmed_at
                   FROM moderation_actions WHERE id=?""",
                (action_id,),
            ).fetchone()
            with self.assertRaisesRegex(LookupError, "tenant moderation action not found"):
                mark_moderation_action_completed(
                    connection,
                    action_id,
                    tenant=TenantContext(2),
                    provider_status="204",
                )
        finally:
            connection.close()

        self.assertEqual(row[0], "completed")
        self.assertEqual(row[1], "204")
        self.assertIsNotNone(row[2])


if __name__ == "__main__":
    unittest.main()