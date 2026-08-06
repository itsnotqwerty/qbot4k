from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone

RESET = "\x1b[0m"
DIM = "\x1b[2m"
COLORS = {
	logging.DEBUG: "\x1b[36m",
	logging.INFO: "\x1b[32m",
	logging.WARNING: "\x1b[33m",
	logging.ERROR: "\x1b[31m",
	logging.CRITICAL: "\x1b[35m",
}
EMOJIS = {
	logging.WARNING: "⚠",
	logging.ERROR: "✖",
	logging.CRITICAL: "‼",
}


def _simplify_json_text(raw_text: str) -> str | None:
	text = raw_text.strip()
	if not text:
		return None

	try:
		parsed = json.loads(text)
	except json.JSONDecodeError:
		return None

	if isinstance(parsed, (dict, list)):
		return json.dumps(parsed, separators=(",", ":"), sort_keys=True)

	return None


def _coerce_message(message: object) -> str:
	if isinstance(message, str):
		simplified_json = _simplify_json_text(message)
		if simplified_json is not None:
			return simplified_json
		return message

	if isinstance(message, Mapping):
		return json.dumps(message, separators=(",", ":"), sort_keys=True)

	return str(message)


class CompactColorLogFormatter(logging.Formatter):
	def format(self, record: logging.LogRecord) -> str:
		color = COLORS.get(record.levelno, "")
		emoji = EMOJIS.get(record.levelno, "")
		payload = {
			"timestamp": datetime.now(timezone.utc).isoformat(),
			"level": record.levelname,
			"logger": record.name,
			"message": _coerce_message(record.getMessage()),
		}
		line = f"{payload['timestamp']} · {payload['logger']} · {payload['message']}"
		if emoji:
			line = f"{emoji} {line}"
		if color:
			return f"{color}{line}{RESET}"
		return line


def configure_logging(log_level: str) -> None:
	handler = logging.StreamHandler()
	handler.setFormatter(CompactColorLogFormatter())

	root_logger = logging.getLogger()
	root_logger.handlers.clear()
	root_logger.addHandler(handler)
	root_logger.setLevel(getattr(logging, log_level))