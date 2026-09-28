"""
Tmux Mirror Engine for agy-telegram.
Captures live output from the active agy CLI session in tmux,
detects response completion and interactive permission requests, and forwards them to Telegram.
"""

import asyncio
import logging
import re
from typing import Optional, Callable, Coroutine, Any, List

logger = logging.getLogger("agy_telegram.tmux_mirror")

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text: str) -> str:
    """Rimuove codici di escape ANSI / VT100."""
    return ANSI_ESCAPE.sub('', text)

class TmuxMirror:
    def __init__(self, target_session: str = "aegis:0.0", poll_interval: float = 0.5):
        self.target = target_session
        self.poll_interval = poll_interval
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.on_output_callback: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None
        self.on_prompt_callback: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None
        self.last_seen_lines_count = 0
        self.last_prompt_signature = ""

    def register_callbacks(
        self,
        on_output: Callable[[str], Coroutine[Any, Any, None]],
        on_prompt: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None,
    ):
        self.on_output_callback = on_output
        self.on_prompt_callback = on_prompt

    async def send_input(self, text: str, press_enter: bool = True):
        """Inietta il testo istantaneamente tramite buffer di copia tmux."""
        logger.info(f"Invio input istantaneo a tmux [{self.target}]: {text[:50]}...")
        # Imposta il buffer di tmux con l'intero testo
        proc_buf = await asyncio.create_subprocess_exec("tmux", "set-buffer", "-b", "agy_input", text)
        await proc_buf.wait()

        # Incolla il buffer nel pannello target istantaneamente (senza simulare la digitazione tasto per tasto)
        proc_paste = await asyncio.create_subprocess_exec("tmux", "paste-buffer", "-b", "agy_input", "-t", self.target)
        await proc_paste.wait()

        if press_enter:
            proc_enter = await asyncio.create_subprocess_exec("tmux", "send-keys", "-t", self.target, "Enter")
            await proc_enter.wait()

    async def send_raw_key(self, key: str):
        """Invia un tasto speciale (es. C-c, Enter, '1', '2', Up, Down)."""
        proc = await asyncio.create_subprocess_exec("tmux", "send-keys", "-t", self.target, key)
        await proc.wait()

    async def capture_full_history(self) -> List[str]:
        """Cattura l'intera cronologia del pannello tmux target come lista di righe."""
        cmd = ["tmux", "capture-pane", "-t", self.target, "-p", "-S", "-3000"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        raw = stdout.decode("utf-8", errors="replace")
        clean = strip_ansi(raw)
        return clean.splitlines()

    async def start_monitor(self):
        """Avvia il loop asincrono di cattura differenziale del terminale."""
        self.running = True
        logger.info(f"Monitoraggio tmux attivo sul target: {self.target}")
        initial_lines = await self.capture_full_history()
        self.last_seen_lines_count = len(initial_lines)
        self.task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        accumulated_new_lines: List[str] = []
        quiet_ticks = 0

        while self.running:
            try:
                await asyncio.sleep(self.poll_interval)
                curr_lines = await self.capture_full_history()
                total_len = len(curr_lines)

                # 1. Controllo se ci sono nuove righe aggiunte alla storia
                if total_len > self.last_seen_lines_count:
                    new_slice = curr_lines[self.last_seen_lines_count:]
                    self.last_seen_lines_count = total_len
                    accumulated_new_lines.extend(new_slice)
                    quiet_ticks = 0
                else:
                    if accumulated_new_lines:
                        quiet_ticks += 1

                # 2. Controllo interattivo autorizzazioni (human-in-the-loop)
                # Guarda le ultime 30 righe per intercettare il menu "Requesting permission for:"
                recent_tail = "\n".join(curr_lines[-30:])
                if "Requesting permission for:" in recent_tail and "Run this command?" in recent_tail:
                    # Estrai il comando esatto richiesto
                    match = re.search(r'Requesting permission for:\s*\n\s*(.*?)\n\s*Run this command\?', recent_tail, re.DOTALL)
                    cmd_requested = match.group(1).strip() if match else "Comando di sistema"

                    # Firma univoca per non duplicare la notifica
                    prompt_sig = f"{cmd_requested}"
                    if prompt_sig != self.last_prompt_signature:
                        self.last_prompt_signature = prompt_sig
                        logger.info(f"Rilevata richiesta autorizzazione per: {cmd_requested}")
                        if self.on_prompt_callback:
                            await self.on_prompt_callback(cmd_requested, prompt_sig)
                else:
                    # Se non siamo più in attesa di autorizzazione, resetta la firma
                    self.last_prompt_signature = ""

                # 3. Flush output testuale generale
                if accumulated_new_lines and quiet_ticks >= 2:
                    raw_text = "\n".join(accumulated_new_lines).strip()
                    accumulated_new_lines = []
                    quiet_ticks = 0

                    # Non inoltrare se è un frammento del menu di autorizzazione
                    if "Run this command?" not in raw_text and "Requesting permission for:" not in raw_text:
                        if self.on_output_callback:
                            await self.on_output_callback(raw_text)

            except Exception as e:
                logger.error(f"Errore nel loop tmux poll: {e}")
                await asyncio.sleep(2)

    async def stop_monitor(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
