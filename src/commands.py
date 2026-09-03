from __future__ import annotations

import json
import random
import re
import sqlite3
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus
from urllib.error import URLError
from urllib.request import Request, urlopen

from .contexts import TenantContext
from .db import (
	delete_simple_command_definition,
	get_command_definition,
	get_operator_account_by_discord_user_id,
	get_simple_command_definition,
	upsert_simple_command_definition,
)
from .intelligence.community import operator_has_permission
from .intelligence.userprofiles import get_canonical_user_profile_for_platform_account
from .intelligence.onboarding import self_verify_onboarding_member
from .surface_policy import require_non_http_surface


RESERVED_COMMAND_NAMES = {"addcom", "delcom", "editcom", "alias", "verify"}
_HTTP_TEMPLATE_CALL_PATTERN = re.compile(
	r"\{(GET|POST|PUT|DELETE)\}\((https?://[^\s)]+)\)(?:\[([^\]]+)\])?",
	re.IGNORECASE,
)
_HTTP_SELECTOR_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HTTP_SELECTOR_MAPPING_PATTERN = re.compile(
	r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^,;]+)\s*(?:[,;]|$)"
)
_RANDOM_RANGE_PATTERN = re.compile(r"\{(-?\d+|\{query\})\.\.(-?\d+|\{query\})\}")
_SANITIZED_RANGE_MIN = -1_000_000
_SANITIZED_RANGE_MAX = 1_000_000


@dataclass(frozen=True)
class CommandContext:
	platform: str
	database_path: Path
	connection: sqlite3.Connection
	author_platform_user_id: str
	author_username: str
	channel_id: str
	guild_id: str | None
	message_id: str | None
	content: str
	community_id: int | None = None
	command_name: str = ""
	command_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandField:
	name: str
	value: str
	inline: bool = False


@dataclass(frozen=True)
class CommandCard:
	title: str
	description: str
	fields: tuple[CommandField, ...] = ()
	footer: str | None = None
	color: int | None = None


@dataclass(frozen=True)
class CommandReply:
	card: CommandCard
	text_only: bool = False


CommandHandler = Callable[[CommandContext], CommandReply | None]


class CommandRegistry:
	def __init__(self, *, prefix: str = "!") -> None:
		self.prefix = prefix

	def dispatch(self, content: str, context: CommandContext) -> CommandReply | None:
		parsed = self.parse_command(content)
		if parsed is None:
			return None

		command_name, args = parsed
		resolved_context = replace(context, command_name=command_name, command_args=args)
		return _resolve_command_reply(resolved_context)

	def parse_command(self, content: str) -> tuple[str, tuple[str, ...]] | None:
		"""Parse command text without executing or resolving the command."""
		normalized = content.strip()
		if not normalized.startswith(self.prefix):
			return None

		command_body = normalized[len(self.prefix):].strip()
		if not command_body:
			return None

		parts = command_body.split()
		command_name = parts[0].casefold()
		args = tuple(parts[1:])
		return command_name, args


def build_default_command_registry() -> CommandRegistry:
	return CommandRegistry()


def require_command_surface(context: CommandContext, command_name: str) -> None:
	TenantContext.require(context.community_id)
	normalized_name = command_name.strip().casefold()
	surface = f"command:{normalized_name}" if normalized_name in RESERVED_COMMAND_NAMES | {"credit"} else "command:custom"
	guard = {
		"addcom": "command_operator",
		"editcom": "command_operator",
		"delcom": "command_operator",
		"alias": "command_operator",
		"verify": "platform_identity",
		"credit": "installation_context",
	}.get(normalized_name, "installation_context")
	require_non_http_surface(surface, guard=guard)
	if guard == "platform_identity" and not context.author_platform_user_id.strip():
		raise PermissionError("platform identity is required")


def render_command_reply(reply: CommandReply, platform: str) -> dict[str, object] | str:
	platform_name = platform.casefold().strip()
	if platform_name == "discord":
		return _render_discord_reply(reply)
	if platform_name == "twitch":
		return _render_plaintext_reply(reply)
	raise ValueError(f"unsupported command platform: {platform}")


def _resolve_command_reply(context: CommandContext) -> CommandReply | None:
	command_name = context.command_name.casefold().strip()
	if not command_name:
		return None
	if command_name == "addcom":
		return _addcom_command(context)
	if command_name == "editcom":
		return _editcom_command(context)
	if command_name == "delcom":
		return _delcom_command(context)
	if command_name == "alias":
		return _alias_command(context)
	if command_name == "credit":
		return _credit_command(context)
	if command_name == "verify":
		return _verify_command(context)
	simple_definition = get_simple_command_definition(context.connection, command_name)
	if simple_definition is None or not bool(simple_definition["enabled"]):
		return None
	return _simple_command(context, simple_definition)



def _addcom_command(context: CommandContext) -> CommandReply:
	if not _can_edit_commands(context):
		return _command_editing_denied_reply("addcom")
	if len(context.command_args) < 2:
		return _command_usage_reply("addcom", "Usage: !addcom !name response")
	command_name = _normalize_custom_command_name(context.command_args[0])
	response_template = " ".join(context.command_args[1:]).strip()
	if not command_name or not response_template:
		return _command_usage_reply("addcom", "Usage: !addcom !name response")
	if get_simple_command_definition(context.connection, command_name) is not None:
		return _command_result_reply(
			"!addcom",
			f"!{command_name} already exists. Use !editcom to update it.",
		)
	upsert_simple_command_definition(
		context.connection,
		command_name=command_name,
		response_template=response_template,
		enabled=True,
	)
	return _command_result_reply(
		"!addcom",
		f"Added !{command_name}",
		field_name="Response",
		field_value=response_template,
	)


def _editcom_command(context: CommandContext) -> CommandReply:
	if not _can_edit_commands(context):
		return _command_editing_denied_reply("editcom")
	if len(context.command_args) < 2:
		return _command_usage_reply("editcom", "Usage: !editcom !name response")
	command_name = _normalize_custom_command_name(context.command_args[0])
	response_template = " ".join(context.command_args[1:]).strip()
	if not command_name or not response_template:
		return _command_usage_reply("editcom", "Usage: !editcom !name response")
	if get_simple_command_definition(context.connection, command_name) is None:
		return _command_result_reply(
			"!editcom",
			f"!{command_name} does not exist. Use !addcom to create it.",
		)
	upsert_simple_command_definition(
		context.connection,
		command_name=command_name,
		response_template=response_template,
		enabled=True,
	)
	return _command_result_reply(
		"!editcom",
		f"Updated !{command_name}",
		field_name="Response",
		field_value=response_template,
	)


def _delcom_command(context: CommandContext) -> CommandReply:
	if not _can_edit_commands(context):
		return _command_editing_denied_reply("delcom")
	if not context.command_args:
		return _command_usage_reply("delcom", "Usage: !delcom !name")
	command_name = _normalize_custom_command_name(context.command_args[0])
	if not command_name:
		return _command_usage_reply("delcom", "Usage: !delcom !name")
	if get_simple_command_definition(context.connection, command_name) is None:
		return _command_result_reply(
			"!delcom",
			f"!{command_name} does not exist.",
		)
	delete_simple_command_definition(context.connection, command_name)
	return _command_result_reply(
		"!delcom",
		f"Deleted !{command_name}",
		field_name="Command",
		field_value=f"!{command_name}",
	)


def _alias_command(context: CommandContext) -> CommandReply:
	if not _can_edit_commands(context):
		return _command_editing_denied_reply("alias")
	if len(context.command_args) != 2:
		return _command_usage_reply("alias", "Usage: !alias !newcommand !oldcommand")

	new_command_name = _normalize_custom_command_name(context.command_args[0])
	source_command_name = _normalize_custom_command_name(context.command_args[1])
	if not new_command_name or not source_command_name:
		return _command_usage_reply("alias", "Usage: !alias !newcommand !oldcommand")

	if get_simple_command_definition(context.connection, new_command_name) is not None:
		return _command_result_reply(
			"!alias",
			f"!{new_command_name} already exists. Use !editcom to update it.",
		)

	source_definition = get_simple_command_definition(context.connection, source_command_name)
	if source_definition is None:
		return _command_result_reply(
			"!alias",
			f"!{source_command_name} does not exist.",
		)

	upsert_simple_command_definition(
		context.connection,
		command_name=new_command_name,
		response_template=str(source_definition[1]),
		enabled=bool(source_definition[2]),
	)

	return _command_result_reply(
		"!alias",
		f"Aliased !{new_command_name} to !{source_command_name}",
		field_name="Response",
		field_value=str(source_definition[1]),
	)


def _command_result_reply(
	title: str,
	description: str,
	*,
	field_name: str | None = None,
	field_value: str | None = None,
) -> CommandReply:
	fields: tuple[CommandField, ...] = ()
	if field_name is not None and field_value is not None:
		fields = (CommandField(name=field_name, value=field_value, inline=False),)
	return CommandReply(
		card=CommandCard(
			title=title,
			description=description,
			fields=fields,
		),
		text_only=True,
	)


def _command_usage_reply(command_name: str, description: str) -> CommandReply:
	return CommandReply(
		card=CommandCard(
			title=f"!{command_name}",
			description=description,
		),
		text_only=True,
	)


def _command_editing_denied_reply(command_name: str) -> CommandReply:
	return CommandReply(
		card=CommandCard(
			title=f"!{command_name}",
			description="Command editing is restricted to dashboard operators.",
		),
		text_only=True,
	)


def _normalize_custom_command_name(raw_name: str) -> str:
	command_name = raw_name.strip().lstrip("!").casefold()
	if command_name in RESERVED_COMMAND_NAMES or command_name == "credit":
		return ""
	return command_name


def _can_edit_commands(context: CommandContext) -> bool:
	try:
		community_id = TenantContext.require(context.community_id).community_id
	except (TypeError, ValueError):
		return False
	try:
		policy = require_non_http_surface(
			f"command:{context.command_name}", guard="command_operator"
		)
	except (PermissionError, ValueError):
		return False
	if context.platform == "discord":
		operator = get_operator_account_by_discord_user_id(
			context.connection, context.author_platform_user_id
		)
		return operator is not None and operator_has_permission(
			context.connection, operator_id=int(operator[0]), community_id=community_id,
			permission=policy.capability,
		)
	profile = get_canonical_user_profile_for_platform_account(
		context.connection,
		platform=context.platform,
		platform_user_id=context.author_platform_user_id,
	)
	if profile is None:
		return False
	operators = {
		str(row[1]): int(row[0])
		for row in context.connection.execute(
			"SELECT id,discord_user_id FROM operator_accounts"
		).fetchall()
	}
	if not operators:
		return False
	operator_ids = {
		operators[account.platform_user_id]
		for account in profile.linked_accounts
		if account.platform == "discord" and account.platform_user_id in operators
	}
	return any(
		operator_has_permission(
			context.connection, operator_id=operator_id, community_id=community_id,
			permission=policy.capability,
		)
		for operator_id in operator_ids
	)


def _verify_command(context: CommandContext) -> CommandReply:
	if context.platform != "discord" or not context.guild_id:
		return _command_result_reply(
			"!verify", "Self-service verification is available in Discord servers only."
		)
	try:
		community_id = TenantContext.require(context.community_id).community_id
		self_verify_onboarding_member(
			context.connection, community_id=community_id,
			platform_user_id=context.author_platform_user_id,
		)
	except LookupError:
		return _command_result_reply(
			"!verify", "No pending verification checkpoint was found for you."
		)
	except PermissionError as exc:
		return _command_result_reply("!verify", str(exc).capitalize() + ".")
	return _command_result_reply(
		"!verify", "Verification complete. Welcome to the community."
	)


def _credit_command(context: CommandContext) -> CommandReply | None:
	profile = get_canonical_user_profile_for_platform_account(
		context.connection,
		platform=context.platform,
		platform_user_id=context.author_platform_user_id,
	)
	if profile is None:
		raise ValueError(f"{context.platform} user profile not found")
	definition = get_command_definition(context.connection, "credit")
	if definition is not None and not bool(definition["enabled"]):
		return None
	title = str(definition["title"]) if definition is not None else "Social Credit Profile"
	description_template = (
		str(definition["description_template"]) if definition is not None else "Profile for {display_name}"
	)
	footer_template = str(definition["footer_template"]) if definition is not None and definition["footer_template"] is not None else "{platform} user: {author_username}"

	linked_accounts = _format_linked_accounts(profile.linked_accounts)
	recent_note = _format_recent_note(profile.notes)
	field_values = (
		CommandField(name="Social Credit", value=str(profile.current_reputation_score), inline=True),
		CommandField(name="Power User", value="Yes" if profile.candidate_flag else "No", inline=True),
		CommandField(name="Linked Accounts", value=linked_accounts, inline=False),
		CommandField(name="Score Band", value=profile.score_band.title(), inline=True),
		CommandField(name="Evidence Confidence", value=f"{profile.score_confidence * 100:.0f}%", inline=True),
	)
	fields = list(field_values)
	if recent_note is not None:
		fields.append(CommandField(name="Latest Note", value=recent_note, inline=False))
	description = _format_command_template(
		description_template,
		display_name=profile.primary_display_name,
		author_username=context.author_username,
		platform=context.platform,
	)
	footer = _format_command_template(
		footer_template,
		display_name=profile.primary_display_name,
		author_username=context.author_username,
		platform=context.platform,
	)

	card = CommandCard(
		title=title,
		description=description,
		color=_score_color(profile.current_reputation_score),
		fields=tuple(fields),
		footer=footer,
	)
	return CommandReply(card=card)


def _simple_command(context: CommandContext, definition: sqlite3.Row) -> CommandReply:
	response_template = str(definition[1])
	response_text = _format_command_template(
		response_template,
		display_name=_context_values(context)["display_name"],
		author_username=context.author_username,
		platform=context.platform,
		score=_context_values(context)["score"],
		power_user=_context_values(context)["power_user"],
		linked_accounts=_context_values(context)["linked_accounts"],
		latest_note=_context_values(context)["latest_note"],
		command_name=context.command_name,
		query=" ".join(context.command_args).strip(),
	)
	card = CommandCard(
		title=f"!{context.command_name}",
		description=response_text,
	)
	return CommandReply(card=card, text_only=True)


def _render_discord_reply(reply: CommandReply) -> dict[str, object]:
	if reply.text_only:
		return {
			"allowed_mentions": {"parse": []},
			"content": reply.card.description,
		}
	card = reply.card
	embed = {
		"title": card.title,
		"description": card.description,
		"color": card.color,
		"fields": [
			{"name": field.name, "value": field.value, "inline": field.inline}
			for field in card.fields
		],
	}
	if card.footer is not None:
		embed["footer"] = {"text": card.footer}
	return {
		"allowed_mentions": {"parse": []},
		"embeds": [embed],
	}


def _render_plaintext_reply(reply: CommandReply) -> str:
	card = reply.card
	if reply.text_only or (not card.fields and card.footer is None):
		return card.description
	parts = [card.title, card.description]
	parts.extend(f"{field.name}: {field.value}" for field in card.fields)
	if card.footer is not None:
		parts.append(card.footer)
	return " | ".join(part for part in parts if part)


def _format_linked_accounts(accounts: tuple[object, ...]) -> str:
	if not accounts:
		return "No linked accounts"

	lines: list[str] = []
	for account in accounts:
		platform = str(getattr(account, "platform", ""))
		username = str(getattr(account, "username", ""))
		context = getattr(account, "guild_or_channel_context", None)
		detail = f" ({context})" if context else ""
		lines.append(f"{platform}: {username}{detail}")
	return "\n".join(lines)


def _format_recent_note(notes: tuple[object, ...]) -> str | None:
	if not notes:
		return None

	latest_note = notes[-1]
	if len(latest_note) < 3:
		return None
	return str(latest_note[2])


def _score_color(score: int) -> int:
	if score >= 800:
		return 0x22C55E
	if score >= 650:
		return 0x38BDF8
	if score >= 500:
		return 0xF59E0B
	return 0xEF4444


def _context_values(context: CommandContext) -> dict[str, object]:
	profile = get_canonical_user_profile_for_platform_account(
		context.connection,
		platform=context.platform,
		platform_user_id=context.author_platform_user_id,
	)
	linked_accounts = ""
	score = 500
	power_user = "No"
	latest_note = ""
	display_name = context.author_username
	if profile is not None:
		display_name = profile.primary_display_name
		linked_accounts = _format_linked_accounts(profile.linked_accounts)
		score = profile.current_reputation_score
		power_user = "Yes" if profile.candidate_flag else "No"
		latest_note = _format_recent_note(profile.notes) or ""
	return {
		"display_name": display_name,
		"score": score,
		"power_user": power_user,
		"linked_accounts": linked_accounts,
		"latest_note": latest_note,
		"query": " ".join(context.command_args).strip(),
	}


def _format_command_template(template: str, **values: object) -> str:
	resolved_template = _resolve_random_range_templates(template, values)
	resolved_template = _resolve_http_template_calls(resolved_template, values)
	try:
		return resolved_template.format(**values)
	except Exception:
		return resolved_template


def _resolve_random_range_templates(template: str, values: dict[str, object]) -> str:
	def _replace(match: re.Match[str]) -> str:
		lower_bound = _resolve_range_bound(match.group(1), values)
		upper_bound = _resolve_range_bound(match.group(2), values)
		if lower_bound > upper_bound:
			lower_bound, upper_bound = upper_bound, lower_bound
		return str(random.randint(lower_bound, upper_bound))

	return _RANDOM_RANGE_PATTERN.sub(_replace, template)


def _resolve_range_bound(raw_bound: str, values: dict[str, object]) -> int:
	binding = raw_bound.strip()
	if binding == "{query}":
		query_value = str(values.get("query") or "")
		number_match = re.search(r"-?\d+", query_value)
		if number_match is None:
			return 0
		parsed = int(number_match.group(0))
		return max(_SANITIZED_RANGE_MIN, min(_SANITIZED_RANGE_MAX, parsed))

	parsed = int(binding)
	return max(_SANITIZED_RANGE_MIN, min(_SANITIZED_RANGE_MAX, parsed))


def _resolve_http_template_calls(template: str, values: dict[str, object]) -> str:
	response_cache: dict[tuple[str, str], str] = {}

	def _replace(match: re.Match[str]) -> str:
		method = match.group(1).upper()
		url = _substitute_http_url_template(match.group(2), values)
		selector_spec = (match.group(3) or "").strip()
		selectors = _parse_http_selector_spec(selector_spec) if selector_spec else ()
		for selector in selectors:
			if ":" not in selector:
				continue
			alias, _path = selector.split(":", 1)
			alias_key = alias.strip()
			if alias_key and _HTTP_SELECTOR_ALIAS_PATTERN.fullmatch(alias_key) is not None:
				values.setdefault(alias_key, f"{{{alias_key}}}")

		decoded_body = _fetch_http_template_response(method, url, response_cache)
		if decoded_body is None:
			return ""

		if not selector_spec:
			return _escape_format_braces(decoded_body)

		if not selectors:
			return ""

		try:
			json_payload = json.loads(decoded_body)
		except json.JSONDecodeError:
			return ""

		inline_values: list[str] = []
		for selector in selectors:
			if ":" in selector:
				alias, json_path = selector.split(":", 1)
				alias_key = alias.strip()
				path_key = json_path.strip()
				if not alias_key or not path_key:
					continue
				if _HTTP_SELECTOR_ALIAS_PATTERN.fullmatch(alias_key) is None:
					continue
				extracted_value = _extract_json_path_value(json_payload, path_key)
				if extracted_value is None:
					values.setdefault(alias_key, f"{{{alias_key}}}")
				else:
					values[alias_key] = _render_extracted_json_value(extracted_value)
				continue

			path_key = selector.strip()
			if not path_key:
				continue
			extracted_value = _extract_json_path_value(json_payload, path_key)
			if extracted_value is None:
				continue
			inline_values.append(_render_extracted_json_value(extracted_value))

		if inline_values:
			return _escape_format_braces(" ".join(inline_values))
		return ""

	return _HTTP_TEMPLATE_CALL_PATTERN.sub(_replace, template)


def _fetch_http_template_response(
	method: str,
	url: str,
	response_cache: dict[tuple[str, str], str],
) -> str | None:
	cache_key = (method, url)
	if cache_key in response_cache:
		return response_cache[cache_key]

	try:
		request = Request(
			url,
			headers={
				"Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
				"User-Agent": "qbot4k/1.0 (+https://example.invalid/qbot4k)",
			},
			method=method,
		)
		with urlopen(request, timeout=5) as response:
			body = response.read()
	except (URLError, ValueError):
		return None

	decoded_body = body.decode("utf-8", errors="replace").strip()
	response_cache[cache_key] = decoded_body
	return decoded_body


def _parse_http_selector_spec(selector_spec: str) -> tuple[str, ...]:
	trimmed_spec = selector_spec.strip()
	if not trimmed_spec:
		return ()

	matches = list(_HTTP_SELECTOR_MAPPING_PATTERN.finditer(trimmed_spec))
	if matches:
		position = 0
		mapped_selectors: list[str] = []
		for match in matches:
			if match.start() != position:
				break
			alias = match.group(1).strip()
			path = match.group(2).strip()
			mapped_selectors.append(f"{alias}:{path}")
			position = match.end()
		else:
			if position == len(trimmed_spec):
				return tuple(mapped_selectors)

	selectors = [segment.strip() for segment in re.split(r"[,;]", trimmed_spec) if segment.strip()]
	return tuple(selectors)


def _substitute_http_url_template(url_template: str, values: dict[str, object]) -> str:
	query_value = str(values.get("query") or "").strip()
	return url_template.replace("{query}", quote_plus(query_value))


def _extract_json_path_value(payload: object, path: str) -> object | None:
	segments = [segment.strip() for segment in path.split(".") if segment.strip()]
	if not segments:
		return None

	current: object = payload
	for segment in segments:
		if isinstance(current, dict):
			if segment not in current:
				return None
			current = current[segment]
			continue
		if isinstance(current, list) and segment.isdigit():
			index = int(segment)
			if index < 0 or index >= len(current):
				return None
			current = current[index]
			continue
		return None

	return current


def _render_extracted_json_value(value: object) -> str:
	if value is None:
		return ""
	if isinstance(value, str):
		return value
	if isinstance(value, (dict, list, bool, int, float)):
		return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
	return str(value)


def _escape_format_braces(value: str) -> str:
	return value.replace("{", "{{").replace("}", "}}")
