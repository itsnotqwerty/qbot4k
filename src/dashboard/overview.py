from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class OverviewSnapshot:
	messages_total: int
	open_reviews: int
	pending_actions: int
	top_channels: tuple[tuple[str, int], ...]
	top_platforms: tuple[tuple[str, int], ...]


def load_overview_snapshot(connection: sqlite3.Connection) -> OverviewSnapshot:
	messages_total = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
	open_reviews = connection.execute(
		"SELECT COUNT(*) FROM review_queue WHERE status = 'open'"
	).fetchone()[0]
	pending_actions = connection.execute(
		"SELECT COUNT(*) FROM moderation_actions WHERE status = 'pending'"
	).fetchone()[0]
	top_channels = connection.execute(
		"""
		SELECT channel_id, COUNT(*) AS count
		FROM messages
		GROUP BY channel_id
		ORDER BY count DESC, channel_id
		LIMIT 5
		"""
	).fetchall()
	top_platforms = connection.execute(
		"""
		SELECT platform, COUNT(*) AS count
		FROM messages
		GROUP BY platform
		ORDER BY count DESC, platform
		"""
	).fetchall()
	return OverviewSnapshot(
		messages_total=int(messages_total),
		open_reviews=int(open_reviews),
		pending_actions=int(pending_actions),
		top_channels=tuple((str(row[0]), int(row[1])) for row in top_channels),
		top_platforms=tuple((str(row[0]), int(row[1])) for row in top_platforms),
	)
