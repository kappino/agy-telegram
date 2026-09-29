"""
Unit tests for session management, title extraction, and history lookup.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from agy_telegram.core.session import SessionManager


class TestSessionManager(unittest.TestCase):
    def test_session_manager_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = SessionManager(data_dir=tmpdir)
            self.assertIsNone(sm.get_active_session())
            sm.set_active_session("test-conv-123")
            self.assertEqual(sm.get_active_session(), "test-conv-123")

    def test_list_recent_sessions_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = SessionManager(data_dir=tmpdir)
            sessions = sm.list_recent_sessions(limit=5)
            self.assertIsInstance(sessions, list)
            self.assertEqual(len(sessions), 0)

    def test_list_recent_sessions_from_sqlite_db(self):
        """Extracts titles and timestamps from conversation_summaries.db."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conversation_summaries.db"
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE conversation_summaries (
                    conversation_id text PRIMARY KEY,
                    title text NOT NULL DEFAULT '',
                    preview text NOT NULL DEFAULT '',
                    last_modified_time datetime NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO conversation_summaries VALUES
                ('conv-1', 'Setup Docker Environment', 'docker-compose up', '2026-09-28 20:00:00.000+00:00'),
                ('conv-2', 'Fix Nginx Gateway Bug', 'nginx -t', '2026-09-28 21:30:00.000+00:00')
                """
            )
            conn.commit()
            conn.close()

            sm = SessionManager(data_dir=tmpdir)
            sessions = sm.list_recent_sessions(limit=5)

            self.assertEqual(len(sessions), 2)
            # Must be ordered by last_modified_time DESC
            self.assertEqual(sessions[0]["id"], "conv-2")
            self.assertEqual(sessions[0]["display_title"], "Fix Nginx Gateway Bug")
            self.assertEqual(sessions[0]["title"], "Fix Nginx Gateway Bug")
            self.assertEqual(sessions[0]["timestamp"], "2026-09-28 21:30")

            self.assertEqual(sessions[1]["id"], "conv-1")
            self.assertEqual(sessions[1]["display_title"], "Setup Docker Environment")

    def test_get_last_interaction_from_brain_transcript(self):
        """Extracts the last message from brain/<conv_id>/.system_generated/logs/transcript.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            conv_id = "test-conv-last-msg"
            log_dir = Path(tmpdir) / "brain" / conv_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "transcript.jsonl"

            entries = [
                {"type": "USER_INPUT", "content": "How do I restart the bot?"},
                {"type": "PLANNER_RESPONSE", "content": "Use systemctl restart agy-telegram to reload the service."},
            ]
            with open(log_file, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")

            sm = SessionManager(data_dir=tmpdir)
            last = sm.get_last_interaction(conv_id)
            self.assertIsNotNone(last)
            role, content = last
            self.assertEqual(role, "PLANNER_RESPONSE")
            self.assertIn("systemctl restart agy-telegram", content)


if __name__ == "__main__":
    unittest.main()
