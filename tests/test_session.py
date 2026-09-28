"""
Unit tests for session management and history lookup.
"""

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

    def test_list_recent_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = SessionManager(data_dir=tmpdir)
            sessions = sm.list_recent_sessions(limit=5)
            self.assertIsInstance(sessions, list)
            self.assertEqual(len(sessions), 0)


if __name__ == "__main__":
    unittest.main()

