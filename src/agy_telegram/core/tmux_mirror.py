"""
Tmux Mirror Engine for agy-telegram (v2.0 - Resource-Efficient & Dynamic Keys).
- Uses instant tmux buffer injection (set-buffer + paste-buffer)
- Zero 24/7 background polling: monitoring is turn-scoped and only captures recent tail (-S -25)
- Session existence validation with graceful fallback
- Solid, deterministic permission prompts
"""

import asyncio
import logging
import re
from typing import Optional, Callable, Coroutine, Any, List, Dict, Tuple

logger = logging.getLogger("agy_telegram.tmux_mirror")

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text: str) -> str:
    """Strips ANSI and VT100 escape codes."""
    return ANSI_ESCAPE.sub('', text)


class TmuxMirror:
    def __init__(self, target_session: str = "main:0.0", check_interval: float = 1.0):
        self.target = target_session
        self.check_interval = check_interval
        self.running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.on_output_callback: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None
        self.on_prompt_callback: Optional[Callable[[str, List[Tuple[str, str]]], Coroutine[Any, Any, None]]] = None
        self.last_prompt_signature = ""

    def register_callbacks(
        self,
        on_output: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
        on_prompt: Optional[Callable[[str, List[Tuple[str, str]]], Coroutine[Any, Any, None]]] = None,
    ):
        self.on_output_callback = on_output
        self.on_prompt_callback = on_prompt

    async def _exec_tmux(self, *args: str, timeout: float = 5.0) -> Tuple[bool, str, str]:
        """Executes a tmux command with rigid timeout and safe exception handling."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            return (proc.returncode == 0, out, err)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.error(f"tmux command timed out: tmux {' '.join(args)} (exceeded {timeout}s)")
            return (False, "", "timeout")
        except Exception as e:
            logger.debug(f"Error running tmux {' '.join(args)}: {e}")
            return (False, "", str(e))

    async def check_session_exists(self) -> bool:
        """Verifies if the target tmux session is alive and accessible."""
        session_name = self.target.split(":")[0] if ":" in self.target else self.target
        success, _, _ = await self._exec_tmux("has-session", "-t", session_name, timeout=3.0)
        return success

    async def send_input(self, text: str, press_enter: bool = True) -> bool:
        """Injects text instantaneously using tmux copy buffer with timeout guarantees."""
        if not await self.check_session_exists():
            logger.warning(f"tmux session '{self.target}' not found.")
            return False

        # Verify target pane is not dead or sitting at a bare shell
        ok_cmd, cmd_out, _ = await self._exec_tmux("display-message", "-p", "-t", self.target, "#{pane_current_command}")
        running_cmd = cmd_out.strip().lower() if ok_cmd else ""
        bare_shells = {"bash", "zsh", "sh", "fish", "csh", "tcsh", "dash"}
        if not ok_cmd or not running_cmd or running_cmd in bare_shells:
            logger.error(
                f"Cannot send input: pane '{self.target}' is running a bare shell or no process ({running_cmd})"
            )
            return False

        logger.info(f"Injecting input to tmux [{self.target}]: {text[:50]}...")
        ok_buf, _, _ = await self._exec_tmux("set-buffer", "-b", "agy_input", "--", text)
        if not ok_buf:
            return False

        ok_paste, _, _ = await self._exec_tmux("paste-buffer", "-p", "-d", "-b", "agy_input", "-t", self.target)
        if not ok_paste:
            return False

        if press_enter:
            ok_enter, _, _ = await self._exec_tmux("send-keys", "-t", self.target, "Enter")
            return ok_enter

        return True

    async def send_raw_key(self, key: str) -> bool:
        """Sends a single key or shortcut (e.g. C-c, Enter, '1', '4') with timeout."""
        if not await self.check_session_exists():
            return False

        success, _, _ = await self._exec_tmux("send-keys", "-t", self.target, key)
        return success

    async def capture_recent_lines(self, lines_count: int = 25) -> List[str]:
        """
        Captures only the last N visible lines of the target pane without wrapping (-J).
        Prevents full history captures and reduces CPU/memory footprint.
        """
        success, raw, _ = await self._exec_tmux(
            "capture-pane", "-t", self.target, "-p", "-J", "-S", f"-{lines_count}", timeout=3.0
        )
        if not success:
            return []
        clean = strip_ansi(raw)
        return clean.splitlines()

    def parse_permission_options(self, lines: List[str]) -> Tuple[str, List[Tuple[str, str]]]:
        """
        Extracts requested tool/command from terminal lines requiring confirmation.
        Searches backward from the most recent lines to avoid stale prompts.
        Fail-closed: if regex cannot identify the command, returns raw terminal context
        and disables Approve button (allows only Reject).
        """
        prompt_indicators = [
            "Requesting permission for:",
            "Run this command?",
            "Allow once",
            "Do you want to run this tool?",
        ]

        prompt_idx = -1
        for idx in range(len(lines) - 1, -1, -1):
            if any(ind in lines[idx] for ind in prompt_indicators):
                prompt_idx = idx
                break

        if prompt_idx == -1:
            raw_context = "\n".join(lines[-10:]).strip()
            return raw_context or "Unknown prompt", [("1", "Approve"), ("2", "Reject")]

        # Look for "Requesting permission for:" preceding prompt_idx
        req_idx = -1
        for idx in range(prompt_idx, -1, -1):
            if "Requesting permission for:" in lines[idx]:
                req_idx = idx
                break

        # Look for "Run this command?" or "1. Yes" following req_idx
        run_idx = -1
        start_search = max(0, req_idx) if req_idx != -1 else 0
        for idx in range(start_search, len(lines)):
            if "Run this command?" in lines[idx] or "1. Yes" in lines[idx]:
                run_idx = idx
                break

        cmd_requested = None
        if req_idx != -1 and run_idx != -1 and run_idx > req_idx:
            extracted = "\n".join(lines[req_idx + 1:run_idx]).strip()
            if extracted:
                cmd_requested = extracted

        # Fallback to regex search on relevant chunk if structured boundaries were not found
        if not cmd_requested:
            start_idx = max(0, prompt_idx - 10)
            end_idx = min(len(lines), prompt_idx + 10)
            recent_text = "\n".join(lines[start_idx:end_idx])

            patterns = [
                r'Requesting permission for:\s*\n\s*(.*?)(?:\n\s*Run this command|\n\s*\[|\n\s*1\.|\n\s*$)',
                r'Permission requested?:\s*\n\s*(.*?)(?:\n|$)',
                r'Run this command\?.*?`([^`]+)`',
                r'(?:command|execute):\s*`?([^\n`]+)`?',
            ]

            for pat in patterns:
                m = re.search(pat, recent_text, re.DOTALL | re.IGNORECASE)
                if m and m.group(1).strip():
                    candidate = m.group(1).strip()
                    if not candidate.startswith("1.") and not candidate.startswith("[1]"):
                        cmd_requested = candidate
                        break

        # Dynamically determine the correct Reject key (2 in 2-option prompts, 4 in 4-option prompts, default 4)
        reject_key = "4"
        for line in lines[max(0, prompt_idx - 2):min(len(lines), prompt_idx + 10)]:
            lower_line = line.lower()
            if "cancel" in lower_line or "no" in lower_line:
                if "2." in line:
                    reject_key = "2"
                    break
                elif "4." in line:
                    reject_key = "4"
                    break

        if cmd_requested:
            options = [
                ("1", "Approve"),
                (reject_key, "Reject"),
            ]
            return cmd_requested, options
        else:
            raw_context = "\n".join(lines[max(0, prompt_idx - 6):min(len(lines), prompt_idx + 8)]).strip()
            raw_desc = raw_context if raw_context else "Unrecognized permission prompt"
            options = [
                (reject_key, "Reject"),
            ]
            return raw_desc, options

    async def check_for_prompt(self) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
        """Checks whether terminal is currently halted on a permission request."""
        lines = await self.capture_recent_lines(lines_count=25)
        text = "\n".join(lines)
        prompt_indicators = [
            "Requesting permission for:",
            "Run this command?",
            "Allow once",
            "Do you want to run this tool?",
        ]
        if any(indicator in text for indicator in prompt_indicators):
            cmd, options = self.parse_permission_options(lines)
            return cmd, options
        return None

    async def start_turn_monitoring(
        self,
        on_prompt: Callable[[str, List[Tuple[str, str]]], Coroutine[Any, Any, None]],
    ):
        """
        Starts permission prompt monitoring ONLY for the duration of the active turn.
        Stops as soon as the turn concludes.
        """
        self.running = True
        self.last_prompt_signature = ""

        async def _turn_loop():
            while self.running:
                try:
                    await asyncio.sleep(self.check_interval)
                    prompt_info = await self.check_for_prompt()
                    if prompt_info:
                        cmd, options = prompt_info
                        sig = f"{cmd}:{[opt[0] for opt in options]}"
                        if sig != self.last_prompt_signature:
                            self.last_prompt_signature = sig
                            logger.info(f"Permission prompt intercepted: {cmd}")
                            await on_prompt(cmd, options)
                    else:
                        self.last_prompt_signature = ""
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Exception in turn monitoring: {e}")

        self.monitor_task = asyncio.create_task(_turn_loop())

    async def stop_turn_monitoring(self):
        """Stops turn monitoring immediately, releasing CPU."""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
        self.last_prompt_signature = ""

    async def start_monitor(self):
        logger.info(f"TmuxMirror initialized for target {self.target} (event-driven mode).")

    async def stop_monitor(self):
        await self.stop_turn_monitoring()
