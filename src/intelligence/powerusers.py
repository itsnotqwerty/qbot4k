from __future__ import annotations

from difflib import SequenceMatcher
import re
import sqlite3
from dataclasses import dataclass


SOCIAL_SCORE_MIN = 350
SOCIAL_SCORE_MAX = 900
SOCIAL_SCORE_DEFAULT = 500
POWERUSER_THRESHOLD = 700

_POSITIVE_TERMS = {
	"thanks",
	"thank you",
	"great",
	"awesome",
	"nice",
	"love",
	"good job",
	"well done",
}

# Slurs and explicit ToS violations that warrant automatic moderation in addition to a
# reputation penalty. All terms here are also present in _VERY_NEGATIVE_TERMS so that
# reputation scoring requires no separate check.
_EGREGIOUS_TERMS = {
	"alligatorbait", "gatorbait",
	"beaner", "bohunk",
	"boong", "boonga", "boonie", "bountybar",
	"cameljockey",
	"chink", "chinky",
	"coon", "coondog",
	"dago", "darkie", "darky", "datnigga",
	"faggot", "fagot", "fag",
	"gook", "greaseball",
	"hebe", "heeb", "honkey", "honky", "hymie",
	"ikey",
	"jap",
	"jiga", "jigaboo", "jigg", "jigga", "jiggabo", "jigger", "jijjiboo",
	"junglebunny",
	"kaffer", "kaffir", "kaffre", "kafir", "kanake", "kigger",
	"kike", "kyke", "kkk",
	"lynch",
	"macaca", "mgger", "mggor", "mooncricket", "mulatto", "munt",
	"nazi",
	"negro", "negroes", "negroid", "negro's",
	"nig", "nigg", "nigga", "niggah", "niggaracci", "niggaz",
	"nigger", "niggerhead", "niggerhole", "niggers", "nigger's",
	"niggor", "niggur", "niglet", "nignog", "nigr", "nigra", "nigre",
	"nlgger", "nlggor",
	"nip",
	"paki", "palesimian",
	"pickaninny", "picaninny", "piccaninny",
	"piker", "pikey", "piky",
	"polack", "porchmonkey",
	"raghead",
	"rape", "raped", "raper", "rapist",
	"roundeye",
	"sandnigger", "slant", "slanteye", "snownigger",
	"spaghettibender", "spaghettinigger",
	"spic", "spick", "spig", "spigotty", "spik",
	"swastika",
	"tarbaby", "timbernigger", "towelhead",
	"wetback", "whigger", "wigger",
	"wog", "wop",
	"yellowman", "zigabo", "zipperhead",
}

_VERY_NEGATIVE_TERMS = {
	"abuse", "assassin", "assassinate", "assassination", "assault",
	"asshole", "assholes", "asswipe", "bastard", "beaner", "bitch", "bitches",
	"bohunk", "boong", "boonga", "boonie", "bullshit", "cameljockey", "chink", "chinky",
	"clogwog", "coon", "coondog", "coolie", "cooly", "cunt", "dago", "darkie", "darky",
	"datnigga", "fag", "faggot", "fagot", "fuck", "fucked", "fucker", "fuckers", "fucking",
	"gaymuthafuckinwhore", "gook", "greaseball", "gyp", "gypo", "gypp", "gyppie", "gyppo", "gyppy",
	"hebe", "heeb", "honkey", "honky", "hymie", "ikey", "jap", "japcrap", "jiga", "jigaboo",
	"jigg", "jigga", "jiggabo", "jigger", "jijjiboo", "junglebunny", "kaffer", "kaffir", "kaffre",
	"kafir", "kanake", "kigger", "kike", "kkk", "koon", "kraut", "kyke", "lynch", "macaca",
	"mgger", "mggor", "mooncricket", "mulatto", "munt", "nazi", "negro", "negroes", "negroid",
	"negro's", "nig", "nigg", "nigga", "niggah", "niggaracci", "niggaz", "nigger", "niggerhead",
	"niggerhole", "niggers", "nigger's", "niggor", "niggur", "niglet", "nignog", "nigr", "nigra",
	"nigre", "nlgger", "nlggor", "nip", "paki", "palesimian", "pickaninny", "picaninny", "piccaninny",
	"piker", "pikey", "piky", "polack", "porchmonkey", "raghead", "rape", "raped", "raper", "rapist",
	"retard", "retarded", "roundeye", "sandnigger", "slant", "slanteye", "snownigger", "spaghettibender",
	"spaghettinigger", "spic", "spick", "spig", "spigotty", "spik", "swastika", "tarbaby",
	"timbernigger", "towelhead", "wetback", "whigger", "wigger", "wog", "wop", "yellowman", "zigabo",
	"zipperhead",
}

_TOKEN_PATTERN = re.compile(r"[a-z0-9']+")
_MIN_FUZZY_TERM_LENGTH = 5
_MAX_FUZZY_LENGTH_DELTA = 1
_FUZZY_MIN_SIMILARITY = 0.92

_VERY_NEGATIVE_SINGLE_TERMS = frozenset(
	term
	for term in _VERY_NEGATIVE_TERMS
	if " " not in term
)
_VERY_NEGATIVE_PHRASE_TERMS = tuple(
	term for term in sorted(_VERY_NEGATIVE_TERMS) if " " in term
)

_POSITIVE_SINGLE_TERMS = frozenset(
	term for term in _POSITIVE_TERMS if " " not in term
)
_POSITIVE_PHRASE_TERMS = tuple(
	term for term in sorted(_POSITIVE_TERMS) if " " in term
)

_FUZZY_NEGATIVE_BY_INITIAL: dict[str, tuple[str, ...]] = {}
for _term in sorted(_VERY_NEGATIVE_SINGLE_TERMS):
	if len(_term) < _MIN_FUZZY_TERM_LENGTH:
		continue
	if not _term.isalpha():
		continue
	if _term[0] not in _FUZZY_NEGATIVE_BY_INITIAL:
		_FUZZY_NEGATIVE_BY_INITIAL[_term[0]] = ()
	_FUZZY_NEGATIVE_BY_INITIAL[_term[0]] = (
		*_FUZZY_NEGATIVE_BY_INITIAL[_term[0]],
		_term,
	)


def _contains_bounded_phrase(text: str, phrase: str) -> bool:
	return re.search(rf"(?<!\\w){re.escape(phrase)}(?!\\w)", text) is not None


def _is_single_edit_apart_or_equal(first: str, second: str) -> bool:
	if first == second:
		return True
	if abs(len(first) - len(second)) > 1:
		return False

	if len(first) > len(second):
		first, second = second, first

	first_index = 0
	second_index = 0
	edits = 0
	while first_index < len(first) and second_index < len(second):
		if first[first_index] == second[second_index]:
			first_index += 1
			second_index += 1
			continue
		edits += 1
		if edits > 1:
			return False
		if len(first) == len(second):
			first_index += 1
			second_index += 1
		else:
			second_index += 1

	if first_index < len(first) or second_index < len(second):
		edits += 1

	return edits <= 1


def _is_fuzzy_negative_token_match(token: str) -> bool:
	if len(token) < _MIN_FUZZY_TERM_LENGTH:
		return False
	if not token.isalpha():
		return False

	for candidate in _FUZZY_NEGATIVE_BY_INITIAL.get(token[0], ()):
		if abs(len(candidate) - len(token)) > _MAX_FUZZY_LENGTH_DELTA:
			continue
		if token[-1] != candidate[-1]:
			continue
		if _is_single_edit_apart_or_equal(token, candidate):
			return True
		if SequenceMatcher(None, token, candidate).ratio() >= _FUZZY_MIN_SIMILARITY:
			return True

	return False


def _contains_very_negative_content(normalized: str) -> bool:
	tokens = _TOKEN_PATTERN.findall(normalized)
	token_set = set(tokens)
	if token_set & _VERY_NEGATIVE_SINGLE_TERMS:
		return True

	for phrase in _VERY_NEGATIVE_PHRASE_TERMS:
		if _contains_bounded_phrase(normalized, phrase):
			return True

	for token in tokens:
		if _is_fuzzy_negative_token_match(token):
			return True

	return False


def _contains_positive_signal(normalized: str) -> bool:
	tokens = _TOKEN_PATTERN.findall(normalized)
	if set(tokens) & _POSITIVE_SINGLE_TERMS:
		return True

	for phrase in _POSITIVE_PHRASE_TERMS:
		if _contains_bounded_phrase(normalized, phrase):
			return True

	return False


def clamp_social_score(score: int) -> int:
	return max(SOCIAL_SCORE_MIN, min(SOCIAL_SCORE_MAX, score))


def is_poweruser_score(score: int) -> bool:
	return clamp_social_score(score) >= POWERUSER_THRESHOLD


def average_social_scores(first_score: int, second_score: int) -> int:
	return clamp_social_score(int(round((first_score + second_score) / 2)))


def default_social_score_for_name(display_name: str) -> int:
	return SOCIAL_SCORE_DEFAULT


def enforced_social_score_for_name(display_name: str, proposed_score: int) -> int:
	return clamp_social_score(proposed_score)


def record_reputation_evidence(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	delta: int,
	reason_code: str,
	source_type: str,
	source_id: int | None = None,
) -> int:
	"""Record classified evidence without directly mutating the materialized score."""
	if connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
		raise ValueError("canonical user not found")
	cursor = connection.execute(
		"""
		INSERT INTO reputation_events (user_id, source_type, source_id, delta, reason_code)
		VALUES (?, ?, ?, ?, ?)
		""",
		(user_id, source_type, source_id, delta, reason_code),
	)
	return int(cursor.lastrowid)


def score_delta_for_message(content_raw: str) -> tuple[int, str] | None:
	normalized = content_raw.casefold().strip()
	if not normalized:
		return None
	if normalized.startswith("!") or normalized.startswith("/"):
		return None

	if _contains_very_negative_content(normalized):
		return (-10, "very_negative_content")

	if _contains_positive_signal(normalized):
		return (1, "positive_message")

	return (1, "message_sent")


def is_egregious_content(content: str) -> bool:
	normalized = content.casefold().strip()
	return any(
		re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized) is not None
		for term in _EGREGIOUS_TERMS
	)


def score_delta_for_moderation(
	*,
	severity: str,
	action_type: str | None = None,
	reason_code: str | None = None,
) -> tuple[int, str]:
	reason_key = (reason_code or "").casefold().strip()
	if reason_key == "egregious_term":
		return (-20, "moderation_penalty")

	severity_key = severity.casefold().strip()
	base_delta = {
		"low": -20,
		"medium": -35,
		"high": -55,
	}.get(severity_key, -25)
	if action_type:
		base_delta -= 15
	return (base_delta, "moderation_penalty")


@dataclass(frozen=True)
class ReputationUpdate:
	user_id: int
	delta: int
	current_score: int
	candidate_flag: bool
	reason_code: str


def apply_reputation_event(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	delta: int,
	reason_code: str,
	source_type: str,
	source_id: int | None = None,
	candidate_threshold: int = POWERUSER_THRESHOLD,
	minimum_score: int = SOCIAL_SCORE_MIN,
	maximum_score: int = SOCIAL_SCORE_MAX,
) -> ReputationUpdate:
	user = connection.execute(
		"""
		SELECT id, primary_display_name, current_reputation_score
		FROM users
		WHERE id = ?
		""",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	current_score = int(user[2])
	updated_score = max(minimum_score, min(maximum_score, current_score + delta))
	updated_score = enforced_social_score_for_name(str(user[1]), updated_score)
	candidate_flag = updated_score >= candidate_threshold

	with connection:
		connection.execute(
			"""
			INSERT INTO reputation_events (
				user_id,
				source_type,
				source_id,
				delta,
				reason_code
			) VALUES (?, ?, ?, ?, ?)
			""",
			(user_id, source_type, source_id, delta, reason_code),
		)
		connection.execute(
			"""
			UPDATE users
			SET current_reputation_score = ?,
			    candidate_flag = ?,
			    updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(updated_score, int(candidate_flag), user_id),
		)
		if current_score > minimum_score and updated_score <= minimum_score:
			enforce_score_floor_ban(connection, user_id=user_id, floor_score=minimum_score)
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type,
				actor_id,
				action_type,
				entity_type,
				entity_id,
				payload_json
			) VALUES (
				'system',
				NULL,
				'user_reputation_update',
				'user',
				?,
				json_object('delta', ?, 'reason_code', ?, 'source_type', ?, 'source_id', ?)
			)
			""",
			(user_id, delta, reason_code, source_type, source_id),
		)

	return ReputationUpdate(
		user_id=user_id,
		delta=delta,
		current_score=updated_score,
		candidate_flag=candidate_flag,
		reason_code=reason_code,
	)


def enforce_score_floor_ban(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	floor_score: int,
) -> None:
	platform_accounts = connection.execute(
		"""
		SELECT id, platform, username
		FROM platform_accounts
		WHERE user_id = ?
		ORDER BY id
		""",
		(user_id,),
	).fetchall()
	deleted_message_count = connection.execute(
		"""
		SELECT COUNT(*)
		FROM messages
		INNER JOIN platform_accounts ON platform_accounts.id = messages.platform_account_id
		WHERE platform_accounts.user_id = ?
		""",
		(user_id,),
	).fetchone()[0]

	for account in platform_accounts:
		connection.execute(
			"""
			INSERT INTO moderation_actions (
				platform,
				message_id,
				target_platform_account_id,
				action_type,
				actor_type,
				actor_id,
				reason,
				status
			) VALUES (?, NULL, ?, 'ban', 'system', NULL, ?, 'completed')
			""",
			(
				str(account[1]),
				int(account[0]),
				f"social_score_floor_reached:{floor_score}",
			),
		)

	connection.execute(
		"""
		DELETE FROM messages
		WHERE platform_account_id IN (
			SELECT id FROM platform_accounts WHERE user_id = ?
		)
		""",
		(user_id,),
	)
	connection.execute(
		"""
		INSERT INTO audit_log (
			actor_type,
			actor_id,
			action_type,
			entity_type,
			entity_id,
			payload_json
		) VALUES (
			'system',
			NULL,
			'user_score_floor_enforced',
			'user',
			?,
			json_object(
				'floor_score', ?,
				'deleted_message_count', ?,
				'banned_account_count', ?
			)
		)
		""",
		(user_id, floor_score, int(deleted_message_count), len(platform_accounts)),
	)


def get_reputation_history(
	connection: sqlite3.Connection,
	user_id: int,
) -> list[sqlite3.Row]:
	rows = connection.execute(
		"""
		SELECT id, source_type, source_id, delta, reason_code, created_at
		FROM reputation_events
		WHERE user_id = ?
		ORDER BY created_at, id
		""",
		(user_id,),
	).fetchall()
	return list(rows)
