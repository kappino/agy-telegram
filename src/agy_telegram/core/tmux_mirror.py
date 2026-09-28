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

    async def _exec_tmux(self, *args: str, timeout: float = 5.0) -> Tuple[bool, str, str]:
        """Esegue un comando tmux con timeout rigido e gestione sicura delle eccezioni."""
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
            logger.error(f"Timeout comando tmux: tmux {' '.join(args)} (superati {timeout}s)")
            return (False, "", "timeout")
        except Exception as e:
            logger.debug(f"Errore esecuzione tmux {' '.join(args)}: {e}")
            return (False, "", str(e))

    async def check_session_exists(self) -> bool:
        """Verifica se la sessione tmux target è attiva e accessibile."""
        session_name = self.target.split(":")[0] if ":" in self.target else self.target
        success, _, _ = await self._exec_tmux("has-session", "-t", session_name, timeout=3.0)
        return success

    async def send_input(self, text: str, press_enter: bool = True) -> bool:
        """Inietta il testo istantaneamente tramite buffer di copia tmux con timeout garantito."""
        if not await self.check_session_exists():
            logger.warning(f"Sessione tmux '{self.target}' non trovata!")
            return False

        logger.info(f"Invio input istantaneo a tmux [{self.target}]: {text[:50]}...")
        ok_buf, _, _ = await self._exec_tmux("set-buffer", "-b", "agy_input", text)
        if not ok_buf:
            return False

        ok_paste, _, _ = await self._exec_tmux("paste-buffer", "-b", "agy_input", "-t", self.target)
        if not ok_paste:
            return False

        if press_enter:
            ok_enter, _, _ = await self._exec_tmux("send-keys", "-t", self.target, "Enter")
            return ok_enter

        return True

    async def send_raw_key(self, key: str) -> bool:
        """Invia un tasto speciale (es. C-c, Enter, '1', '4') con timeout garantito."""
        if not await self.check_session_exists():
            return False

        success, _, _ = await self._exec_tmux("send-keys", "-t", self.target, key)
        return success

    async def capture_recent_lines(self, lines_count: int = 25) -> List[str]:
        """
        Cattura unicamente le ultime N righe visibili del pannello target.
        Evita di catturare l'intera cronologia e riduce drasticamente l'overhead di CPU e memoria.
        """
        success, raw, _ = await self._exec_tmux(
            "capture-pane", "-t", self.target, "-p", "-S", f"-{lines_count}", timeout=3.0
        )
        if not success:
            return []
        clean = strip_ansi(raw)
        return clean.splitlines()

    def parse_permission_options(self, lines: List[str]) -> Tuple[str, List[Tuple[str, str]]]:
        """
        Analizza le righe del terminale per estrarre il comando per cui viene chiesta autorizzazione.
        Supporta molteplici pattern di prompt per resilienza a variazioni di formato.
        """
        text = "\n".join(lines)
        cmd_requested = "Comando di sistema"

        patterns = [
            r'Requesting permission for:\s*\n\s*(.*?)(?:\n\s*Run this command|\n\s*\[|\n\s*1\.)',
            r'Permission requested?:\s*\n\s*(.*?)(?:\n|$)',
            r'Run this command\?.*?`([^`]+)`',
            r'(?:command|execute):\s*`?([^\n`]+)`?',
        ]

        for pat in patterns:
            m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
            if m and m.group(1).strip():
                cmd_requested = m.group(1).strip()
                break

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
