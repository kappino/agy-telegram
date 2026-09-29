"""
Proactive Watchdog for Aegis Proxmox & Security Monitoring.
Checks container health, host resources, and security anomalies,
and triggers Sentinel alerts with autonomous investigation prompts.
"""

import asyncio
import logging
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple, Callable, Coroutine, Any

logger = logging.getLogger("agy_telegram.sentinel.watchdog")


class SentinelWatchdog:
    def __init__(
        self,
        alert_callback: Optional[Callable[[dict], Coroutine[Any, Any, None]]] = None,
        check_interval: int = 300,
        cooldown_seconds: int = 1800,
        ignored_vmids: Optional[List[str]] = None,
    ):
        self.alert_callback = alert_callback
        self.check_interval = check_interval
        self.cooldown_seconds = cooldown_seconds
        # 106 is sport-watchdog which is normally stopped
        self.ignored_vmids = set(ignored_vmids or ["106"])
        self.last_alerts: Dict[str, float] = {}
        self.running = False
        self.task: Optional[asyncio.Task] = None

    def should_alert(self, key: str) -> bool:
        now = time.monotonic()
        last = self.last_alerts.get(key, 0.0)
        if now - last < self.cooldown_seconds:
            return False
        self.last_alerts[key] = now
        return True

    async def run_cmd(self, cmd: List[str], timeout: float = 10.0) -> Tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace").strip()
            return proc.returncode == 0, out
        except Exception as e:
            logger.debug(f"Watchdog error running {' '.join(cmd)}: {e}")
            return False, str(e)

    async def check_proxmox_containers(self) -> List[dict]:
        """Checks aegis-ops list for stopped or unhealthy containers."""
        alerts = []
        if not shutil.which("aegis-ops"):
            return alerts

        ok, out = await self.run_cmd(["aegis-ops", "list"])
        if not ok or not out:
            return alerts

        lines = out.splitlines()
        in_vm_section = False
        for line in lines:
            if "--- VM KVM ---" in line:
                in_vm_section = True
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                vmid = parts[0]
                if in_vm_section:
                    name = parts[1] if len(parts) >= 2 else vmid
                    status = parts[2].lower() if len(parts) >= 3 else "unknown"
                else:
                    status = parts[1].lower()
                    if len(parts) == 3:
                        name = parts[2]
                    elif len(parts) >= 4:
                        name = parts[3]
                    else:
                        name = vmid

                if vmid in self.ignored_vmids:
                    continue

                if status == "stopped":
                    alert_key = f"ct_stopped_{vmid}"
                    if self.should_alert(alert_key):
                        alerts.append({
                            "title": f"Proxmox Alert: {name} (ID {vmid}) STOPPED",
                            "message": f"Il container/VM <b>{name}</b> (ID <code>{vmid}</code>) risulta arrestato sul nodo Proxmox.",
                            "level": "warning",
                            "action_prompt": f"Controlla lo stato del container {vmid} ({name}) con aegis-ops e valuta se riavviarlo.",
                        })
        return alerts

    async def check_host_resources(self) -> List[dict]:
        """Checks host memory, disk and swap via aegis-ops status or local system."""
        alerts = []
        if not shutil.which("aegis-ops"):
            return alerts

        ok, out = await self.run_cmd(["aegis-ops", "status"])
        if not ok or not out:
            return alerts

        # Check disk space in output, e.g. /dev/sdc1 1.8T 103G 1.7T 6% /mnt/storage-2tb
        for line in out.splitlines():
            m = re.search(r'(\S+)\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+(\S+)', line)
            if m:
                device = m.group(1)
                use_pct = int(m.group(2))
                mount_pt = m.group(3)
                if use_pct >= 90:
                    alert_key = f"disk_full_{mount_pt}"
                    if self.should_alert(alert_key):
                        alerts.append({
                            "title": f"Spazio Disco Critico: {mount_pt} ({use_pct}%)",
                            "message": f"Il filesystem <code>{device}</code> montato su <code>{mount_pt}</code> ha raggiunto il <b>{use_pct}%</b> di utilizzo.",
                            "level": "alert",
                            "action_prompt": f"Analizza l'utilizzo del disco su {mount_pt} ed elenca i file o log più pesanti.",
                        })

        return alerts

    async def run_single_check(self) -> List[dict]:
        """Runs all checks and returns any generated alerts."""
        alerts = []
        ct_alerts = await self.check_proxmox_containers()
        alerts.extend(ct_alerts)
        res_alerts = await self.check_host_resources()
        alerts.extend(res_alerts)
        return alerts

    async def check_and_notify(self):
        alerts = await self.run_single_check()
        for alert in alerts:
            if self.alert_callback:
                try:
                    await self.alert_callback(alert)
                except Exception as e:
                    logger.error(f"Error dispatching watchdog alert: {e}")

    async def start(self):
        """Starts background periodic watchdog loop."""
        self.running = True
        logger.info(f"SentinelWatchdog started (check interval: {self.check_interval}s)")

        async def _loop():
            # Initial grace period before first periodic check
            await asyncio.sleep(15.0)
            while self.running:
                try:
                    await self.check_and_notify()
                except Exception as ex:
                    logger.error(f"Error in SentinelWatchdog loop: {ex}")
                await asyncio.sleep(self.check_interval)

        self.task = asyncio.create_task(_loop())

    async def stop(self):
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("SentinelWatchdog stopped.")
