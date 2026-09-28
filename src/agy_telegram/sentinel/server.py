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
        """Starts listening on local Unix domain socket with restricted permissions."""
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
        # Enforce restricted permissions (owner-only 0600)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError as e:
            logger.warning(f"Failed to set chmod 0600 on {self.socket_path}: {e}")

        logger.info(f"Sentinel IPC Server active at {self.socket_path} (mode=0600)")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Validate peer credentials (SO_PEERCRED on Linux) to prevent local spoofing
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
                    # Allow only same UID or root
                    if uid != expected_uid and uid != 0:
                        logger.warning(
                            f"Rejected unauthorized Sentinel IPC connection: client UID={uid}, expected={expected_uid}"
                        )
                        writer.close()
                        await writer.wait_closed()
                        return
                except Exception as ex:
                    logger.warning(f"Unable to verify SO_PEERCRED on client: {ex}")

            data = await reader.read(4096)
            if not data:
                return

            payload = json.loads(data.decode("utf-8"))
            logger.info(f"Proactive notification received: {payload.get('title')}")

            if self.notification_callback:
                await self.notification_callback(payload)

            writer.write(b'{"status": "delivered"}\n')
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling Sentinel client: {e}")
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
