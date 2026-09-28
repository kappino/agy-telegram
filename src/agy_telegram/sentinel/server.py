"""
Sentinel IPC Server allowing any local script or cron job to trigger push notifications.
"""

import asyncio
import json
import logging
import os
import socket
import struct
from pathlib import Path
from typing import Callable, Coroutine, Any

logger = logging.getLogger("agy_telegram.sentinel")

class SentinelServer:
    def __init__(self, socket_path: str = "/tmp/agy-sentinel.sock"):
        self.socket_path = socket_path
        self.server: asyncio.Server | None = None
        self.notification_callback: Callable[[dict], Coroutine[Any, Any, None]] | None = None

    def register_callback(self, cb: Callable[[dict], Coroutine[Any, Any, None]]):
        self.notification_callback = cb

    async def start(self):
        """Avvia l'ascolto sul socket Unix locale con permessi restrittivi."""
        sock_p = Path(self.socket_path)
        sock_p.parent.mkdir(parents=True, exist_ok=True)

        if sock_p.exists():
            try:
                sock_p.unlink()
            except OSError:
                pass

        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        # Permessi restrittivi: solo il proprietario del processo (0600)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError as e:
            logger.warning(f"Impossibile impostare chmod 0600 su {self.socket_path}: {e}")

        logger.info(f"Sentinel IPC Server attivo su: {self.socket_path} (mode=0600)")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Validazione credenziali peer (SO_PEERCRED su Linux) per prevenire spoofing locale
            sock = writer.get_extra_info("socket")
            if sock and hasattr(socket, "SO_PEERCRED"):
                try:
                    creds = sock.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        struct.calcsize("iII"),
                    )
                    pid, uid, gid = struct.unpack("iII", creds)
                    expected_uid = os.getuid()
                    # Consenti solo lo stesso UID o root (se il server non è già root)
                    if uid != expected_uid and uid != 0:
                        logger.warning(
                            f"Rifiutata connessione IPC Sentinel non autorizzata: client UID={uid}, atteso={expected_uid}"
                        )
                        writer.close()
                        await writer.wait_closed()
                        return
                except Exception as ex:
                    logger.warning(f"Impossibile verificare SO_PEERCRED sul client: {ex}")

            data = await reader.read(4096)
            if not data:
                return

            payload = json.loads(data.decode("utf-8"))
            logger.info(f"Notifica proattiva ricevuta: {payload.get('title')}")

            if self.notification_callback:
                await self.notification_callback(payload)

            writer.write(b'{"status": "delivered"}\n')
            await writer.drain()
        except Exception as e:
            logger.error(f"Errore gestione client Sentinel: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
