"""
Session manager interfacing with Antigravity history and conversations database.
"""

import json
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

class SessionManager:
    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir or Path.home() / ".gemini/antigravity-cli")
        self.history_file = self.data_dir / "history.jsonl"
        self.db_file = self.data_dir / "conversation_summaries.db"
        self.current_conversation_id: Optional[str] = None

    def set_active_session(self, conv_id: Optional[str]):
        """Imposta l'ID di conversazione attiva (None = nuova sessione)."""
        self.current_conversation_id = conv_id

    def get_active_session(self) -> Optional[str]:
        return self.current_conversation_id

    def list_recent_sessions(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Elenca le sessioni recenti estraendo metadata e riassunto."""
        sessions = []
        
        # 1. Prova prima dal DB sqlite delle sintesi se disponibile
        if self.db_file.is_file():
            try:
                conn = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
                cur = conn.cursor()
                # Trova schema tabelle
                cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = [r[0] for r in cur.fetchall()]
                if "summaries" in tables or "conversations" in tables:
                    table_name = "summaries" if "summaries" in tables else "conversations"
                    cur.execute(f"SELECT * FROM {table_name} LIMIT ?", (limit,))
                    # se disponibile estrai i record
                conn.close()
            except Exception:
                pass

        # 2. Parsing limitato da history.jsonl (usando deque con dimensione limitata per evitare memory spikes)
        if self.history_file.is_file():
            from collections import deque
            try:
                # Leggi solo le ultime righe senza allocare l'intero file in memoria
                max_tail = max(50, limit * 10)
                with open(self.history_file, "r", encoding="utf-8", errors="replace") as f:
                    recent_lines = deque(f, maxlen=max_tail)

                seen = set()
                for line in reversed(recent_lines):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        conv_id = entry.get("conversationId")
                        if not conv_id and "id" in entry:
                            conv_id = entry["id"]

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
                                "preview": (display[:80] + "...") if len(display) > 80 else display,
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
