"""
Transcript Watcher for agy-telegram (v2.0 - High Performance O(1) Tailing).
Monitors the active session's transcript.jsonl in real time without full file rescans:
- O(1) Byte-offset seeking with buffer split for streaming JSONL records
- Emits real-time rich status events for thinking, reasoning, and tool calls
- Robust turn completion detection (does NOT stop prematurely on interim tool messages)
"""

import asyncio
import html
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Callable, Coroutine, Any, Dict, List, Tuple, Set

from agy_telegram.config import get_antigravity_home

logger = logging.getLogger("agy_telegram.watcher")


def _read_transcript_delta(transcript_path: Path, current_offset: int) -> Tuple[str, int]:
    """Synchronous binary delta file read executed inside a worker thread."""
    try:
        file_size = transcript_path.stat().st_size
        if file_size < current_offset:
            # File rotated or truncated
            current_offset = 0

        if file_size == current_offset:
            return "", current_offset

        with open(transcript_path, "rb") as f:
            f.seek(current_offset)
            raw_bytes = f.read()
            new_offset = f.tell()
            chunk = raw_bytes.decode("utf-8", errors="replace")
            return chunk, new_offset
    except Exception as e:
        logger.debug(f"Error reading transcript file at offset {current_offset}: {e}")
        return "", current_offset


class TranscriptWatcher:
    def __init__(self, brain_dir: Optional[str] = None):
        self.brain_dir = Path(brain_dir) if brain_dir else get_antigravity_home() / "brain"

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

    async def await_latest_transcript(
        self, conv_id: Optional[str] = None, timeout: float = 5.0
    ) -> Optional[Path]:
        """Polls asynchronously for up to `timeout` seconds waiting for transcript.jsonl to appear."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            p = self.get_latest_transcript_path(conv_id=conv_id)
            if p and p.is_file():
                return p
            await asyncio.sleep(0.4)
        return None

    async def await_active_transcript_and_offset(
        self,
        conv_id: Optional[str] = None,
        prev_transcript_path: Optional[Path] = None,
        prev_offset: int = 0,
        timeout: float = 6.0,
    ) -> Tuple[Optional[Path], int]:
        """
        Detects either a newly created transcript file (after /new or session rotation)
        or new data appended to an existing transcript.
        Returns (transcript_path, start_offset).
        """
        if conv_id:
            target_path = self.get_latest_transcript_path(conv_id=conv_id)
            if target_path and target_path.is_file():
                return target_path, prev_offset

        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            latest = self.get_latest_transcript_path()
            if latest and latest.is_file():
                # Case 1: Brand new transcript session created
                if prev_transcript_path is None or latest.resolve() != prev_transcript_path.resolve():
                    return latest, 0

                # Case 2: Existing session grew with the new turn
                curr_size = self.get_current_offset(latest)
                if curr_size > prev_offset:
                    return latest, prev_offset

            await asyncio.sleep(0.2)

        fallback = self.get_latest_transcript_path(conv_id=conv_id) or prev_transcript_path
        return fallback, prev_offset

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
        on_interim: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
        timeout_seconds: int = 300,
    ):
        """
        Streams transcript in real-time from a byte offset O(1).
        Emits status updates for reasoning, tool invocations, and interim messages.
        Accurately detects turn completion without terminating prematurely during
        background/async task execution.
        Raises TimeoutError if timeout_seconds is exceeded without turn completion.
        """
        current_offset = 0

        # Calculate initial seek offset
        if start_offset is not None:
            current_offset = max(0, start_offset)
        elif start_line is not None and start_line > 0 and transcript_path.is_file():
            # Backward-compatible fallback: scan once to offset in binary
            try:
                with open(transcript_path, "rb") as f:
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
        last_activity_time = time.monotonic()
        poll_interval = 0.25
        debounce_seconds = 0.6

        active_tasks: Set[str] = set()
        running_tasks_count = 0
        running_subagents = 0
        modified_files: Set[str] = set()
        pending_final_content: Optional[str] = None
        pending_final_candidate_time: float = 0.0

        while (time.monotonic() - last_activity_time) < timeout_seconds:
            await asyncio.sleep(poll_interval)

            if not transcript_path.is_file():
                continue

            chunk, current_offset = await asyncio.to_thread(
                _read_transcript_delta, transcript_path, current_offset
            )

            if chunk:
                last_activity_time = time.monotonic()
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
                    rec_status = record.get("status")
                    content = record.get("content")
                    tool_calls = record.get("tool_calls")
                    thinking = record.get("thinking")

                    # 1. Track background task lifecycle from GENERIC records
                    if rec_type == "GENERIC":
                        content_str = str(content or "")
                        if rec_status == "RUNNING" or ("background task" in content_str.lower() and "task id:" in content_str.lower()):
                            m = re.search(r'task id:?\s*["\']?([^"\'\s\n]+)', content_str, re.IGNORECASE)
                            if m:
                                task_id = m.group(1).strip()
                                active_tasks.add(task_id)
                                logger.info(f"Background task started: {task_id} (active: {len(active_tasks)})")
                            else:
                                running_tasks_count += 1
                        elif rec_status == "DONE":
                            m = re.search(r'Task id\s*["\']?([^"\'\s\n]+)["\']?\s*finished', content_str, re.IGNORECASE)
                            if m:
                                active_tasks.discard(m.group(1).strip())

                    # 2. Track background task / subagent completion from SYSTEM_MESSAGE
                    elif rec_type == "SYSTEM_MESSAGE":
                        content_str = str(content or "")
                        m = re.search(r'Task id\s*["\']?([^"\'\s\n]+)["\']?\s*finished', content_str, re.IGNORECASE)
                        if not m:
                            m = re.search(r'sender=([^\s\n]+).*finished', content_str, re.IGNORECASE)
                        if m:
                            task_id = m.group(1).strip()
                            active_tasks.discard(task_id)
                            logger.info(f"Background task completed: {task_id} (remaining active: {len(active_tasks)})")
                        elif "finished with result" in content_str.lower() or "completed" in content_str.lower():
                            if running_tasks_count > 0:
                                running_tasks_count -= 1

                        if "subagent" in content_str.lower() and running_subagents > 0:
                            running_subagents -= 1

                    # 3. Model Responses
                    elif rec_type == "PLANNER_RESPONSE":
                        # A. In-progress Tool Action
                        if tool_calls:
                            pending_final_content = None
                            for tc in tool_calls:
                                name = tc.get("name", "tool")
                                if name == "invoke_subagent":
                                    running_subagents += 1
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
                                    target_fp = str(args["TargetFile"]).strip('\"\'')
                                    modified_files.add(target_fp)
                                    target = Path(target_fp).name
                                    detail = f"\n📁 <code>{html.escape(target)}</code>"
                                elif "AbsolutePath" in args:
                                    target = Path(args["AbsolutePath"]).name
                                    detail = f"\n📄 <code>{html.escape(target)}</code>"

                                status_text = f"⚡ <b>Action:</b> {action}{detail}"
                                if status_text != last_status_sent and on_status:
                                    last_status_sent = status_text
                                    await on_status(status_text)

                        # B. Reasoning Phase
                        elif thinking and not content:
                            pending_final_content = None
                            status_text = "🧠 <b>Thinking...</b>"
                            if status_text != last_status_sent and on_status:
                                last_status_sent = status_text
                                await on_status(status_text)

                        # C. Text Response: Check if Interim or Final Candidate
                        elif content and not tool_calls:
                            has_async = (len(active_tasks) > 0 or running_tasks_count > 0 or running_subagents > 0)
                            if has_async:
                                logger.info(f"Received interim response ({len(content)} characters) while async tasks running.")
                                pending_final_content = None
                                if on_interim:
                                    await on_interim(content)
                                elif on_status:
                                    await on_status(f"💬 <i>{html.escape(content[:250])}</i>\n\n⏳ <i>Background task in progress...</i>")
                            else:
                                pending_final_content = content
                                pending_final_candidate_time = time.monotonic()
            else:
                # No new chunk in this tick: evaluate settled final candidate
                has_async = (len(active_tasks) > 0 or running_tasks_count > 0 or running_subagents > 0)
                if pending_final_content is not None and not has_async:
                    if (time.monotonic() - pending_final_candidate_time) >= debounce_seconds:
                        logger.info(f"Received final response ({len(pending_final_content)} characters).")
                        if on_final:
                            sig = inspect.signature(on_final)
                            if len(sig.parameters) >= 2 or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                                await on_final(pending_final_content, modified_files=list(modified_files))
                            else:
                                await on_final(pending_final_content)
                        return

        logger.warning(f"watch_turn timed out after {timeout_seconds}s of inactivity on {transcript_path}")
        raise asyncio.TimeoutError(f"Task execution timed out after {timeout_seconds} seconds of inactivity.")
