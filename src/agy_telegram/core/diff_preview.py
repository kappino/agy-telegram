"""
In-memory unified diff generator and Telegram preview formatter for agy-telegram.
Synthesizes pre-execution diffs without altering on-disk files.
"""

from __future__ import annotations

import difflib
import io
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

logger = logging.getLogger("agy_telegram.diff")


def generate_diff_for_tool(
    tool_name: str,
    args: Dict[str, Any],
    workspace: Optional[Path] = None,
) -> Optional[str]:
    """Generates an in-memory unified diff for file modification tools."""
    if not isinstance(args, dict):
        return None

    norm_tool = (tool_name or "").lower().strip()
    ws = Path(workspace) if workspace else Path.cwd()

    try:
        if norm_tool in ("replace_file_content", "replace_file"):
            target_path_raw = args.get("TargetFile") or args.get("target_file") or args.get("path")
            target_content = args.get("TargetContent") or args.get("target_content") or ""
            replacement_content = args.get("ReplacementContent") or args.get("replacement_content") or ""
            allow_multiple = bool(args.get("AllowMultiple") or args.get("allow_multiple", False))

            if not target_path_raw:
                return None

            file_path = Path(target_path_raw)
            if not file_path.is_absolute():
                file_path = ws / file_path

            if not file_path.is_file():
                return None

            original = file_path.read_text(encoding="utf-8", errors="replace")
            if target_content not in original:
                return None

            count = -1 if allow_multiple else 1
            modified = original.replace(target_content, replacement_content, count)

            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    modified.splitlines(keepends=True),
                    fromfile=f"a/{file_path.name}",
                    tofile=f"b/{file_path.name}",
                )
            )
            return "".join(diff_lines) if diff_lines else None

        elif norm_tool in ("write_to_file", "create_file", "write_file"):
            target_path_raw = args.get("TargetFile") or args.get("target_file") or args.get("path")
            code_content = args.get("CodeContent") or args.get("code_content") or ""
            append_mode = bool(args.get("Append") or args.get("append", False))

            if not target_path_raw:
                return None

            file_path = Path(target_path_raw)
            if not file_path.is_absolute():
                file_path = ws / file_path

            if file_path.is_file():
                original = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                original = ""

            if append_mode:
                modified = original + code_content
            else:
                modified = code_content

            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    modified.splitlines(keepends=True),
                    fromfile=f"a/{file_path.name}" if original else "/dev/null",
                    tofile=f"b/{file_path.name}",
                )
            )
            return "".join(diff_lines) if diff_lines else None

        elif norm_tool in ("multi_replace_file_content", "multi_replace"):
            target_path_raw = args.get("TargetFile") or args.get("target_file") or args.get("path")
            chunks = args.get("ReplacementChunks") or args.get("replacement_chunks") or []

            if not target_path_raw or not chunks:
                return None

            file_path = Path(target_path_raw)
            if not file_path.is_absolute():
                file_path = ws / file_path

            if not file_path.is_file():
                return None

            original = file_path.read_text(encoding="utf-8", errors="replace")
            current = original

            for chunk in chunks:
                if isinstance(chunk, dict):
                    tc = chunk.get("TargetContent") or chunk.get("target_content") or ""
                    rc = chunk.get("ReplacementContent") or chunk.get("replacement_content") or ""
                    if tc and tc in current:
                        current = current.replace(tc, rc, 1)

            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"a/{file_path.name}",
                    tofile=f"b/{file_path.name}",
                )
            )
            return "".join(diff_lines) if diff_lines else None

    except Exception as e:
        logger.error(f"Error computing in-memory diff for tool '{tool_name}': {e}")
        return None

    return None


def format_diff_for_telegram(
    diff_text: str,
    filename: str = "changes",
) -> Tuple[Optional[str], Optional[io.BytesIO]]:
    """
    Formats diff content for Telegram delivery:
    - If length <= 3000 chars: returns formatted markdown block and None.
    - If length > 3000 chars: returns None and in-memory BytesIO patch file.
    """
    if not diff_text or not diff_text.strip():
        return None, None

    if len(diff_text) <= 3000:
        return f"```diff\n{diff_text.strip()}\n```", None

    buf = io.BytesIO(diff_text.encode("utf-8"))
    clean_name = Path(filename).name if filename else "changes"
    buf.name = f"{clean_name}.patch" if not clean_name.endswith(".patch") else clean_name
    buf.seek(0)
    return None, buf


def extract_latest_tool_call_from_transcript(transcript_path: Path) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Scans the trailing lines of transcript.jsonl backwards to find the last requested tool call."""
    if not transcript_path or not transcript_path.is_file():
        return None

    try:
        # Bounded read from tail: up to 64KB
        file_size = transcript_path.stat().st_size
        read_bytes = min(file_size, 65536)

        with open(transcript_path, "rb") as f:
            if file_size > read_bytes:
                f.seek(file_size - read_bytes)
            raw_data = f.read().decode("utf-8", errors="replace")

        lines = [line.strip() for line in raw_data.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                record = json.loads(line)
                if record.get("type") == "PLANNER_RESPONSE":
                    tool_calls = record.get("tool_calls")
                    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                        last_call = tool_calls[-1]
                        name = last_call.get("name", "")
                        args = last_call.get("args", {})
                        if name:
                            return name, args
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Unable to extract tool calls from transcript {transcript_path}: {e}")

    return None
