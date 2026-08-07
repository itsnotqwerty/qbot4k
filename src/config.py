from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    pass


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


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
    discord_bot_token: str | None
    discord_oauth_client_id: str | None
    discord_oauth_client_secret: str | None
    discord_oauth_redirect_uri: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppSettings:
        repo_root = Path(__file__).resolve().parents[1]
        _load_dotenv(repo_root / ".env")

        env_map = dict(os.environ if env is None else env)

        database_raw = env_map.get("QBOT_DATABASE_PATH")
        if not database_raw:
            raise ConfigError("QBOT_DATABASE_PATH is required")

        enabled_services = _parse_csv(env_map.get("QBOT_ENABLED_SERVICES", "web,jobs"))
        if not enabled_services:
            enabled_services = ("web", "jobs")

        allowed_services = {"web", "jobs", "twitch", "discord"}
        unknown_services = sorted(set(enabled_services) - allowed_services)
        if unknown_services:
            raise ConfigError(
                f"Unknown services requested: {', '.join(unknown_services)}"
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

        settings = cls(
            database_path=Path(database_raw).expanduser().resolve(),
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
            discord_bot_token=env_map.get("QBOT_DISCORD_BOT_TOKEN"),
            discord_oauth_client_id=env_map.get("QBOT_DISCORD_OAUTH_CLIENT_ID"),
            discord_oauth_client_secret=env_map.get(
                "QBOT_DISCORD_OAUTH_CLIENT_SECRET"
            ),
            discord_oauth_redirect_uri=env_map.get(
                "QBOT_DISCORD_OAUTH_REDIRECT_URI"
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.dashboard_port <= 0 or self.dashboard_port > 65535:
            raise ConfigError("QBOT_DASHBOARD_PORT must be between 1 and 65535")

        if self.message_retention_days <= 0:
            raise ConfigError("QBOT_MESSAGE_RETENTION_DAYS must be greater than zero")

        if self.audit_retention_days <= 0:
            raise ConfigError("QBOT_AUDIT_RETENTION_DAYS must be greater than zero")

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

        if "twitch" in self.enabled_services:
            if not self.twitch_bot_token:
                raise ConfigError(
                    "QBOT_TWITCH_BOT_TOKEN is required when twitch service is enabled"
                )
            if not self.twitch_join_command_channel:
                raise ConfigError(
                    "QBOT_TWITCH_JOIN_COMMAND_CHANNEL must not be empty when twitch service is enabled"
                )

        if "discord" in self.enabled_services:
            if not self.discord_bot_token:
                raise ConfigError(
                    "QBOT_DISCORD_BOT_TOKEN is required when discord service is enabled"
                )

    def safe_summary(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "backup_dir": str(self.backup_dir),
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
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
            "discord_configured": bool(self.discord_bot_token),
        }