from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class OverviewSnapshot:
	messages_total: int
	open_reviews: int
	pending_actions: int
	derived_signals: int
	top_channels: tuple[tuple[str, int], ...]
	top_platforms: tuple[tuple[str, int], ...]


def load_overview_snapshot(
	connection: sqlite3.Connection,
	*,
	community_id: int,
) -> OverviewSnapshot:
	messages_total = connection.execute(
		"SELECT COUNT(*) FROM messages WHERE community_id = ?", (community_id,)
	).fetchone()[0]
	open_reviews = connection.execute(
		"""SELECT COUNT(*) FROM review_queue
		   INNER JOIN messages ON messages.id = review_queue.message_id
		   WHERE review_queue.status = 'open' AND messages.community_id = ?""",
		(community_id,),
	).fetchone()[0]
	pending_actions = connection.execute(
		"""SELECT COUNT(*) FROM moderation_actions
		   WHERE status = 'pending' AND community_id = ?""",
		(community_id,),
	).fetchone()[0]
	derived_signals = connection.execute(
		"""SELECT COUNT(*) FROM derived_signals
		   WHERE EXISTS (
		       SELECT 1 FROM messages
		       WHERE messages.user_id = derived_signals.user_id
		         AND messages.community_id = ?
		   )""",
		(community_id,),
	).fetchone()[0]
	top_channels = connection.execute(
		"""
		SELECT
			messages.platform,
			messages.channel_id,
			discord_channels.channel_name,
			COUNT(*) AS count
		FROM messages
		LEFT JOIN discord_channels ON (
			discord_channels.channel_id = messages.channel_id
			AND messages.platform = 'discord'
		)
		WHERE messages.community_id = ?
		GROUP BY messages.platform, messages.channel_id, discord_channels.channel_name
		ORDER BY count DESC, messages.channel_id
		LIMIT 5
		""",
		(community_id,),
	).fetchall()
	top_platforms = connection.execute(
		"""
		SELECT platform, COUNT(*) AS count
		FROM messages
		WHERE community_id = ?
		GROUP BY platform
		ORDER BY count DESC, platform
		""",
		(community_id,),
	).fetchall()
	return OverviewSnapshot(
		messages_total=int(messages_total),
		open_reviews=int(open_reviews),
		pending_actions=int(pending_actions),
		derived_signals=int(derived_signals),
		top_channels=tuple(
			(
				f"#{str(row[2])}" if str(row[0]) == "discord" and row[2] is not None else str(row[1]),
				int(row[3]),
			)
			for row in top_channels
		),
		top_platforms=tuple((str(row[0]), int(row[1])) for row in top_platforms),
	)
