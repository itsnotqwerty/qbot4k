from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.db import connect_database, initialize_database, ensure_platform_account
from src.intelligence.powerusers import apply_reputation_event, get_reputation_history
from src.intelligence.userprofiles import (
	create_canonical_user,
	add_user_note,
	get_canonical_user_profile,
	list_user_notes,
	link_platform_account,
	unlink_platform_account,
)


class IdentityTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tempdir = TemporaryDirectory()
		self.database_path = Path(self.tempdir.name) / "identity.sqlite3"

	def tearDown(self) -> None:
		self.tempdir.cleanup()

	def test_can_link_and_unlink_platform_accounts(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				username="sam_twitch",
				guild_or_channel_context="its_not_qwerty",
			)
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				username="sam_discord",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				user_id=user_id,
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				user_id=user_id,
			)
			profile = get_canonical_user_profile(connection, user_id)
			unlink_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
			)
			linked_account_rows = connection.execute(
				"SELECT platform, user_id FROM platform_accounts ORDER BY platform"
			).fetchall()
			audit_count = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
		finally:
			connection.close()

		self.assertEqual(profile.primary_display_name, "sam")
		self.assertEqual(len(profile.linked_accounts), 2)
		self.assertEqual(profile.linked_accounts[0].platform, "discord")
		self.assertEqual(profile.linked_accounts[1].platform, "twitch")
		self.assertEqual(linked_account_rows[0][0], "discord")
		self.assertIsNone(linked_account_rows[0][1])
		self.assertEqual(linked_account_rows[1][0], "twitch")
		self.assertEqual(linked_account_rows[1][1], user_id)
		self.assertGreaterEqual(audit_count, 3)

	def test_reputation_events_update_score_and_candidate_flag(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")

			first_update = apply_reputation_event(
				connection,
				user_id=user_id,
				delta=220,
				reason_code="good_behavior",
				source_type="manual_adjustment",
			)
			second_update = apply_reputation_event(
				connection,
				user_id=user_id,
				delta=-500,
				reason_code="rule_violation",
				source_type="moderation_action",
				source_id=123,
			)

			user_row = connection.execute(
				"SELECT current_reputation_score, candidate_flag FROM users WHERE id = ?",
				(user_id,),
			).fetchone()
			history_rows = get_reputation_history(connection, user_id)
			audit_count = connection.execute(
				"SELECT COUNT(*) FROM audit_log WHERE entity_type = 'user' AND entity_id = ?",
				(user_id,),
			).fetchone()[0]
		finally:
			connection.close()

		self.assertEqual(first_update.current_score, 720)
		self.assertTrue(first_update.candidate_flag)
		self.assertEqual(second_update.current_score, 350)
		self.assertFalse(second_update.candidate_flag)
		self.assertEqual(user_row[0], 350)
		self.assertEqual(user_row[1], 0)
		self.assertEqual(len(history_rows), 2)
		self.assertEqual(history_rows[0][3], 220)
		self.assertEqual(history_rows[1][3], -500)
		self.assertEqual(audit_count, 3)

	def test_reaching_score_floor_bans_linked_accounts_and_deletes_messages(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam", current_reputation_score=400)
			twitch_account_id = ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				username="sam_twitch",
				guild_or_channel_context="its_not_qwerty",
			)
			discord_account_id = ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				username="sam_discord",
				guild_or_channel_context="guild-1",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-1",
				user_id=user_id,
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-1",
				user_id=user_id,
			)
			with connection:
				connection.execute(
					"""
					INSERT INTO messages (
						platform,
						platform_message_id,
						platform_account_id,
						channel_id,
						content_raw,
						content_normalized,
						sent_at
					) VALUES ('twitch', 'msg-1', ?, 'channel-1', 'hello', 'hello', '2026-08-01T00:00:00+00:00')
					""",
					(twitch_account_id,),
				)
				connection.execute(
					"""
					INSERT INTO messages (
						platform,
						platform_message_id,
						platform_account_id,
						channel_id,
						content_raw,
						content_normalized,
						sent_at
					) VALUES ('discord', 'msg-2', ?, 'channel-2', 'hello', 'hello', '2026-08-01T00:00:00+00:00')
					""",
					(discord_account_id,),
				)

			update = apply_reputation_event(
				connection,
				user_id=user_id,
				delta=-50,
				reason_code="rule_violation",
				source_type="moderation_action",
			)

			message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
			actions = connection.execute(
				"SELECT platform, action_type, status, reason FROM moderation_actions ORDER BY id"
			).fetchall()
			audit_row = connection.execute(
				"SELECT action_type, payload_json FROM audit_log WHERE entity_type = 'user' AND entity_id = ? ORDER BY id DESC LIMIT 1",
				(user_id,),
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(update.current_score, 350)
		self.assertFalse(update.candidate_flag)
		self.assertEqual(message_count, 0)
		self.assertEqual(len(actions), 2)
		self.assertEqual({row[0] for row in actions}, {"discord", "twitch"})
		self.assertTrue(all(row[1] == "ban" for row in actions))
		self.assertTrue(all(row[2] == "completed" for row in actions))
		self.assertTrue(all(row[3] == "social_score_floor_reached:350" for row in actions))
		self.assertEqual(audit_row[0], "user_reputation_update")

	def test_linking_account_recalculates_from_combined_evidence_without_averaging(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			target_user_id = create_canonical_user(
				connection,
				primary_display_name="target",
				current_reputation_score=800,
			)
			source_user_id = create_canonical_user(
				connection,
				primary_display_name="source",
				current_reputation_score=400,
			)
			ensure_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-2",
				username="viewer_two",
				guild_or_channel_context="guild-2",
			)
			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-2",
				user_id=source_user_id,
			)
			ensure_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-2",
				username="viewer_two_twitch",
				guild_or_channel_context="viewer_two",
			)
			link_platform_account(
				connection,
				platform="twitch",
				platform_user_id="twitch-user-2",
				user_id=source_user_id,
			)

			link_platform_account(
				connection,
				platform="discord",
				platform_user_id="discord-user-2",
				user_id=target_user_id,
			)

			target_row = connection.execute(
				"SELECT current_reputation_score, candidate_flag FROM users WHERE id = ?",
				(target_user_id,),
			).fetchone()
			reputation_row = connection.execute(
				"SELECT source_type, reason_code FROM reputation_events WHERE user_id = ? ORDER BY id DESC LIMIT 1",
				(target_user_id,),
			).fetchone()
			merge_event_count = int(connection.execute(
				"SELECT COUNT(*) FROM reputation_events WHERE source_type = 'account_link_merge'"
			).fetchone()[0])
			source_user_count = int(
				connection.execute("SELECT COUNT(*) FROM users WHERE id = ?", (source_user_id,)).fetchone()[0]
			)
			merged_account_owners = {
				int(row[0])
				for row in connection.execute(
					"SELECT user_id FROM platform_accounts WHERE platform_user_id IN ('discord-user-2', 'twitch-user-2')"
				).fetchall()
			}
		finally:
			connection.close()

		self.assertEqual(target_row[0], 800)
		self.assertEqual(target_row[1], 1)
		self.assertEqual(reputation_row[0], "initial_calibration")
		self.assertEqual(reputation_row[1], "initial_score_calibration")
		self.assertEqual(merge_event_count, 0)
		self.assertEqual(source_user_count, 0)
		self.assertEqual(merged_account_owners, {target_user_id})

	def test_operator_notes_are_stored_and_listed(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			user_id = create_canonical_user(connection, primary_display_name="sam")
			note_id = add_user_note(
				connection,
				user_id=user_id,
				operator_id=42,
				body="Needs follow-up",
			)
			notes = list_user_notes(connection, user_id)
			profile = get_canonical_user_profile(connection, user_id)
			audit_row = connection.execute(
				"SELECT action_type, entity_type FROM audit_log WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
				(user_id,),
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(note_id, notes[0][0])
		self.assertEqual(notes[0][1], 42)
		self.assertEqual(notes[0][2], "Needs follow-up")
		self.assertEqual(len(profile.notes), 1)
		self.assertEqual(profile.notes[0][2], "Needs follow-up")
		self.assertEqual(audit_row[0], "user_note_create")
		self.assertEqual(audit_row[1], "user")

	def test_usernames_do_not_bypass_evidence_based_scoring(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			apollyon_id = create_canonical_user(connection, primary_display_name="apollyon")
			qwerty_id = create_canonical_user(connection, primary_display_name="its_not_qwerty")
			rows = connection.execute(
				"SELECT id, current_reputation_score, candidate_flag FROM users WHERE id IN (?, ?) ORDER BY id",
				(apollyon_id, qwerty_id),
			).fetchall()
		finally:
			connection.close()

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0][1], 500)
		self.assertEqual(rows[0][2], 0)
		self.assertEqual(rows[1][1], 500)
		self.assertEqual(rows[1][2], 0)

	def test_initialization_does_not_pin_privileged_usernames(self) -> None:
		connection = connect_database(self.database_path)
		try:
			initialize_database(connection)
			with connection:
				connection.execute(
					"""
					INSERT INTO users (primary_display_name, current_reputation_score, candidate_flag)
					VALUES ('apollyon', 410, 0)
					"""
				)
			row = connection.execute(
				"SELECT id FROM users WHERE primary_display_name = 'apollyon' ORDER BY id DESC LIMIT 1"
			).fetchone()
			assert row is not None
			user_id = int(row[0])

			initialize_database(connection)
			apply_reputation_event(
				connection,
				user_id=user_id,
				delta=-500,
				reason_code="rule_violation",
				source_type="moderation_action",
			)

			user_row = connection.execute(
				"SELECT current_reputation_score, candidate_flag FROM users WHERE id = ?",
				(user_id,),
			).fetchone()
		finally:
			connection.close()

		self.assertEqual(user_row[0], 350)
		self.assertEqual(user_row[1], 0)
