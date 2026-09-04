# SQLite to PostgreSQL Transfer

This offline workflow is used during the paused-ingress migration window. The
PostgreSQL target is replaced transactionally, so use a dedicated empty or
approved migration target.

## Export

Checkpoint SQLite, export deterministic JSONL files, verify source foreign keys
and tenant ownership, and make the final source file read-only:

```bash
deno task database-transfer export \
  var/qbot4k.sqlite3 var/transfers/final \
  --mark-source-read-only
```

The generated `manifest.json` records schema version, columns, dependencies,
primary keys, row counts, SHA-256 checksums, ownership checks, orphan results,
and source totals. Preserve the manifest and all adjacent JSONL files together.

## Import

Set the PostgreSQL URL through the deployment secret mechanism, then run:

```bash
deno task database-transfer import \
  var/transfers/final/manifest.json "$QBOT_DATABASE_URL" \
  --replace-target
```

`--replace-target` is mandatory. The importer initializes the schema, truncates
the listed target tables in one transaction, imports in foreign-key order,
resets identity sequences, checks PostgreSQL constraint validation, and compares
every target table count and checksum with the source manifest. Any mismatch
rolls back the replacement.

The Deno tool preserves transfer manifest format version 1, including the
canonical JSONL and byte encoding used by the transition exporter. Run
`deno task test:database-transfer` for the non-destructive gate. The destructive
integration rehearsal requires a disposable database URL in
`QBOT_TRANSFER_TEST_POSTGRES_URL`.

After a successful rehearsal, verify application search results and retain the
read-only SQLite source through the rollback window. Production cutover still
requires the pause, monitoring, and rollback steps in the transition plan.
