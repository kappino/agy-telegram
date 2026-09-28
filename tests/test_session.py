"""
Unit tests for session management and history lookup.
"""

import unittest
from agy_telegram.core.session import SessionManager


class TestSessionManager(unittest.TestCase):
    def test_session_manager_init(self):
        sm = SessionManager()
        self.assertIsNone(sm.get_active_session())
        sm.set_active_session("test-conv-123")
        self.assertEqual(sm.get_active_session(), "test-conv-123")

    def test_list_recent_sessions(self):
        sm = SessionManager()
        sessions = sm.list_recent_sessions(limit=5)
        self.assertIsInstance(sessions, list)


if __name__ == "__main__":
    unittest.main()
