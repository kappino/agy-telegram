"""
Transcript Watcher for agy-telegram (v2.0 - High Performance O(1) Tailing).
Monitors the active session's transcript.jsonl in real time without full file rescans:
- O(1) Byte-offset seeking with buffer split for streaming JSONL records
- Emits real-time rich status events for thinking, reasoning, and tool calls
- Robust turn completion detection (does NOT stop prematurely on interim tool messages)
"""

import asyncio
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Callable, Coroutine, Any, Dict, List, Tuple

logger = logging.getLogger("agy_telegram.watcher")


def _read_transcript_delta(transcript_path: Path, current_offset: int) -> Tuple[str, int]:
    """Synchronous delta file read executed inside a worker thread."""
    try:
        file_size = transcript_path.stat().st_size
        if file_size < current_offset:
            # File rotated or truncated
            current_offset = 0

        if file_size == current_offset:
            return "", current_offset

        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(current_offset)
            chunk = f.read()
            new_offset = f.tell()
            return chunk, new_offset
    except Exception as e:
        logger.debug(f"Error reading transcript file at offset {current_offset}: {e}")
        return "", current_offset


class TranscriptWatcher:
    def __init__(self, brain_dir: Optional[str] = None):
        self.brain_dir = Path(brain_dir or Path.home() / ".gemini/antigravity-cli/brain")

    def get_latest_transcript_path(self, conv_id: Optional[str] = None) -> Optional[Path]:
        """Finds most recent transcript or the transcript for a specific conversation."""
        if conv_id:
            path = self.brain_dir / conv_id / ".system_generated/logs/transcript.jsonl"
            if path.is_file():
                return path

        if not self.brain_dir.is_dir():
            return None

        transcripts = sorted(
            self.brain_dir.glob("*/.system_generated/logs/transcript.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return transcripts[0] if transcripts else None

    def get_current_offset(self, transcript_path: Path) -> int:
        """Returns current file size in bytes for instant O(1) tailing."""
        try:
            return transcript_path.stat().st_size
        except Exception:
            return 0

    async def watch_turn(
        self,
        transcript_path: Path,
        start_line: Optional[int] = None,
        start_offset: Optional[int] = None,
        on_status: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
        on_final: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
        timeout_seconds: int = 300,
    ):
        """
        Streams transcript in real-time from a byte offset O(1).
        Emits status updates for reasoning and tool invocations, returning final response.
        """
        current_offset = 0

        # Calculate initial seek offset
        if start_offset is not None:
            current_offset = max(0, start_offset)
        elif start_line is not None and start_line > 0 and transcript_path.is_file():
            # Backward-compatible fallback: scan once to offset
            try:
                with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                    for _ in range(start_line):
                        if not f.readline():
                            break
                    current_offset = f.tell()
            except Exception as e:
                logger.warning(f"Failed to seek from start_line {start_line}: {e}")
                current_offset = 0
        elif transcript_path.is_file():
            current_offset = self.get_current_offset(transcript_path)

        last_status_sent = ""
        pending_buffer = ""
        start_time = time.monotonic()
        poll_interval = 0.3

        while (time.monotonic() - start_time) < timeout_seconds:
            await asyncio.sleep(poll_interval)

            if not transcript_path.is_file():
                continue

            chunk, current_offset = await asyncio.to_thread(
                _read_transcript_delta, transcript_path, current_offset
            )

            if not chunk:
                continue

            pending_buffer += chunk
            raw_lines = pending_buffer.split("\n")
            # Last element may be an incomplete line
            pending_buffer = raw_lines.pop()

            for line in raw_lines:
                clean_line = line.strip()
                if not clean_line:
                    continue

                try:
                    record = json.loads(clean_line)
                except Exception:
                    continue

                rec_type = record.get("type")
                content = record.get("content")
                tool_calls = record.get("tool_calls")
                thinking = record.get("thinking")

                if rec_type == "PLANNER_RESPONSE":
                    # 1. In-progress Tool Action
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

                            status_text = f"⚡ <b>Action:</b> {action}{detail}"
                            if status_text != last_status_sent and on_status:
                                last_status_sent = status_text
                                await on_status(status_text)

                    # 2. Reasoning Phase
                    elif thinking and not content:
                        status_text = "🧠 <b>Thinking...</b>"
                        if status_text != last_status_sent and on_status:
                            last_status_sent = status_text
                            await on_status(status_text)

                    # 3. Final Response (only when no concurrent tool calls exist)
                    elif content and not tool_calls:
                        logger.info(f"Received final response ({len(content)} characters).")
                        if on_final:
                            await on_final(content)
                        return
