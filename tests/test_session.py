"""
Unit tests for session management and history lookup.
"""

from agy_telegram.core.session import SessionManager

def test_session_manager_init():
    sm = SessionManager()
    assert sm.get_active_session() is None
    sm.set_active_session("test-conv-123")
    assert sm.get_active_session() == "test-conv-123"

def test_list_recent_sessions():
    sm = SessionManager()
    sessions = sm.list_recent_sessions(limit=5)
    assert isinstance(sessions, list)
