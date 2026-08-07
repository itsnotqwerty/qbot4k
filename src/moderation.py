from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .intelligence.powerusers import is_egregious_content
from .models import NormalizedMessage


_LINK_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


@dataclass(frozen=True)
class ModerationRule:
	id: int
	name: str
	rule_type: str
	pattern: str
	severity: str
	auto_enforce_action: str | None
	enabled: bool


@dataclass(frozen=True)
class ModerationFinding:
	rule_id: int
	rule_name: str
	rule_type: str
	severity: str
	reason_code: str
	auto_enforce_action: str | None = None


def evaluate_egregious_content(
	message: NormalizedMessage,
	rule: ModerationRule,
) -> list[ModerationFinding]:
	"""Return an auto-enforceable finding when the message contains a slur or ToS violation."""
	if message.is_moderator:
		return []
	if not is_egregious_content(message.content_raw):
		return []
	return [_build_finding(rule)]


def evaluate_message_moderation(
	message: NormalizedMessage,
	rules: Iterable[ModerationRule],
) -> list[ModerationFinding]:
	findings: list[ModerationFinding] = []
	for rule in rules:
		if not rule.enabled:
			continue

		if rule.rule_type == "exact_term":
			if _matches_exact_term(message.content_raw, rule.pattern):
				findings.append(_build_finding(rule))
			continue

		if rule.rule_type == "banned_phrase":
			if _matches_phrase(message.content_raw, rule.pattern):
				findings.append(_build_finding(rule))
			continue

		if rule.rule_type == "link_restriction":
			if _contains_link(message.content_raw) and not message.is_moderator:
				findings.append(_build_finding(rule))
			continue

		if rule.rule_type == "duplicate_message":
			if _matches_duplicate(message, rule.pattern):
				findings.append(_build_finding(rule))
			continue

	return findings


def _build_finding(rule: ModerationRule) -> ModerationFinding:
	return ModerationFinding(
		rule_id=rule.id,
		rule_name=rule.name,
		rule_type=rule.rule_type,
		severity=rule.severity,
		reason_code=rule.rule_type,
		auto_enforce_action=rule.auto_enforce_action,
	)


def _matches_exact_term(content: str, pattern: str) -> bool:
	normalized_pattern = pattern.strip().casefold()
	if not normalized_pattern:
		return False

	content_casefolded = content.casefold()
	return re.search(rf"(?<!\w){re.escape(normalized_pattern)}(?!\w)", content_casefolded) is not None


def _matches_phrase(content: str, pattern: str) -> bool:
	regex = pattern.strip()
	if not regex:
		return False

	try:
		compiled = re.compile(regex, re.IGNORECASE)
	except re.error:
		return False
	return compiled.search(content) is not None


def _contains_link(content: str) -> bool:
	return _LINK_PATTERN.search(content) is not None


def _matches_duplicate(message: NormalizedMessage, pattern: str) -> bool:
	if pattern.strip().casefold() != "same_user_same_content":
		return False
	previous_content = message.metadata.get("previous_normalized_content")
	if not isinstance(previous_content, str):
		return False
	return previous_content.casefold() == message.content_normalized.casefold()
