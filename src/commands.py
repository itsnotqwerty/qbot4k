from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .db import (
	delete_simple_command_definition,
	get_command_definition,
	get_operator_account_by_discord_user_id,
	get_simple_command_definition,
	upsert_simple_command_definition,
)
from .intelligence.userprofiles import get_canonical_user_profile_for_platform_account


RESERVED_COMMAND_NAMES = {"addcom", "delcom", "editcom"}


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
		parsed = self._parse_command(content)
		if parsed is None:
			return None

		command_name, args = parsed
		resolved_context = replace(context, command_name=command_name, command_args=args)
		return _resolve_command_reply(resolved_context)

	def _parse_command(self, content: str) -> tuple[str, tuple[str, ...]] | None:
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
	if command_name == "credit":
		return _credit_command(context)
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
	if context.platform == "discord":
		return get_operator_account_by_discord_user_id(context.connection, context.author_platform_user_id) is not None
	profile = get_canonical_user_profile_for_platform_account(
		context.connection,
		platform=context.platform,
		platform_user_id=context.author_platform_user_id,
	)
	if profile is None:
		return False
	operator_ids = {
		str(row[0])
		for row in context.connection.execute("SELECT discord_user_id FROM operator_accounts").fetchall()
	}
	if not operator_ids:
		return False
	return any(
		account.platform == "discord" and account.platform_user_id in operator_ids
		for account in profile.linked_accounts
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
	}


def _format_command_template(template: str, **values: object) -> str:
	try:
		return template.format(**values)
	except Exception:
		return template