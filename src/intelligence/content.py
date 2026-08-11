from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse


ANALYZER_VERSION = 3
_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MENTION = re.compile(r"<@!?(\d+)>|(?<!\w)@([\w.-]+)")
_HASHTAG = re.compile(r"(?<!\w)#([\w-]+)")
_PROPER_NAME = re.compile(r"\b(?:[A-Z][a-z]{2,})(?:\s+[A-Z][a-z]{2,}){0,2}\b")

_LANGUAGE_WORDS = {
    "en": {"the", "and", "you", "this", "that", "with", "for", "are", "not", "have"},
    "es": {"el", "la", "los", "las", "que", "para", "con", "una", "por", "como"},
    "fr": {"le", "la", "les", "des", "que", "pour", "avec", "une", "pas", "est"},
    "de": {"der", "die", "das", "und", "ist", "mit", "für", "nicht", "ein", "eine"},
    "pt": {"o", "a", "os", "as", "que", "para", "com", "uma", "não", "por"},
}
_POSITIVE = {"good", "great", "love", "helpful", "excellent", "thanks", "safe", "happy", "win"}
_NEGATIVE = {"bad", "hate", "awful", "angry", "fraud", "scam", "danger", "hurt", "kill", "threat"}
_NEGATIONS = {"not", "never", "no", "isn't", "wasn't", "don't", "didn't"}


@dataclass(frozen=True)
class ContentUnderstanding:
    language_code: str
    language_confidence: float
    sentiment_label: str
    sentiment_score: float
    intent_label: str
    intent_confidence: float
    threat_level: str
    threat_score: float
    indicators: tuple[str, ...]
    entities: tuple[tuple[str, str, str, float, int | None, int | None], ...]
    conversation: dict[str, object]


def understand_content(text: str, attributes: dict[str, object] | None = None) -> ContentUnderstanding:
    attributes = attributes or {}
    words = [match.group(0).casefold() for match in _WORD.finditer(text)]
    language, language_confidence = _detect_language(words, text)

    sentiment_total = 0
    for index, word in enumerate(words):
        polarity = 1 if word in _POSITIVE else -1 if word in _NEGATIVE else 0
        if polarity and any(token in _NEGATIONS for token in words[max(0, index - 3):index]):
            polarity *= -1
        sentiment_total += polarity
    sentiment_score = max(-1.0, min(1.0, sentiment_total / max(2.0, len(words) ** 0.5)))
    sentiment = "positive" if sentiment_score >= 0.2 else "negative" if sentiment_score <= -0.2 else "neutral"

    lowered = text.casefold()
    indicators: list[str] = []
    threat_score = 0.0
    if re.search(r"\b(?:i|we)\s+(?:will|gonna|am going to)\s+(?:kill|hurt|attack|shoot|bomb|doxx)\b", lowered):
        threat_score = 0.95
        indicators.append("direct_future_threat")
    elif re.search(r"\b(?:kill|shoot|bomb|attack|doxx|swat)\b", lowered):
        threat_score = 0.55
        indicators.append("threat_term_in_context")
    if _IP.search(text) or _EMAIL.search(text):
        if any(term in lowered for term in ("address", "home", "leak", "dox")):
            threat_score = max(threat_score, 0.75)
            indicators.append("possible_personal_data_exposure")
    threat_level = "critical" if threat_score >= 0.9 else "high" if threat_score >= 0.7 else "medium" if threat_score >= 0.4 else "none"

    if threat_score >= 0.7:
        intent, intent_confidence = "threat", threat_score
    elif text.rstrip().endswith("?"):
        intent, intent_confidence = "question", 0.85
    elif re.search(r"\b(?:please|could you|can you|need you to)\b", lowered):
        intent, intent_confidence = "request", 0.78
    elif re.search(r"\b(?:join|meet|coordinate|everyone|at \d{1,2}(?::\d{2})?)\b", lowered):
        intent, intent_confidence = "coordination", 0.66
    elif re.search(r"\b(?:buy|sale|discount|promo|subscribe)\b", lowered):
        intent, intent_confidence = "promotion", 0.68
    elif sentiment == "negative":
        intent, intent_confidence = "complaint", 0.58
    else:
        intent, intent_confidence = "statement", 0.52

    entities: list[tuple[str, str, str, float, int | None, int | None]] = []
    for match in _URL.finditer(text):
        value = match.group(0).rstrip(".,;!?)")
        entities.append(("url", value, value.casefold(), 0.99, match.start(), match.start() + len(value)))
        domain = (urlparse(value).hostname or "").casefold().removeprefix("www.")
        if domain:
            entities.append(("domain", domain, domain, 0.99, match.start(), match.end()))
    for entity_type, pattern in (("email", _EMAIL), ("ip_address", _IP), ("hashtag", _HASHTAG), ("mention", _MENTION)):
        for match in pattern.finditer(text):
            value = next((group for group in match.groups() if group), match.group(0))
            entities.append((entity_type, value, value.casefold(), 0.92, match.start(), match.end()))
    for match in _PROPER_NAME.finditer(text):
        value = match.group(0)
        if value.casefold() not in {"http", "https"}:
            entities.append(("named_entity", value, value.casefold(), 0.62, match.start(), match.end()))

    reply_id = attributes.get("referenced_message_id") or attributes.get("message_reference_id")
    conversation = {
        "is_question": text.rstrip().endswith("?"),
        "reply_to": str(reply_id) if reply_id else None,
        "thread_id": attributes.get("thread_id"),
        "mentioned_accounts": [item[1] for item in entities if item[0] == "mention"],
    }
    return ContentUnderstanding(
        language, language_confidence, sentiment, round(sentiment_score, 4), intent,
        intent_confidence, threat_level, threat_score, tuple(indicators), tuple(entities), conversation,
    )


def analyze_observation_content(connection: sqlite3.Connection, observation_id: int) -> ContentUnderstanding:
    row = connection.execute(
        "SELECT text_raw, attributes_json FROM observations WHERE id = ?", (observation_id,)
    ).fetchone()
    if row is None:
        raise ValueError("observation not found")
    attributes = json.loads(str(row[1] or "{}"))
    result = understand_content(str(row[0] or ""), attributes if isinstance(attributes, dict) else {})
    connection.execute(
        """
        INSERT INTO content_analysis(
            observation_id, analyzer_version, language_code, language_confidence,
            sentiment_label, sentiment_score, intent_label, intent_confidence,
            threat_level, threat_score, conversation_json, indicators_json, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(observation_id) DO UPDATE SET
            analyzer_version=excluded.analyzer_version, language_code=excluded.language_code,
            language_confidence=excluded.language_confidence, sentiment_label=excluded.sentiment_label,
            sentiment_score=excluded.sentiment_score, intent_label=excluded.intent_label,
            intent_confidence=excluded.intent_confidence, threat_level=excluded.threat_level,
            threat_score=excluded.threat_score, conversation_json=excluded.conversation_json,
            indicators_json=excluded.indicators_json, analyzed_at=CURRENT_TIMESTAMP
        """,
        (observation_id, ANALYZER_VERSION, result.language_code, result.language_confidence,
         result.sentiment_label, result.sentiment_score, result.intent_label, result.intent_confidence,
         result.threat_level, result.threat_score, json.dumps(result.conversation, sort_keys=True),
         json.dumps(result.indicators)),
    )
    connection.execute("DELETE FROM content_entities WHERE observation_id = ?", (observation_id,))
    connection.executemany(
        """INSERT INTO content_entities(
            observation_id, entity_type, entity_value, normalized_value, confidence, start_offset, end_offset
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ((observation_id, *entity) for entity in result.entities),
    )
    return result


def emit_content_alert(
    connection: sqlite3.Connection,
    observation_id: int,
    result: ContentUnderstanding,
) -> int | None:
    """Turn high-confidence content findings into evidence-linked triage work."""
    if result.threat_level not in {"high", "critical"}:
        return None
    observation = connection.execute(
        """
        SELECT actor.user_id, target.user_id
        FROM observations AS o
        LEFT JOIN platform_accounts AS actor ON actor.id=o.actor_platform_account_id
        LEFT JOIN platform_accounts AS target ON target.id=o.target_platform_account_id
        WHERE o.id=?
        """,
        (observation_id,),
    ).fetchone()
    if observation is None:
        return None
    user_id = observation[0] if observation[0] is not None else observation[1]
    severity = "critical" if result.threat_level == "critical" else "high"
    cursor = connection.execute(
        """
        INSERT INTO intelligence_alerts(
            user_id, observation_id, alert_type, severity, title, summary,
            confidence, dedupe_key
        ) VALUES (?, ?, 'content_threat', ?, 'Potential Threat', ?, ?, ?)
        ON CONFLICT(dedupe_key) DO NOTHING
        """,
        (
            user_id,
            observation_id,
            severity,
            "Content analysis identified: " + ", ".join(result.indicators),
            result.threat_score,
            f"content-threat:{observation_id}:v{ANALYZER_VERSION}",
        ),
    )
    return int(cursor.lastrowid) if cursor.rowcount == 1 else None


def _detect_language(words: list[str], text: str) -> tuple[str, float]:
    if not words:
        return "und", 0.0
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru", 0.9
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh", 0.9
    scores = {code: sum(word in vocabulary for word in words) for code, vocabulary in _LANGUAGE_WORDS.items()}
    best = max(scores, key=scores.get)
    hits = scores[best]
    if not hits:
        return ("en", 0.35) if all(ord(char) < 128 for char in text) else ("und", 0.2)
    return best, min(0.98, 0.45 + hits / max(2, len(words)))
