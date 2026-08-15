from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    pass


_SYSTEMD_SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")


_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_environment_file(
        environment_path: Path, *, required: bool = False) -> dict[str, str]:
    if not environment_path.exists():
        if required:
            raise ConfigError(f"Environment file does not exist: {environment_path}")
        return {}
    if not environment_path.is_file():
        raise ConfigError(f"Environment file is not a regular file: {environment_path}")

    try:
        raw_contents = environment_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"Unable to read environment file {environment_path}: {exc}"
        ) from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(raw_contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise ConfigError(
                f"Invalid environment assignment at {environment_path}:{line_number}"
            )

        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ConfigError(
                f"Invalid environment key at {environment_path}:{line_number}"
            )
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def _parse_csv(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()

    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def _parse_int(name: str, raw_value: str | None, default: int) -> int:
    if raw_value is None or raw_value == "":
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _parse_bool(raw_value: str | None, default: bool = False) -> bool:
    if raw_value is None or raw_value == "":
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class AppSettings:
    database_path: Path
    backup_dir: Path
    dashboard_host: str
    dashboard_port: int
    dashboard_session_secret: str | None
    systemd_service_name: str
    log_level: str
    enabled_services: tuple[str, ...]
    twitch_channels: tuple[str, ...]
    twitch_join_command_channel: str
    discord_guild_ids: tuple[str, ...]
    discord_allow_bot_messages: bool
    operator_guild_ids: tuple[str, ...]
    message_retention_days: int
    audit_retention_days: int
    twitch_bot_token: str | None
    twitch_refresh_token: str | None
    twitch_client_id: str | None
    twitch_client_secret: str | None
    discord_bot_token: str | None
    discord_oauth_client_id: str | None
    discord_oauth_client_secret: str | None
    discord_oauth_redirect_uri: str | None
    moderation_shadow_mode: bool
    ingest_api_token: str | None
    maintenance_interval_seconds: int
    analytics_interval_seconds: int
    backup_interval_seconds: int
    backup_retention_count: int
    raw_archive_dir: Path
    default_community_slug: str
    twitch_eventsub_secret: str | None
    twitch_eventsub_callback_url: str | None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> AppSettings:
        repo_root = Path(__file__).resolve().parents[1]
        if env is None:
            env_map = _read_environment_file(repo_root / ".env")
            env_map.update(os.environ)
        else:
            env_map = dict(env)

        if env_file is not None:
            explicit_path = Path(env_file).expanduser().resolve()
            # An explicitly selected file is authoritative, including over
            # inherited values. This makes --env-file deterministic under
            # systemd, interactive shells, and configuration checks.
            env_map.update(_read_environment_file(explicit_path, required=True))

        database_raw = env_map.get("QBOT_DATABASE_PATH")
        if not database_raw:
            raise ConfigError("QBOT_DATABASE_PATH is required")

        enabled_services = _parse_csv(env_map.get("QBOT_ENABLED_SERVICES", "web,jobs,analysis"))
        if not enabled_services:
            enabled_services = ("web", "jobs", "analysis")

        allowed_services = {"web", "jobs", "twitch", "discord", "analysis"}
        unknown_services = sorted(set(enabled_services) - allowed_services)
        if unknown_services:
            raise ConfigError(
                f"Unknown services requested: {', '.join(unknown_services)}"
            )
        
        if (
            {"discord", "twitch"} & set(enabled_services)
            and "analysis" not in enabled_services
        ):
            raise ConfigError(
                "analysis service is required when a "
                "collection service is enabled"
            )

        log_level = env_map.get("QBOT_LOG_LEVEL", "INFO").upper()
        valid_log_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if log_level not in valid_log_levels:
            raise ConfigError(
                f"QBOT_LOG_LEVEL must be one of: {', '.join(sorted(valid_log_levels))}"
            )

        twitch_channels = _parse_csv(env_map.get("QBOT_TWITCH_CHANNELS"))
        if not twitch_channels:
            twitch_channels = ("its_not_qwerty",)

        database_path = Path(database_raw).expanduser().resolve()
        settings = cls(
            database_path=database_path,
            backup_dir=Path(
                env_map.get("QBOT_BACKUP_DIR", "./var/backups")
            ).expanduser().resolve(),
            dashboard_host=env_map.get("QBOT_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_parse_int(
                "QBOT_DASHBOARD_PORT",
                env_map.get("QBOT_DASHBOARD_PORT"),
                8080,
            ),
            dashboard_session_secret=env_map.get("QBOT_DASHBOARD_SESSION_SECRET"),
            systemd_service_name=env_map.get(
                "QBOT_SYSTEMD_SERVICE_NAME",
                "qbot4k.service",
            ).strip(),
            log_level=log_level,
            enabled_services=enabled_services,
            twitch_channels=twitch_channels,
            twitch_join_command_channel=env_map.get(
                "QBOT_TWITCH_JOIN_COMMAND_CHANNEL",
                "its_not_qwerty",
            ).strip(),
            discord_guild_ids=_parse_csv(env_map.get("QBOT_DISCORD_GUILD_IDS")),
            discord_allow_bot_messages=_parse_bool(
                env_map.get("QBOT_DISCORD_ALLOW_BOT_MESSAGES"),
                default=False,
            ),
            operator_guild_ids=_parse_csv(env_map.get("QBOT_OPERATOR_GUILD_IDS")),
            message_retention_days=_parse_int(
                "QBOT_MESSAGE_RETENTION_DAYS",
                env_map.get("QBOT_MESSAGE_RETENTION_DAYS"),
                90,
            ),
            audit_retention_days=_parse_int(
                "QBOT_AUDIT_RETENTION_DAYS",
                env_map.get("QBOT_AUDIT_RETENTION_DAYS"),
                365,
            ),
            twitch_bot_token=env_map.get("QBOT_TWITCH_BOT_TOKEN"),
            twitch_refresh_token=env_map.get("QBOT_TWITCH_REFRESH_TOKEN"),
            twitch_client_id=env_map.get("QBOT_TWITCH_CLIENT_ID"),
            twitch_client_secret=env_map.get("QBOT_TWITCH_CLIENT_SECRET"),
            discord_bot_token=env_map.get("QBOT_DISCORD_BOT_TOKEN"),
            discord_oauth_client_id=env_map.get("QBOT_DISCORD_OAUTH_CLIENT_ID"),
            discord_oauth_client_secret=env_map.get(
                "QBOT_DISCORD_OAUTH_CLIENT_SECRET"
            ),
            discord_oauth_redirect_uri=env_map.get(
                "QBOT_DISCORD_OAUTH_REDIRECT_URI"
            ),
            moderation_shadow_mode=_parse_bool(
                env_map.get("QBOT_MODERATION_SHADOW_MODE"),
                default=False,
            ),
            ingest_api_token=env_map.get("QBOT_INGEST_API_TOKEN"),
            maintenance_interval_seconds=_parse_int(
                "QBOT_MAINTENANCE_INTERVAL_SECONDS",
                env_map.get("QBOT_MAINTENANCE_INTERVAL_SECONDS"),
                60,
            ),
            analytics_interval_seconds=_parse_int(
                "QBOT_ANALYTICS_INTERVAL_SECONDS",
                env_map.get("QBOT_ANALYTICS_INTERVAL_SECONDS"),
                300,
            ),
            backup_interval_seconds=_parse_int(
                "QBOT_BACKUP_INTERVAL_SECONDS",
                env_map.get("QBOT_BACKUP_INTERVAL_SECONDS"),
                3600,
            ),
            backup_retention_count=_parse_int(
                "QBOT_BACKUP_RETENTION_COUNT",
                env_map.get("QBOT_BACKUP_RETENTION_COUNT"),
                48,
            ),
            raw_archive_dir=Path(
                env_map.get("QBOT_RAW_ARCHIVE_DIR", str(database_path.parent / "raw-events"))
            ).expanduser().resolve(),
            default_community_slug=env_map.get(
                "QBOT_DEFAULT_COMMUNITY_SLUG", "default"
            ).strip().casefold(),
            twitch_eventsub_secret=env_map.get("QBOT_TWITCH_EVENTSUB_SECRET"),
            twitch_eventsub_callback_url=env_map.get("QBOT_TWITCH_EVENTSUB_CALLBACK_URL"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not _SYSTEMD_SERVICE_PATTERN.fullmatch(self.systemd_service_name):
            raise ConfigError(
                "QBOT_SYSTEMD_SERVICE_NAME must be a valid .service unit name"
            )
        if self.dashboard_port <= 0 or self.dashboard_port > 65535:
            raise ConfigError("QBOT_DASHBOARD_PORT must be between 1 and 65535")

        if self.message_retention_days <= 0:
            raise ConfigError("QBOT_MESSAGE_RETENTION_DAYS must be greater than zero")

        if self.audit_retention_days <= 0:
            raise ConfigError("QBOT_AUDIT_RETENTION_DAYS must be greater than zero")

        if not self.default_community_slug:
            raise ConfigError("QBOT_DEFAULT_COMMUNITY_SLUG must not be empty")

        if self.twitch_eventsub_secret and len(self.twitch_eventsub_secret) < 16:
            raise ConfigError("QBOT_TWITCH_EVENTSUB_SECRET must be at least 16 characters")

        for name, value in (
            ("QBOT_MAINTENANCE_INTERVAL_SECONDS", self.maintenance_interval_seconds),
            ("QBOT_ANALYTICS_INTERVAL_SECONDS", self.analytics_interval_seconds),
            ("QBOT_BACKUP_INTERVAL_SECONDS", self.backup_interval_seconds),
            ("QBOT_BACKUP_RETENTION_COUNT", self.backup_retention_count),
        ):
            if value <= 0:
                raise ConfigError(f"{name} must be greater than zero")

        if "web" in self.enabled_services:
            required_web_values = {
                "QBOT_DASHBOARD_SESSION_SECRET": self.dashboard_session_secret,
                "QBOT_DISCORD_OAUTH_CLIENT_ID": self.discord_oauth_client_id,
                "QBOT_DISCORD_OAUTH_CLIENT_SECRET": self.discord_oauth_client_secret,
            }
            missing_web_values = [
                key for key, value in required_web_values.items() if not value
            ]
            if missing_web_values:
                raise ConfigError(
                    f"Missing web configuration: {', '.join(missing_web_values)}"
                )
            if not self.operator_guild_ids:
                raise ConfigError(
                    "QBOT_OPERATOR_GUILD_IDS is required when web service is enabled"
                )

        if "twitch" in self.enabled_services:
            if not self.twitch_bot_token:
                raise ConfigError(
                    "QBOT_TWITCH_BOT_TOKEN is required when twitch service is enabled"
                )
            if not self.twitch_join_command_channel:
                raise ConfigError(
                    "QBOT_TWITCH_JOIN_COMMAND_CHANNEL must not be empty when twitch service is enabled"
                )
            if self.twitch_refresh_token:
                required_refresh_values = {
                    "QBOT_TWITCH_CLIENT_ID": self.twitch_client_id,
                    "QBOT_TWITCH_CLIENT_SECRET": self.twitch_client_secret,
                }
                missing_refresh_values = [
                    key for key, value in required_refresh_values.items() if not value
                ]
                if missing_refresh_values:
                    raise ConfigError(
                        "Missing Twitch refresh configuration: "
                        f"{', '.join(missing_refresh_values)}"
                    )

        if "discord" in self.enabled_services:
            if not self.discord_bot_token:
                raise ConfigError(
                    "QBOT_DISCORD_BOT_TOKEN is required when discord service is enabled"
                )
            if (
                "jobs" in self.enabled_services
                and "twitch" in self.enabled_services
                and not self.discord_guild_ids
            ):
                raise ConfigError(
                    "QBOT_DISCORD_GUILD_IDS is required when jobs, twitch, and discord services are enabled"
                )

    def safe_summary(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "backup_dir": str(self.backup_dir),
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
            "systemd_service_name": self.systemd_service_name,
            "log_level": self.log_level,
            "enabled_services": list(self.enabled_services),
            "twitch_channels": list(self.twitch_channels),
            "twitch_join_command_channel": self.twitch_join_command_channel,
            "discord_guild_ids": list(self.discord_guild_ids),
            "discord_allow_bot_messages": self.discord_allow_bot_messages,
            "operator_guild_ids": list(self.operator_guild_ids),
            "message_retention_days": self.message_retention_days,
            "audit_retention_days": self.audit_retention_days,
            "web_auth_configured": bool(
                self.dashboard_session_secret
                and self.discord_oauth_client_id
                and self.discord_oauth_client_secret
            ),
            "twitch_configured": bool(self.twitch_bot_token),
            "twitch_refresh_configured": bool(
                self.twitch_refresh_token
                and self.twitch_client_id
                and self.twitch_client_secret
            ),
            "discord_configured": bool(self.discord_bot_token),
            "moderation_shadow_mode": self.moderation_shadow_mode,
            "ingest_api_token_configured": bool(self.ingest_api_token),
            "maintenance_interval_seconds": self.maintenance_interval_seconds,
            "analytics_interval_seconds": self.analytics_interval_seconds,
            "backup_interval_seconds": self.backup_interval_seconds,
            "backup_retention_count": self.backup_retention_count,
            "raw_archive_dir": str(self.raw_archive_dir),
            "default_community_slug": self.default_community_slug,
            "twitch_eventsub_configured": bool(
                self.twitch_eventsub_secret and self.twitch_eventsub_callback_url
            ),
        }
