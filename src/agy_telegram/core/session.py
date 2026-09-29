"""
Session manager interfacing with Antigravity history and conversations database.
"""

import json
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from agy_telegram.config import get_antigravity_home

class SessionManager:
    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else get_antigravity_home()
        self.history_file = self.data_dir / "history.jsonl"
        self.db_file = self.data_dir / "conversation_summaries.db"
        self.current_conversation_id: Optional[str] = None

    def set_active_session(self, conv_id: Optional[str]):
        """Sets active conversation ID (None = new clean session)."""
        self.current_conversation_id = conv_id

    def get_active_session(self) -> Optional[str]:
        return self.current_conversation_id

    def list_recent_sessions(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Lists recent sessions by extracting title, preview, and timestamp."""
        sessions = []
        seen = set()

        # 1. Query SQLite summary database if present (primary source for conversation titles)
        if self.db_file.is_file():
            try:
                conn = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT conversation_id, title, preview, last_modified_time
                    FROM conversation_summaries
                    ORDER BY last_modified_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                conn.close()

                for conv_id, title, preview, last_mod in rows:
                    if not conv_id or conv_id in seen:
                        continue
                    seen.add(conv_id)

                    dt_str = ""
                    if last_mod:
                        try:
                            # Format: '2026-09-28 21:59:13.43408046+00:00' -> '2026-09-28 21:59'
                            raw_ts = str(last_mod).split(".")[0]
                            dt_str = raw_ts[:16]
                        except Exception:
                            pass

                    clean_title = (title or "").strip()
                    clean_preview = (preview or "").strip()
                    display_text = clean_title or clean_preview or "Untitled session"

                    sessions.append({
                        "id": conv_id,
                        "title": clean_title,
                        "preview": clean_preview,
                        "display_title": display_text,
                        "timestamp": dt_str,
                    })

                if sessions:
                    return sessions[:limit]
            except Exception:
                pass

        # 2. Fallback to bounded tail parsing from history.jsonl
        if self.history_file.is_file():
            from collections import deque
            try:
                max_tail = max(50, limit * 10)
                with open(self.history_file, "r", encoding="utf-8", errors="replace") as f:
                    recent_lines = deque(f, maxlen=max_tail)

                for line in reversed(recent_lines):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        conv_id = entry.get("conversationId") or entry.get("id")
                        display = entry.get("display", "").strip()
                        ts = entry.get("timestamp")

                        if conv_id and conv_id not in seen:
                            seen.add(conv_id)
                            dt_str = ""
                            if ts:
                                try:
                                    dt = datetime.fromtimestamp(ts / 1000.0)
                                    dt_str = dt.strftime("%Y-%m-%d %H:%M")
                                except Exception:
                                    pass

                            sessions.append({
                                "id": conv_id,
                                "title": "",
                                "preview": (display[:80] + "...") if len(display) > 80 else display,
                                "display_title": (display[:80] + "...") if len(display) > 80 else (display or "Untitled session"),
                                "timestamp": dt_str,
                                "workspace": entry.get("workspace", ""),
                            })
                            if len(sessions) >= limit:
                                break
                    except Exception:
                        continue
            except Exception:
                pass

        return sessions

    def get_last_interaction(self, conv_id: str) -> Optional[Tuple[str, str]]:
        """
        Retrieves the last significant message (type, content) from the conversation transcript.
        Returns: ('PLANNER_RESPONSE' | 'USER_INPUT', content_text) or None.
        """
        if not conv_id:
            return None

        # Look in brain directory
        brain_log = self.data_dir / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        if not brain_log.is_file():
            return None

        try:
            with open(brain_log, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f if l.strip()]

            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type")
                    content = entry.get("content")
                    if msg_type in ("PLANNER_RESPONSE", "USER_INPUT") and content and str(content).strip():
                        return msg_type, str(content).strip()
                except Exception:
                    continue
        except Exception:
            pass

        return None
