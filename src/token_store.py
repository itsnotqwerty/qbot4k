from __future__ import annotations

import os
import threading
from pathlib import Path

_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_TWITCH_TOKEN_ENV_LOCK = threading.Lock()


def persist_refreshed_twitch_tokens(access_token: str, refresh_token: str | None) -> None:
	normalized_access_token = access_token.removeprefix("oauth:").strip()
	if not normalized_access_token:
		return

	updates = {
		"QBOT_TWITCH_BOT_TOKEN": f"oauth:{normalized_access_token}",
	}
	if refresh_token and refresh_token.strip():
		updates["QBOT_TWITCH_REFRESH_TOKEN"] = refresh_token.strip()

	for key, value in updates.items():
		os.environ[key] = value

	if not _DOTENV_PATH.exists():
		return

	with _TWITCH_TOKEN_ENV_LOCK:
		lines = _DOTENV_PATH.read_text(encoding="utf-8").splitlines()
		updated_lines: list[str] = []
		updated_keys: set[str] = set()
		for line in lines:
			stripped = line.strip()
			if not stripped or stripped.startswith("#") or "=" not in line:
				updated_lines.append(line)
				continue

			key, _existing_value = line.split("=", 1)
			normalized_key = key.strip()
			if normalized_key in updates:
				updated_lines.append(f"{normalized_key}={updates[normalized_key]}")
				updated_keys.add(normalized_key)
			else:
				updated_lines.append(line)

		for key, value in updates.items():
			if key not in updated_keys:
				updated_lines.append(f"{key}={value}")

		content = "\n".join(updated_lines)
		if updated_lines:
			content = f"{content}\n"
		_DOTENV_PATH.write_text(content, encoding="utf-8")
