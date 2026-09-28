"""
Terminal mirror logger allowing live terminal history / split-pane logging.
"""

from pathlib import Path
from datetime import datetime
import os


class TerminalMirror:
    def __init__(self, log_file: str = "/tmp/agy-telegram-chat.log"):
        self.log_path = Path(log_file)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.log_path.exists():
                self.log_path.touch(mode=0o666, exist_ok=True)
        except Exception:
            # Fallback sicuro in directory temporanea utente
            self.log_path = Path("/tmp/agy-telegram-chat.log")

    def log(self, sender: str, text: str):
        """Accoda l'evento nel file di log per visualizzazione live."""
        now = datetime.now().strftime("%H:%M:%S")
        entry = f"[{now}] [{sender.upper()}]: {text}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass
