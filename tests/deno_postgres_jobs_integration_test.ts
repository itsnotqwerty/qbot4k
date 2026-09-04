import { assert, assertEquals } from "jsr:@std/assert@1.0.14";
import postgres from "postgres";
import { PostgresDatabase } from "../src/data/database.ts";
import { withOperationalService } from "../src/data/operations.ts";

const databaseUrl = Deno.env.get("QBOT_TEST_POSTGRES_URL");

Deno.test({
  name:
    "PostgreSQL jobs claim once, preserve tenant fairness, and recover leases",
  ignore: !databaseUrl,
  async fn() {
    const fixtureId = crypto.randomUUID();
    const ownershipFixtureType = `fixture.ownership.${fixtureId}`;
    const sql = postgres(databaseUrl!, { max: 2 });
    const database = new PostgresDatabase(databaseUrl!, { maxConnections: 4 });
    let organizationId: number | null = null;
    try {
      const [organization] = await sql`
        INSERT INTO organizations(name,slug)
        VALUES (${`Deno jobs ${fixtureId}`},${`deno-jobs-${fixtureId}`})
        RETURNING id
      `;
      organizationId = Number(organization.id);
      const [workspace] = await sql`
        INSERT INTO workspaces(organization_id,name,slug)
        VALUES (${organizationId},${"Deno jobs"},${`deno-jobs-${fixtureId}`})
        RETURNING id
      `;
      const [communityA] = await sql`
        INSERT INTO communities(workspace_id,name,slug)
        VALUES (${
        Number(workspace.id)
      },${"Deno jobs A"},${`deno-jobs-a-${fixtureId}`})
        RETURNING id
      `;
      const [communityB] = await sql`
        INSERT INTO communities(workspace_id,name,slug)
        VALUES (${
        Number(workspace.id)
      },${"Deno jobs B"},${`deno-jobs-b-${fixtureId}`})
        RETURNING id
      `;
      const communityAId = Number(communityA.id);
      const communityBId = Number(communityB.id);
      const communityIds = [communityAId, communityBId];
      await sql`
        INSERT INTO processing_job_ownership(job_type,owner_runtime)
        VALUES ('fixture','deno')
        ON CONFLICT(job_type) DO UPDATE SET owner_runtime='deno'
      `;
      for (const suffix of ["c", "d", "e"]) {
        const [community] = await sql`
          INSERT INTO communities(workspace_id,name,slug)
          VALUES (
            ${Number(workspace.id)},
            ${`Deno jobs ${suffix.toUpperCase()}`},
            ${`deno-jobs-${suffix}-${fixtureId}`}
          )
          RETURNING id
        `;
        communityIds.push(Number(community.id));
      }
      const store = database.processingJobStore();

      await sql`
        INSERT INTO processing_job_ownership(job_type,owner_runtime)
        VALUES (${ownershipFixtureType},'python')
      `;
      const ownershipJobId = await store.enqueue({
        stage: "analysis",
        jobType: ownershipFixtureType,
        idempotencyKey: `deno-ownership-${fixtureId}`,
        communityId: communityAId,
      });
      assertEquals(
        await store.claim("analysis", `deno-blocked-${fixtureId}`),
        null,
      );
      await withOperationalService(
        databaseUrl!,
        (service) =>
          service.transferJobOwnership(
            ownershipFixtureType,
            "deno",
            "python",
          ),
      );
      const ownershipJob = await store.claim(
        "analysis",
        `deno-owner-${fixtureId}`,
      );
      assertEquals(ownershipJob?.id, ownershipJobId);
      await store.complete(ownershipJob!.id, `deno-owner-${fixtureId}`);
      const [ownershipAudit] = await sql`
        SELECT payload_json::jsonb AS payload
          FROM audit_log
         WHERE action_type='processing_job.ownership_transferred'
           AND payload_json::jsonb->>'job_type'=${ownershipFixtureType}
         ORDER BY id DESC LIMIT 1
      `;
      assertEquals(ownershipAudit.payload.owner_runtime, "deno");
      assertEquals(ownershipAudit.payload.previous_owner, "python");

      const abortedIds = await Promise.all(
        ["a", "b"].map((suffix) =>
          store.enqueue({
            stage: "analysis",
            jobType: ownershipFixtureType,
            idempotencyKey: `deno-aborted-${fixtureId}-${suffix}`,
            communityId: communityAId,
          })
        ),
      );
      const abortedJobIds = abortedIds.map((id) => {
        assert(id !== null);
        return id;
      });
      await withOperationalService(
        databaseUrl!,
        (service) =>
          service.transferJobOwnership(
            ownershipFixtureType,
            "python",
            "deno",
          ),
      );
      assertEquals(
        await store.claim("analysis", `deno-aborted-${fixtureId}`),
        null,
      );
      const preserved = await sql`
        SELECT id,status,attempts FROM processing_jobs
         WHERE id IN (${abortedJobIds[0]},${abortedJobIds[1]}) ORDER BY id
      `;
      assertEquals(
        preserved.map((job) => ({
          id: Number(job.id),
          status: job.status,
          attempts: Number(job.attempts),
        })),
        abortedJobIds.map((id) => ({ id, status: "pending", attempts: 0 })),
      );

      const raceJobId = await store.enqueue({
        stage: "analysis",
        jobType: "fixture",
        idempotencyKey: `deno-race-${fixtureId}`,
        communityId: communityAId,
      });
      const claims = await Promise.all([
        store.claim("analysis", `deno-a-${fixtureId}`),
        store.claim("analysis", `deno-b-${fixtureId}`),
      ]);
      const winner = claims.find((job) => job !== null);
      assertEquals(claims.filter((job) => job !== null).length, 1);
      assertEquals(winner?.id, raceJobId);
      await store.complete(
        winner!.id,
        claims[0] ? `deno-a-${fixtureId}` : `deno-b-${fixtureId}`,
      );

      const backlogId = await store.enqueue({
        stage: "analysis",
        jobType: "fixture",
        idempotencyKey: `deno-backlog-${fixtureId}`,
        communityId: communityAId,
      });
      const peerId = await store.enqueue({
        stage: "analysis",
        jobType: "fixture",
        idempotencyKey: `deno-peer-${fixtureId}`,
        communityId: communityBId,
      });
      const peer = await store.claim("analysis", `deno-peer-${fixtureId}`);
      assertEquals(peer?.id, peerId);
      await store.complete(peer!.id, `deno-peer-${fixtureId}`);
      const backlog = await store.claim(
        "analysis",
        `deno-backlog-${fixtureId}`,
      );
      assertEquals(backlog?.id, backlogId);
      await store.complete(backlog!.id, `deno-backlog-${fixtureId}`);

      for (const communityId of communityIds) {
        for (let index = 0; index < 2; index += 1) {
          await store.enqueue({
            stage: "analysis",
            jobType: "fixture",
            idempotencyKey: `deno-fair-${fixtureId}-${communityId}-${index}`,
            communityId,
          });
        }
      }
      const fairnessWorkers = Array.from(
        { length: 10 },
        (_, index) => `deno-fair-worker-${fixtureId}-${index}`,
      );
      const fairnessClaims = await Promise.all(
        fairnessWorkers.map((workerId) => store.claim("analysis", workerId)),
      );
      assertEquals(new Set(fairnessClaims.map((job) => job?.id)).size, 10);
      assertEquals(
        communityIds.map((communityId) =>
          fairnessClaims.filter((job) => job?.communityId === communityId)
            .length
        ),
        [2, 2, 2, 2, 2],
      );
      await Promise.all(
        fairnessClaims.map((job, index) =>
          store.complete(job!.id, fairnessWorkers[index])
        ),
      );

      const abandonedId = await store.enqueue({
        stage: "analysis",
        jobType: "fixture",
        idempotencyKey: `deno-abandoned-${fixtureId}`,
        communityId: communityAId,
        maxAttempts: 3,
      });
      const abandoned = await store.claim(
        "analysis",
        `deno-terminated-${fixtureId}`,
      );
      assertEquals(abandoned?.id, abandonedId);
      await sql`
        UPDATE processing_jobs
           SET lease_expires_at='2000-01-01T00:00:00+00:00'
         WHERE id=${abandonedId}
      `;
      assertEquals(await store.recoverExpired(), 1);
      const reclaimed = await store.claim(
        "analysis",
        `deno-recovery-${fixtureId}`,
      );
      assertEquals(reclaimed?.id, abandonedId);
      assertEquals(reclaimed?.attempts, 2);
      await store.complete(reclaimed!.id, `deno-recovery-${fixtureId}`);

      const [expiredObservation] = await sql`
        INSERT INTO observations(
          platform,community_id,event_type,external_event_id,occurred_at
        ) VALUES (
          'fixture',${communityAId},'message.created',
          ${`deno-expired-observation-${fixtureId}`},
          '2026-08-11T11:59:00+00:00'
        ) RETURNING id
      `;
      const expiredObservationId = Number(expiredObservation.id);
      const expiredId = await store.enqueue({
        stage: "analysis",
        jobType: "fixture",
        idempotencyKey: `deno-expired-${fixtureId}`,
        observationId: expiredObservationId,
        maxAttempts: 1,
      });
      const expired = await store.claim(
        "analysis",
        `deno-expired-${fixtureId}`,
      );
      assertEquals(expired?.id, expiredId);
      await sql`
        UPDATE processing_jobs
           SET lease_expires_at='2000-01-01T00:00:00+00:00'
         WHERE id=${expiredId}
      `;
      assertEquals(await store.recoverExpired(), 1);
      const [recovered] = await sql`
        SELECT processing_jobs.status,COUNT(dead_letter_events.id)::int AS dead_letters,
               MIN(dead_letter_events.id)::int AS dead_letter_id
          FROM processing_jobs
          LEFT JOIN dead_letter_events
            ON dead_letter_events.processing_job_id=processing_jobs.id
         WHERE processing_jobs.id=${expiredId}
         GROUP BY processing_jobs.status
      `;
      assertEquals(recovered.status, "failed");
      assertEquals(Number(recovered.dead_letters), 1);
      const replayId = await store.replayDeadLetter(
        Number(recovered.dead_letter_id),
        new Date("2026-08-11T12:00:00Z"),
      );
      const [replay] = await sql`
        SELECT d.status,j.status AS job_status,j.community_id,j.observation_id
          FROM dead_letter_events d
          JOIN processing_jobs j ON j.id=${replayId}
         WHERE d.id=${Number(recovered.dead_letter_id)}
      `;
      assertEquals(replay.status, "replayed");
      assertEquals(replay.job_status, "pending");
      assertEquals(Number(replay.community_id), communityAId);
      assertEquals(Number(replay.observation_id), expiredObservationId);
    } finally {
      await sql`
        DELETE FROM processing_job_ownership
         WHERE job_type=${ownershipFixtureType}
      `;
      if (organizationId !== null) {
        await sql`
          DELETE FROM dead_letter_events
           WHERE community_id IN (
             SELECT communities.id
               FROM communities
               JOIN workspaces ON workspaces.id=communities.workspace_id
              WHERE workspaces.organization_id=${organizationId}
           )
        `;
        await sql`
          DELETE FROM processing_jobs
           WHERE community_id IN (
             SELECT communities.id
               FROM communities
               JOIN workspaces ON workspaces.id=communities.workspace_id
              WHERE workspaces.organization_id=${organizationId}
           )
        `;
        await sql`
          DELETE FROM observations
           WHERE community_id IN (
             SELECT communities.id
               FROM communities
               JOIN workspaces ON workspaces.id=communities.workspace_id
              WHERE workspaces.organization_id=${organizationId}
           )
        `;
        await sql`DELETE FROM organizations WHERE id=${organizationId}`;
      }
      await database.close();
      await sql.end();
    }
  },
});
