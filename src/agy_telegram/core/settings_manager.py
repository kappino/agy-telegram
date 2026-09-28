"""
Settings manager for Google Antigravity (agy).
Dynamically locates and safely updates settings.json across all platforms and user accounts.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, List, Tuple

logger = logging.getLogger("agy_telegram.settings")

AVAILABLE_MODELS: List[Tuple[str, str]] = [
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("gemini-3.6-flash-low", "Gemini 3.6 Flash (Low)"),
    ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
]

AVAILABLE_MODES: List[Tuple[str, str, str]] = [
    ("accept-edits", "✍️ Auto-Edit (accept-edits)", "Modifiche file auto-approvate, prompt solo per comandi shell."),
    ("default", "🛡️ Standard (default)", "Chiede conferma per comandi e modifiche ai file."),
    ("plan", "📋 Plan Only (plan)", "Sola pianificazione e analisi, nessuna modifica applicata."),
]


def resolve_settings_path() -> Path:
    """Risolve dinamicamente il percorso di settings.json rispettando le variabili d'ambiente."""
    env_home = os.getenv("ANTIGRAVITY_HOME") or os.getenv("GEMINI_CLI_HOME")
    if env_home:
        base = Path(env_home)
    else:
        base = Path.home() / ".gemini/antigravity-cli"

    return base / "settings.json"


def get_current_model() -> str:
    """Legge il modello attualmente configurato in settings.json."""
    settings_path = resolve_settings_path()
    if settings_path.is_file():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("model", "Gemini 3.8 Flash (High)")
        except Exception as e:
            logger.debug(f"Impossibile leggere il modello da {settings_path}: {e}")
    return "Gemini 3.8 Flash (High)"


import tempfile


def _atomic_update_settings(key: str, value: str) -> bool:
    """Aggiorna atomisticamente un campo in settings.json tramite file temporaneo e rename."""
    settings_path = resolve_settings_path()
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if settings_path.is_file():
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data[key] = value

        # Scrittura atomica per prevenire file vuoto o corrotto in caso di crash
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(settings_path.parent),
            prefix="settings_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        )
        try:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, str(settings_path))
            return True
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.error(f"Errore scrittura atomica in {settings_path}: {e}")
        return False


def set_current_model(model_name: str) -> bool:
    """Aggiorna il modello in settings.json in modo atomico."""
    ok = _atomic_update_settings("model", model_name)
    if ok:
        logger.info(f"Modello aggiornato in {resolve_settings_path()}: {model_name}")
    return ok


def get_current_mode() -> str:
    """Legge la modalità di esecuzione (default, accept-edits, plan)."""
    settings_path = resolve_settings_path()
    if settings_path.is_file():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("mode", "default")
        except Exception:
            pass
    return "default"


def set_current_mode(mode_slug: str) -> bool:
    """Aggiorna la modalità di esecuzione in settings.json in modo atomico."""
    ok = _atomic_update_settings("mode", mode_slug)
    if ok:
        logger.info(f"Modalità aggiornata in {resolve_settings_path()}: {mode_slug}")
    return ok
