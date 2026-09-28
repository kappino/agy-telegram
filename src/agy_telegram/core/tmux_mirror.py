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
    """Rimuove codici di escape ANSI / VT100."""
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

    async def check_session_exists(self) -> bool:
        """Verifica se la sessione tmux target è attiva e accessibile."""
        # Se il target contiene un indice di pannello (es. main:0.0), estrai il nome sessione
        session_name = self.target.split(":")[0] if ":" in self.target else self.target
        cmd = ["tmux", "has-session", "-t", session_name]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception as e:
            logger.debug(f"Verifica sessione tmux fallita ({e})")
            return False

    async def send_input(self, text: str, press_enter: bool = True) -> bool:
        """Inietta il testo istantaneamente tramite buffer di copia tmux (senza lag da digitazione)."""
        if not await self.check_session_exists():
            logger.warning(f"Sessione tmux '{self.target}' non trovata!")
            return False

        logger.info(f"Invio input istantaneo a tmux [{self.target}]: {text[:50]}...")
        proc_buf = await asyncio.create_subprocess_exec("tmux", "set-buffer", "-b", "agy_input", text)
        await proc_buf.wait()

        proc_paste = await asyncio.create_subprocess_exec("tmux", "paste-buffer", "-b", "agy_input", "-t", self.target)
        await proc_paste.wait()

        if press_enter:
            proc_enter = await asyncio.create_subprocess_exec("tmux", "send-keys", "-t", self.target, "Enter")
            await proc_enter.wait()

        return True

    async def send_raw_key(self, key: str) -> bool:
        """Invia un tasto speciale (es. C-c, Enter, '1', '2')."""
        if not await self.check_session_exists():
            return False

        proc = await asyncio.create_subprocess_exec("tmux", "send-keys", "-t", self.target, key)
        await proc.wait()
        return True

    async def capture_recent_lines(self, lines_count: int = 25) -> List[str]:
        """
        Cattura unicamente le ultime N righe visibili del pannello target.
        Evita di catturare 3.000 righe e riduce drasticamente l'overhead di CPU e memoria.
        """
        cmd = ["tmux", "capture-pane", "-t", self.target, "-p", "-S", f"-{lines_count}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return []
            raw = stdout.decode("utf-8", errors="replace")
            clean = strip_ansi(raw)
            return clean.splitlines()
        except Exception as e:
            logger.debug(f"Errore cattura tmux pane: {e}")
            return []

    def parse_permission_options(self, lines: List[str]) -> Tuple[str, List[Tuple[str, str]]]:
        """
        Analizza le righe del terminale per estrarre il comando per cui viene chiesta autorizzazione.
        Restituisce opzioni pulite e sicure (1 = Approva, 4 = Rifiuta).
        """
        text = "\n".join(lines)
        cmd_requested = "Comando di sistema"

        perm_match = re.search(r'Requesting permission for:\s*\n\s*(.*?)(?:\n\s*Run this command|\n\s*\[|\n\s*1\.)', text, re.DOTALL)
        if perm_match:
            cmd_requested = perm_match.group(1).strip()
        else:
            cmd_match = re.search(r'(?:command|execute):\s*`?([^\n`]+)`?', text, re.IGNORECASE)
            if cmd_match:
                cmd_requested = cmd_match.group(1).strip()

        # In Antigravity CLI interattivo:
        # Tasto 1 = Approva (Allow once)
        # Tasto 4 = Rifiuta (Deny)
        options = [
            ("1", "✅ Approva"),
            ("4", "❌ Rifiuta"),
        ]

        return cmd_requested, options

    async def check_for_prompt(self) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
        """Verifica se il terminale è attualmente fermo su una richiesta di autorizzazione."""
        lines = await self.capture_recent_lines(lines_count=25)
        text = "\n".join(lines)
        if "Requesting permission for:" in text or "Run this command?" in text:
            cmd, options = self.parse_permission_options(lines)
            return cmd, options
        return None

    async def start_turn_monitoring(
        self,
        on_prompt: Callable[[str, List[Tuple[str, str]]], Coroutine[Any, Any, None]],
    ):
        """
        Avvia il monitoraggio delle autorizzazioni SOLO per la durata del turno attivo.
        Si arresta non appena il turno è completato.
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
                            logger.info(f"Richiesta autorizzazione intercettata: {cmd}")
                            await on_prompt(cmd, options)
                    else:
                        self.last_prompt_signature = ""
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Eccezione durante turn monitoring: {e}")

        self.monitor_task = asyncio.create_task(_turn_loop())

    async def stop_turn_monitoring(self):
        """Arresta immediatamente il monitoraggio del turno, rilasciando la CPU."""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
        self.last_prompt_signature = ""

    # Metodi di compatibilità con la vecchia interfaccia
    async def start_monitor(self):
        """Modalità dormiente: non effettua polling a vuoto h24."""
        logger.info(f"TmuxMirror inizializzato per target {self.target} (modalità event-driven attiva).")

    async def stop_monitor(self):
        await self.stop_turn_monitoring()
