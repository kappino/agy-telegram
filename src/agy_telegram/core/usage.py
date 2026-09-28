"""
Usage and token tracking for Google Antigravity (agy).
Calculates session steps, tool invocations, token consumption, and context window saturation.
"""

import html
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("agy_telegram.usage")


def compute_session_usage(transcript_path: Optional[Path], current_model: str) -> Dict[str, Any]:
    """Calcola le metriche di consumo token, step e contesto per la sessione corrente."""
    max_context = 1_000_000
    m_lower = current_model.lower()
    if "claude" in m_lower:
        max_context = 200_000
    elif "pro" in m_lower:
        max_context = 1_000_000
    elif "flash" in m_lower:
        max_context = 1_000_000
    elif "120b" in m_lower:
        max_context = 128_000

    steps = 0
    tool_calls_count = 0
    total_chars = 0

    if transcript_path and transcript_path.is_file():
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    steps += 1
                    try:
                        record = json.loads(line)
                        c = record.get("content") or ""
                        t = record.get("thinking") or ""
                        tc = record.get("tool_calls") or []
                        if tc:
                            tool_calls_count += len(tc)
                        total_chars += len(c) + len(t) + len(str(tc))
                    except Exception:
                        total_chars += len(line)
        except Exception as e:
            logger.debug(f"Errore calcolo usage su transcript: {e}")

    est_tokens = max(1, total_chars // 4)
    tokens_remaining = max(0, max_context - est_tokens)
    pct_used = min(100.0, (est_tokens / max_context) * 100.0)

    # Barra grafica di saturazione contesto (10 blocchi)
    filled = min(10, max(0, int(round(pct_used / 10.0))))
    bar = "█" * filled + "░" * (10 - filled)

    return {
        "model": current_model,
        "steps": steps,
        "tool_calls": tool_calls_count,
        "est_tokens_used": est_tokens,
        "max_context": max_context,
        "tokens_remaining": tokens_remaining,
        "pct_used": pct_used,
        "bar": bar,
    }


def format_usage_html(stats: Dict[str, Any], conv_id: str, current_mode: str) -> str:
    """Formatta le metriche in un messaggio Telegram HTML pulito ed esaustivo."""
    used_k = f"{stats['est_tokens_used'] / 1000:.1f}K" if stats['est_tokens_used'] >= 1000 else str(stats['est_tokens_used'])
    rem_k = f"{stats['tokens_remaining'] / 1000:.1f}K" if stats['tokens_remaining'] >= 1000 else str(stats['tokens_remaining'])
    max_k = f"{stats['max_context'] / 1000:.0f}K" if stats['max_context'] < 1_000_000 else "1.0M"

    return (
        "📈 <b>Antigravity Context & Token Usage</b>\n\n"
        f"🤖 <b>Modello:</b> <code>{html.escape(stats['model'])}</code>\n"
        f"⚙️ <b>Modalità:</b> <code>{html.escape(current_mode)}</code>\n"
        f"🆔 <b>Sessione:</b> <code>{html.escape(conv_id)}</code>\n\n"
        f"📊 <b>Finestra di Contesto:</b>\n"
        f"• Token Usati: <b>~{used_k}</b> ({stats['pct_used']:.1f}%)\n"
        f"• Token Rimasti: <b>~{rem_k}</b> ({100 - stats['pct_used']:.1f}% disp.)\n"
        f"• Capacità Massima: <b>{max_k} tokens</b>\n"
        f"• Saturazione: <code>[{stats['bar']}] {stats['pct_used']:.1f}%</code>\n\n"
        f"⚡ <b>Attività Turno:</b>\n"
        f"• Passaggi registrati: <b>{stats['steps']}</b>\n"
        f"• Esecuzioni strumenti (tools): <b>{stats['tool_calls']}</b>\n"
    )
