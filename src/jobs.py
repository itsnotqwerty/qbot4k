from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import AppSettings
from .db import connect_database, initialize_database


JobConnectionFactory = Callable[[Path], sqlite3.Connection]


@dataclass(frozen=True)
class MaintenanceReport:
	deleted_messages: int
	deleted_audit_log_rows: int
	backup_path: str
	backup_metadata_path: str
	backup_sha256: str
	rollup_rows: int


def run_maintenance_jobs(
	settings: AppSettings,
	*,
	connection_factory: JobConnectionFactory = connect_database,
	now: datetime | None = None,
) -> MaintenanceReport:
	current_time = now or datetime.now(UTC)
	connection = connection_factory(settings.database_path)
	try:
		initialize_database(connection)
		deleted_messages = purge_expired_messages(connection, current_time, settings.message_retention_days)
		deleted_audit_rows = purge_expired_audit_log(connection, current_time, settings.audit_retention_days)
		rollup_rows = refresh_metrics_rollups(connection, current_time)
	finally:
		connection.close()

	backup_path, backup_metadata_path, backup_sha256 = create_database_backup(
		settings.database_path,
		settings.backup_dir,
		current_time,
	)
	return MaintenanceReport(
		deleted_messages=deleted_messages,
		deleted_audit_log_rows=deleted_audit_rows,
		backup_path=str(backup_path),
		backup_metadata_path=str(backup_metadata_path),
		backup_sha256=backup_sha256,
		rollup_rows=rollup_rows,
	)


def purge_expired_messages(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"DELETE FROM messages WHERE created_at < ?",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def purge_expired_audit_log(
	connection: sqlite3.Connection,
	now: datetime,
	retention_days: int,
) -> int:
	cutoff = _cutoff_timestamp(now, retention_days)
	with connection:
		cursor = connection.execute(
			"DELETE FROM audit_log WHERE created_at < ?",
			(cutoff,),
		)
	return int(cursor.rowcount or 0)


def refresh_metrics_rollups(connection: sqlite3.Connection, now: datetime) -> int:
	bucket_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
	rollups = {
		"messages_total": connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
		"open_reviews": connection.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0],
		"pending_actions": connection.execute("SELECT COUNT(*) FROM moderation_actions WHERE status = 'pending'").fetchone()[0],
	}
	with connection:
		for metric_name, value in rollups.items():
			connection.execute(
				"""
				INSERT INTO metrics_rollups (
					metric_name,
					bucket_start,
					bucket_size,
					dimension_json,
					value
				) VALUES (?, ?, '1d', '{}', ?)
				ON CONFLICT(metric_name, bucket_start, bucket_size, dimension_json)
				DO UPDATE SET value = excluded.value, created_at = CURRENT_TIMESTAMP
				""",
				(metric_name, bucket_start, float(value)),
			)
	return len(rollups)


def create_database_backup(database_path: Path, backup_dir: Path, now: datetime) -> tuple[Path, Path, str]:
	backup_dir.mkdir(parents=True, exist_ok=True)
	timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
	backup_path = backup_dir / f"{database_path.stem}-{timestamp}{database_path.suffix}"
	shutil.copy2(database_path, backup_path)
	backup_sha256 = _sha256sum(backup_path)
	metadata = {
		"created_at": now.astimezone(UTC).isoformat(),
		"database_path": str(database_path),
		"backup_path": str(backup_path),
		"sha256": backup_sha256,
		"size_bytes": backup_path.stat().st_size,
	}
	backup_metadata_path = backup_path.with_suffix(backup_path.suffix + ".json")
	backup_metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
	return backup_path, backup_metadata_path, backup_sha256


def _cutoff_timestamp(now: datetime, retention_days: int) -> str:
	return (now.astimezone(UTC) - timedelta(days=retention_days)).isoformat()


def _sha256sum(path: Path) -> str:
	hasher = hashlib.sha256()
	with path.open("rb") as file_handle:
		for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
			hasher.update(chunk)
	return hasher.hexdigest()
