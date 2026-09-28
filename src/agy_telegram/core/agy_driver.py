"""
Driver interfacing asynchronously with the agy binary.
Supports streaming, timeout handling, and session resumption.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, AsyncGenerator, Tuple

logger = logging.getLogger("agy_telegram.driver")

class AgyDriver:
    def __init__(
        self,
        executable: str = "agy",
        default_workspace: str = ".",
        default_model: str = "gemini-3.8-flash",
        default_effort: str = "high",
        timeout: int = 600,
        skip_permissions: bool = False,
    ):
        resolved_exe = shutil.which(executable)
        if not resolved_exe:
            home = Path.home()
            candidates = [
                str(home / ".local" / "bin" / "agy"),
                str(home / ".gemini" / "antigravity-cli" / "bin" / "agy"),
                "/usr/local/bin/agy",
                "/usr/bin/agy",
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    resolved_exe = candidate
                    break
        self.executable = resolved_exe or executable
        self.default_workspace = default_workspace
        self.default_model = default_model
        self.default_effort = default_effort
        self.timeout = timeout
        self.skip_permissions = skip_permissions
        self.current_process: Optional[asyncio.subprocess.Process] = None

    def abort_current_task(self) -> bool:
        """Interrompe il processo corrente inviando SIGINT/SIGKILL."""
        if self.current_process and self.current_process.returncode is None:
            try:
                self.current_process.terminate()
                logger.info("Processo agy interrotto su richiesta utente.")
                return True
            except Exception as e:
                logger.error(f"Errore durante l'interruzione di agy: {e}")
                try:
                    self.current_process.kill()
                    return True
                except Exception:
                    pass
        return False

    async def execute_prompt(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """Esegue un prompt verso agy e restituisce returncode, stdout e stderr."""
        ws = workspace or self.default_workspace
        mdl = model or self.default_model

        cmd = [self.executable]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        else:
            # Continua la più recente
            cmd.append("-c")

        if mdl:
            cmd.extend(["--model", mdl])
        if self.default_effort:
            cmd.extend(["--effort", self.default_effort])

        cmd.extend(["-p", prompt])

        if self.skip_permissions:
            logger.warning("ATTENZIONE: --dangerously-skip-permissions e' attivo su richiesta esplicita di configurazione!")
            cmd.append("--dangerously-skip-permissions")

        home_dir = str(Path.home())
        current_path = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        antigravity_bin = str(Path.home() / ".gemini" / "antigravity-cli" / "bin")
        local_bin = str(Path.home() / ".local" / "bin")
        clean_path = f"{antigravity_bin}:{local_bin}:{current_path}"

        clean_env = {
            "PATH": clean_path,
            "HOME": home_dir,
            "USER": os.environ.get("USER", Path.home().name or "user"),
            "TERM": "xterm-256color",
        }
        for k in ["ANTIGRAVITY_HOME", "GEMINI_CLI_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
            if k in os.environ:
                clean_env[k] = os.environ[k]


        logger.info(f"Esecuzione agy in '{ws}' con modello '{mdl}'...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ws,
            env=clean_env,
        )
        self.current_process = proc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("Timeout esecuzione agy superato!")
            return (-1, "", "Timeout operazione (superati 600 secondi)")
        finally:
            self.current_process = None
