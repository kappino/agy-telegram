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
            import stat
            st = sock_p.stat()
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(
                    f"File at {self.socket_path} exists and is not a socket. Refusing to overwrite."
                )
            if hasattr(os, "getuid") and st.st_uid != os.getuid() and os.getuid() != 0:
                raise PermissionError(
                    f"Socket at {self.socket_path} is owned by UID {st.st_uid}, not current user {os.getuid()}."
                )

            # Test if another live process is actively listening
            test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test_sock.settimeout(0.5)
            try:
                test_sock.connect(self.socket_path)
                test_sock.close()
                raise RuntimeError(
                    f"Another active instance is already listening on {self.socket_path}. Refusing to steal socket."
                )
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
                # Stale socket from dead process
                try:
                    sock_p.unlink()
                except OSError as e:
                    raise RuntimeError(f"Failed to remove stale socket at {self.socket_path}: {e}")
            finally:
                test_sock.close()

        # Eliminate race condition on socket permissions by setting umask 0177 (mode 0600)
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self._handle_client,
                path=self.socket_path,
            )
            try:
                os.chmod(self.socket_path, 0o600)
            except OSError as e:
                logger.warning(f"Failed to set chmod 0600 on {self.socket_path}: {e}")
        finally:
            os.umask(old_umask)

        if not hasattr(socket, "SO_PEERCRED"):
            logger.warning(
                "SO_PEERCRED is not available on this platform (e.g. macOS/BSD). Local peer UID validation cannot be enforced."
            )

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

            # Chunked read with rigid 5s timeout up to 64KB
            chunks = []
            total_bytes = 0
            max_bytes = 65536
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    logger.warning(f"Sentinel client payload exceeded {max_bytes} bytes. Aborting.")
                    writer.close()
                    await writer.wait_closed()
                    return
                if b"\n" in chunk:
                    break
                try:
                    json.loads(b"".join(chunks).decode("utf-8"))
                    break
                except Exception:
                    pass

            data = b"".join(chunks)
            if not data:
                return

            payload = json.loads(data.decode("utf-8"))
            logger.info(f"Proactive notification received: {payload.get('title')}")

            if self.notification_callback:
                await self.notification_callback(payload)

            writer.write(b'{"status": "delivered"}\n')
            await writer.drain()
        except asyncio.TimeoutError:
            logger.warning("Sentinel client connection timed out during read.")
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
