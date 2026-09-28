"""
Sentinel IPC Server allowing any local script or cron job to trigger push notifications.
"""

import asyncio
import json
import logging
import os
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
        """Avvia l'ascolto sul socket Unix locale."""
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        # Permessi di lettura/scrittura sul socket
        os.chmod(self.socket_path, 0o666)
        logger.info(f"Sentinel IPC Server attivo sul socket: {self.socket_path}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
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
            await writer.wait_closed()

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
