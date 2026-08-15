from __future__ import annotations

import os
import threading
import tempfile
import re
from pathlib import Path

_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_TWITCH_TOKEN_ENV_LOCK = threading.Lock()


def configure_token_store(env_path: str | Path | None) -> Path:
    """Point token rotation at the authoritative runtime environment file."""
    global _DOTENV_PATH
    if env_path is not None:
        _DOTENV_PATH = Path(env_path).expanduser().resolve()
    return _DOTENV_PATH


def update_env_value(contents: str, key: str, value: str) -> str:
    replacement = f"{key}={value}"

    pattern = re.compile(
        rf"^(?:export\s+)?{re.escape(key)}=.*$",
        flags=re.MULTILINE,
    )

    if pattern.search(contents):
        return pattern.sub(lambda _match: replacement, contents, count=1)

    separator = "" if not contents or contents.endswith("\n") else "\n"
    return f"{contents}{separator}{replacement}\n"


def persist_refreshed_twitch_tokens(
    access_token: str,
    refresh_token: str | None,
) -> None:
    env_path = _DOTENV_PATH
    with _TWITCH_TOKEN_ENV_LOCK:
        contents = env_path.read_text(
            encoding="utf-8") if env_path.exists() else ""

        contents = update_env_value(
            contents,
            "QBOT_TWITCH_BOT_TOKEN",
            access_token,
        )

        if refresh_token:
            contents = update_env_value(
                contents,
                "QBOT_TWITCH_REFRESH_TOKEN",
                refresh_token,
            )

        env_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=env_path.parent,
            prefix=f".{env_path.name}.",
            text=True,
        )
        temporary_path = Path(temporary_name)

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                file.write(contents)
                file.flush()
                os.fsync(file.fileno())

            os.chmod(temporary_path, 0o640)
            os.replace(temporary_path, env_path)
        finally:
            temporary_path.unlink(missing_ok=True)
