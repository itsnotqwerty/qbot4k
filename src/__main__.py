from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
	from src.config import AppSettings, ConfigError
	from src.db import connect_database, initialize_database, list_tables, upsert_service_reliability_bucket
	from src.discord import DiscordConnector
	from src.health import build_health_snapshot, create_health_server
	from src.jobs import run_maintenance_jobs
	from src.jobs import run_twitch_live_announcement_job
	from src.logging_utils import configure_logging
	from src.token_store import persist_refreshed_twitch_tokens
	from src.twitch import TwitchConnector
	from src.twitch_auth import TwitchTokenManager
else:
	from .config import AppSettings, ConfigError
	from .db import connect_database, initialize_database, list_tables, upsert_service_reliability_bucket
	from .discord import DiscordConnector
	from .health import build_health_snapshot, create_health_server
	from .jobs import run_maintenance_jobs
	from .jobs import run_twitch_live_announcement_job
	from .logging_utils import configure_logging
	from .token_store import persist_refreshed_twitch_tokens
	from .twitch import TwitchConnector
	from .twitch_auth import TwitchTokenManager

def _persist_refreshed_twitch_tokens(access_token: str, refresh_token: str | None) -> None:
	persist_refreshed_twitch_tokens(access_token, refresh_token)


def _install_shutdown_handlers(
	shutdown_event: threading.Event,
	logger: logging.Logger,
) -> tuple[dict[int, object], object] | None:
	if threading.current_thread() is not threading.main_thread():
		return None

	def _handle_shutdown(signum: int, _frame: object) -> None:
		if not shutdown_event.is_set():
			logger.info("received shutdown signal signal=%s", signum)
		shutdown_event.set()

	original_handlers = {
		signal.SIGINT: signal.getsignal(signal.SIGINT),
		signal.SIGTERM: signal.getsignal(signal.SIGTERM),
	}
	for sig in original_handlers:
		signal.signal(sig, _handle_shutdown)
	return original_handlers, _handle_shutdown


def _restore_shutdown_handlers(handler_state: tuple[dict[int, object], object] | None) -> None:
	if handler_state is None:
		return
	original_handlers, _handler = handler_state
	for sig, original_handler in original_handlers.items():
		signal.signal(sig, original_handler)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="QBot4K bootstrap utilities")
	subparsers = parser.add_subparsers(dest="command", required=True)

	subparsers.add_parser("check-config", help="Validate environment configuration")
	subparsers.add_parser("init-db", help="Initialize the SQLite schema")

	run_parser = subparsers.add_parser(
		"run",
		help="Initialize services and optionally start the health server",
	)
	run_parser.add_argument(
		"--once",
		action="store_true",
		help="Initialize the app and print a health snapshot instead of serving forever",
	)

	watch_parser = subparsers.add_parser(
		"watch",
		help="Run the app and restart it when source or config files change",
	)
	watch_parser.add_argument(
		"--interval",
		type=float,
		default=1.0,
		help="Polling interval in seconds for file changes (default: 1.0)",
	)
	watch_parser.add_argument(
		"--path",
		action="append",
		dest="watch_paths",
		help="Additional file or directory to watch (can be repeated)",
	)
	return parser


def load_settings() -> AppSettings:
	settings = AppSettings.from_env()
	configure_logging(settings.log_level)
	return settings


def run_check_config() -> int:
	settings = load_settings()
	print(json.dumps(settings.safe_summary(), indent=2, sort_keys=True))
	return 0


def run_init_db() -> int:
	settings = load_settings()
	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
		payload = {
			"database_path": str(settings.database_path),
			"tables": list_tables(connection),
		}
		print(json.dumps(payload, indent=2, sort_keys=True))
		return 0
	finally:
		connection.close()


def run_application(once: bool) -> int:
	settings = load_settings()
	app_started_at = datetime.now(timezone.utc).isoformat()
	service_started_at = {
		service: app_started_at
		for service in settings.enabled_services
		if service in {"web", "jobs", "twitch", "discord"}
	}
	bootstrap_logger = logging.getLogger("qbot4k.bootstrap")
	bootstrap_logger.info(
		"starting application",
	)

	connection = connect_database(settings.database_path)
	try:
		initialize_database(connection)
	finally:
		connection.close()

	service_states = {
		service: "ready"
		for service in settings.enabled_services
		if service in {"web", "jobs"}
	}
	if "discord" in settings.enabled_services:
		discord_connector = DiscordConnector(
			settings.database_path,
			guild_ids=settings.discord_guild_ids,
			allow_bot_messages=settings.discord_allow_bot_messages,
		)
		service_states["discord"] = discord_connector.health_snapshot().status
	if "twitch" in settings.enabled_services:
		twitch_token_manager = TwitchTokenManager(
			initial_access_token=settings.twitch_bot_token or "",
			refresh_token=settings.twitch_refresh_token,
			client_id=settings.twitch_client_id,
			client_secret=settings.twitch_client_secret,
			on_token_refresh=_persist_refreshed_twitch_tokens,
		)
		twitch_connector = TwitchConnector(
			settings.database_path,
			join_command_channel=settings.twitch_join_command_channel,
			bootstrap_channels=settings.twitch_channels,
			token_manager=twitch_token_manager,
		)
		twitch_connector.configured_channels()
		service_states["twitch"] = twitch_connector.health_snapshot().status
	if "jobs" in settings.enabled_services:
		maintenance_report = run_maintenance_jobs(settings)
		logging.getLogger("qbot4k.jobs").info(
			"maintenance run complete messages_deleted=%s audit_deleted=%s rollups=%s backup=%s",
			maintenance_report.deleted_messages,
			maintenance_report.deleted_audit_log_rows,
			maintenance_report.rollup_rows,
			maintenance_report.backup_path,
		)
		if "discord" in settings.enabled_services and "twitch" in settings.enabled_services:
			announcements = run_twitch_live_announcement_job(settings)
			if announcements > 0:
				logging.getLogger("qbot4k.jobs").info("sent twitch live announcements count=%s", announcements)
		service_states["jobs"] = "ready"
	snapshot = build_health_snapshot(
		settings,
		service_states,
		service_started_at=service_started_at,
		app_started_at=app_started_at,
	)

	if once:
		print(json.dumps(snapshot, indent=2, sort_keys=True))
		return 0

	server = None
	server_thread = None
	service_threads: list[threading.Thread] = []
	managed_connectors: list[object] = []
	jobs_thread: threading.Thread | None = None
	discord_thread: threading.Thread | None = None
	twitch_thread: threading.Thread | None = None
	discord_connector: DiscordConnector | None = None
	twitch_connector: TwitchConnector | None = None
	health_logger = logging.getLogger("qbot4k.health")
	shutdown_event = threading.Event()
	handler_state = _install_shutdown_handlers(shutdown_event, health_logger)
	if "web" in settings.enabled_services:
		service_started_at.setdefault("web", datetime.now(timezone.utc).isoformat())
		server = create_health_server(
			settings,
			service_states,
			service_started_at=service_started_at,
			app_started_at=app_started_at,
		)
		server_thread = threading.Thread(target=server.serve_forever, daemon=True)
		server_thread.start()
		health_logger.info("health server listening")

	def job_loop() -> None:
		jobs_logger = logging.getLogger("qbot4k.jobs")
		while not shutdown_event.is_set():
			try:
				run_maintenance_jobs(settings)
				jobs_logger.info("maintenance run complete")
				if "discord" in settings.enabled_services and "twitch" in settings.enabled_services:
					announcements = run_twitch_live_announcement_job(settings)
					if announcements > 0:
						jobs_logger.info("sent twitch live announcements count=%s", announcements)
			except Exception:
				jobs_logger.exception("maintenance run failed")
			shutdown_event.wait(300)

	if "jobs" in settings.enabled_services:
		service_started_at.setdefault("jobs", datetime.now(timezone.utc).isoformat())
		jobs_thread = threading.Thread(target=job_loop, name="maintenance-jobs", daemon=True)
		jobs_thread.start()
		service_threads.append(jobs_thread)

	if "discord" in settings.enabled_services:
		discord_connector = DiscordConnector(
			settings.database_path,
			guild_ids=settings.discord_guild_ids,
			allow_bot_messages=settings.discord_allow_bot_messages,
		)
		managed_connectors.append(discord_connector)
		service_started_at.setdefault("discord", datetime.now(timezone.utc).isoformat())
		discord_thread = threading.Thread(
			target=discord_connector.run_forever,
			args=(settings.discord_bot_token or "",),
			name="discord-connector",
		)
		discord_thread.start()
		service_threads.append(discord_thread)

	if "twitch" in settings.enabled_services:
		twitch_token_manager = TwitchTokenManager(
			initial_access_token=settings.twitch_bot_token or "",
			refresh_token=settings.twitch_refresh_token,
			client_id=settings.twitch_client_id,
			client_secret=settings.twitch_client_secret,
			on_token_refresh=_persist_refreshed_twitch_tokens,
		)
		twitch_connector = TwitchConnector(
			settings.database_path,
			join_command_channel=settings.twitch_join_command_channel,
			bootstrap_channels=settings.twitch_channels,
			token_manager=twitch_token_manager,
		)
		managed_connectors.append(twitch_connector)
		service_started_at.setdefault("twitch", datetime.now(timezone.utc).isoformat())
		twitch_thread = threading.Thread(
			target=twitch_connector.run_forever,
			args=(settings.twitch_bot_token or "",),
			name="twitch-connector",
		)
		twitch_thread.start()
		service_threads.append(twitch_thread)

	def status_monitor_loop() -> None:
		last_written_bucket: dict[str, str] = {}
		last_written_is_up: dict[str, bool] = {}
		enabled_runtime_services = tuple(
			service
			for service in settings.enabled_services
			if service in {"web", "jobs", "twitch", "discord"}
		)

		def _bucket_start(now: datetime) -> str:
			return now.replace(second=0, microsecond=0).isoformat()

		def _should_write_sample(service_name: str, bucket_start: str, is_up: bool) -> bool:
			previous_bucket = last_written_bucket.get(service_name)
			if previous_bucket != bucket_start:
				return True
			previous_is_up = last_written_is_up.get(service_name)
			return previous_is_up is True and not is_up

		while not shutdown_event.is_set():
			if "web" in settings.enabled_services:
				service_states["web"] = "ready" if server_thread is not None and server_thread.is_alive() else "down"
			if "jobs" in settings.enabled_services:
				service_states["jobs"] = "ready" if jobs_thread is not None and jobs_thread.is_alive() else "down"
			if "discord" in settings.enabled_services:
				if discord_connector is None:
					service_states["discord"] = "down"
				elif discord_thread is not None and discord_thread.is_alive():
					service_states["discord"] = discord_connector.health_snapshot().status
				else:
					last_status = discord_connector.health_snapshot().status
					service_states["discord"] = last_status if last_status == "auth_failed" else "down"
			if "twitch" in settings.enabled_services:
				if twitch_connector is None:
					service_states["twitch"] = "down"
				elif twitch_thread is not None and twitch_thread.is_alive():
					service_states["twitch"] = twitch_connector.health_snapshot().status
				else:
					service_states["twitch"] = "down"

			now = datetime.now(timezone.utc)
			current_bucket = _bucket_start(now)
			reliability_samples: list[tuple[str, bool, str]] = []
			for service_name in enabled_runtime_services:
				status = str(service_states.get(service_name) or "down")
				is_up = status == "ready"
				if not _should_write_sample(service_name, current_bucket, is_up):
					continue
				reliability_samples.append((service_name, is_up, status))
				last_written_bucket[service_name] = current_bucket
				last_written_is_up[service_name] = is_up

			system_is_up = all(str(service_states.get(service_name) or "down") == "ready" for service_name in enabled_runtime_services)
			system_status = "ready" if system_is_up else "down"
			if _should_write_sample("system", current_bucket, system_is_up):
				reliability_samples.append(("system", system_is_up, system_status))
				last_written_bucket["system"] = current_bucket
				last_written_is_up["system"] = system_is_up

			if reliability_samples:
				connection = connect_database(settings.database_path)
				try:
					initialize_database(connection)
					for service_name, is_up, status in reliability_samples:
						upsert_service_reliability_bucket(
							connection,
							service_name=service_name,
							bucket_start=current_bucket,
							is_up=is_up,
							status=status,
						)
				except Exception:
					logging.getLogger("qbot4k.health").exception("failed to persist reliability sample")
				finally:
					connection.close()
			shutdown_event.wait(1)

	status_monitor_thread = threading.Thread(target=status_monitor_loop, name="service-status-monitor", daemon=True)
	status_monitor_thread.start()
	try:
		if service_threads:
			while any(thread.is_alive() for thread in service_threads):
				for thread in service_threads:
					thread.join(timeout=0.5)
				if shutdown_event.is_set():
					break
		elif server is not None:
			while server_thread.is_alive() and not shutdown_event.is_set():
				server_thread.join(timeout=0.5)
		else:
			print(json.dumps(snapshot, indent=2, sort_keys=True))
			return 0
	except KeyboardInterrupt:
		health_logger.info("application interrupted")
		shutdown_event.set()
	finally:
		shutdown_event.set()
		for connector in managed_connectors:
			stop = getattr(connector, "stop", None)
			if callable(stop):
				stop()
		if server is not None:
			server.shutdown()
			server.server_close()
			if server_thread is not None:
				server_thread.join(timeout=5)
		for thread in service_threads:
			thread.join(timeout=5)
		status_monitor_thread.join(timeout=2)
		_restore_shutdown_handlers(handler_state)

	return 0


def run_watch(interval: float, watch_paths: tuple[str, ...]) -> int:
	if interval <= 0:
		raise ConfigError("watch interval must be greater than zero")

	settings = load_settings()
	logger = logging.getLogger("qbot4k.watch")
	project_root = Path(__file__).resolve().parent.parent
	entrypoint = Path(__file__).resolve()

	paths_to_watch = [project_root / "src", project_root / ".env"]
	for raw_path in watch_paths:
		candidate = Path(raw_path)
		if not candidate.is_absolute():
			candidate = project_root / candidate
		paths_to_watch.append(candidate)

	def iter_watch_files() -> list[Path]:
		tracked: list[Path] = []
		seen: set[Path] = set()
		for path in paths_to_watch:
			if path.is_dir():
				for child in sorted(path.rglob("*.py")):
					if "__pycache__" in child.parts:
						continue
					resolved = child.resolve()
					if resolved not in seen:
						seen.add(resolved)
						tracked.append(resolved)
			elif path.is_file():
				resolved = path.resolve()
				if resolved not in seen:
					seen.add(resolved)
					tracked.append(resolved)
		return tracked

	def snapshot_fingerprints() -> dict[Path, tuple[int, int]]:
		fingerprints: dict[Path, tuple[int, int]] = {}
		for file_path in iter_watch_files():
			try:
				stat_result = file_path.stat()
			except FileNotFoundError:
				continue
			fingerprints[file_path] = (stat_result.st_mtime_ns, stat_result.st_size)
		for path in paths_to_watch:
			if path.is_dir():
				continue
			resolved = path.resolve()
			if resolved in fingerprints:
				continue
			try:
				stat_result = resolved.stat()
				fingerprints[resolved] = (stat_result.st_mtime_ns, stat_result.st_size)
			except FileNotFoundError:
				fingerprints[resolved] = (-1, -1)
		return fingerprints

	def changed_paths(previous: dict[Path, tuple[int, int]], current: dict[Path, tuple[int, int]]) -> list[Path]:
		changes: list[Path] = []
		for file_path, current_fingerprint in current.items():
			if previous.get(file_path) != current_fingerprint:
				changes.append(file_path)
		for file_path in previous:
			if file_path not in current:
				changes.append(file_path)
		return sorted(changes)

	def spawn_application_process() -> subprocess.Popen[bytes]:
		logger.info("starting application process")
		return subprocess.Popen(
			[sys.executable, str(entrypoint), "run"],
			cwd=str(project_root),
		)

	def stop_application_process(process: subprocess.Popen[bytes]) -> None:
		if process.poll() is not None:
			return
		try:
			process.send_signal(signal.SIGINT)
		except Exception:
			pass
		try:
			process.wait(timeout=8)
		except subprocess.TimeoutExpired:
			process.terminate()
			try:
				process.wait(timeout=4)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait(timeout=2)

	last_snapshot = snapshot_fingerprints()
	logger.info(
		"watch mode active interval=%.2fs tracked_files=%s",
		interval,
		len(last_snapshot),
	)
	if settings.safe_summary():
		logger.debug("watch settings loaded")

	process = spawn_application_process()
	try:
		while True:
			time.sleep(interval)
			current_snapshot = snapshot_fingerprints()
			changed = changed_paths(last_snapshot, current_snapshot)
			if changed:
				preview = ", ".join(str(path.relative_to(project_root)) for path in changed[:5])
				if len(changed) > 5:
					preview = f"{preview}, ..."
				logger.info("change detected: %s", preview)
				stop_application_process(process)
				process = spawn_application_process()
				last_snapshot = current_snapshot
				continue

			if process.poll() is not None:
				logger.warning("application exited with code %s; restarting", process.returncode)
				process = spawn_application_process()
				last_snapshot = current_snapshot
	except KeyboardInterrupt:
		logger.info("watch mode interrupted")
	finally:
		stop_application_process(process)

	return 0


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	try:
		if args.command == "check-config":
			return run_check_config()
		if args.command == "init-db":
			return run_init_db()
		if args.command == "run":
			return run_application(args.once)
		if args.command == "watch":
			return run_watch(args.interval, tuple(args.watch_paths or ()))
	except ConfigError as exc:
		print(f"Configuration error: {exc}", file=sys.stderr)
		return 2

	parser.error(f"Unknown command: {args.command}")
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
