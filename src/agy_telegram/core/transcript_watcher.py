"""
Transcript Watcher for agy-telegram.
Monitors the active session's transcript.jsonl in real time, emitting events for:
- Thinking and reasoning steps
- Tool executions with detailed command previews (e.g. CommandLine, TargetFile, action)
- Final markdown response content
"""

import asyncio
import html
import json
import logging
from pathlib import Path
from typing import Optional, Callable, Coroutine, Any, Dict

logger = logging.getLogger("agy_telegram.watcher")

class TranscriptWatcher:
    def __init__(self, brain_dir: Optional[str] = None):
        self.brain_dir = Path(brain_dir or Path.home() / ".gemini/antigravity-cli/brain")

    def get_latest_transcript_path(self, conv_id: Optional[str] = None) -> Optional[Path]:
        if conv_id:
            path = self.brain_dir / conv_id / ".system_generated/logs/transcript.jsonl"
            if path.is_file():
                return path

        if not self.brain_dir.is_dir():
            return None

        transcripts = sorted(
            self.brain_dir.glob("*/.system_generated/logs/transcript.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        return transcripts[0] if transcripts else None

    async def watch_turn(
        self,
        transcript_path: Path,
        start_line: int,
        on_status: Callable[[str], Coroutine[Any, Any, None]],
        on_final: Callable[[str], Coroutine[Any, Any, None]],
        timeout_seconds: int = 300,
    ):
        """
        Segue in tempo reale il transcript a partire da start_line.
        Emette aggiornamenti di stato arricchiti di dettagli ed infine la risposta finale.
        """
        current_line_idx = start_line
        last_status_sent = ""
        elapsed = 0
        poll_interval = 0.3

        while elapsed < timeout_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            if not transcript_path.is_file():
                continue

            try:
                with open(transcript_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                continue

            total_lines = len(lines)
            if total_lines > current_line_idx:
                for line in lines[current_line_idx:]:
                    current_line_idx += 1
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue

                    rec_type = record.get("type")
                    content = record.get("content")
                    tool_calls = record.get("tool_calls")
                    thinking = record.get("thinking")

                    if rec_type == "PLANNER_RESPONSE":
                        # Caso 1: Tool execution dettagliata
                        if tool_calls:
                            for tc in tool_calls:
                                name = tc.get("name", "tool")
                                args = tc.get("args", {})
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        args = {}
                                action = html.escape(str(args.get("toolAction") or args.get("toolSummary") or name))
                                
                                # Estrai il comando o file specifico
                                detail = ""
                                if "CommandLine" in args:
                                    cmd_preview = str(args["CommandLine"]).strip()
                                    if len(cmd_preview) > 60:
                                        cmd_preview = cmd_preview[:60] + "..."
                                    detail = f"\n<pre><code>{html.escape(cmd_preview)}</code></pre>"
                                elif "TargetFile" in args:
                                    target = Path(args["TargetFile"]).name
                                    detail = f"\n📁 <code>{html.escape(target)}</code>"
                                elif "AbsolutePath" in args:
                                    target = Path(args["AbsolutePath"]).name
                                    detail = f"\n📄 <code>{html.escape(target)}</code>"

                                status_text = f"⚡ <b>Azione:</b> {action}{detail}"
                                if status_text != last_status_sent:
                                    last_status_sent = status_text
                                    await on_status(status_text)
                        
                        # Caso 2: Thinking / Reasoning
                        elif thinking and not content:
                            status_text = "🧠 <b>Ragionamento in corso...</b>"
                            if status_text != last_status_sent:
                                last_status_sent = status_text
                                await on_status(status_text)

                        # Caso 3: Risposta finale per l'utente
                        elif content:
                            logger.info(f"Ricevuta risposta finale ({len(content)} caratteri).")
                            await on_final(content)
                            return
