"""
Unit tests for SentinelWatchdog.
Verifies Proxmox container parsing, resource parsing, alert deduplication/cooldown,
and watchdog background task lifecycle.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agy_telegram.sentinel.watchdog import SentinelWatchdog


class TestWatchdog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_callback = AsyncMock()
        self.watchdog = SentinelWatchdog(
            alert_callback=self.mock_callback,
            check_interval=60,
            cooldown_seconds=1800,
            ignored_vmids=["106"],
        )

    def test_should_alert_and_cooldown(self):
        """Verifies deduplication cooldown logic."""
        key = "test_alert_key"
        self.assertTrue(self.watchdog.should_alert(key))
        # Immediate subsequent alert with the same key should be suppressed
        self.assertFalse(self.watchdog.should_alert(key))

        # Different key should alert
        self.assertTrue(self.watchdog.should_alert("another_key"))

        # Advance timestamp past cooldown
        self.watchdog.last_alerts[key] = 0.0
        self.assertTrue(self.watchdog.should_alert(key))

    async def test_check_proxmox_containers(self):
        """Verifies parsing of aegis-ops list output."""
        sample_output = """
VMID       STATUS     LOCK         NAME
100        running                 nextcloud
104        running                 splunk
105        stopped                 docker-swarm
106        stopped                 sport-watchdog
107        running                 aegis-agent
--- VM KVM ---
VMID       NAME                 STATUS     MEM(MB)    BOOTDISK(GB)
200        home-assistant       running    4096       32.00
201        win-desktop          stopped    8192       100.00
"""
        with patch.object(self.watchdog, "run_cmd", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (True, sample_output)
            with patch("shutil.which", return_value="/usr/local/bin/aegis-ops"):
                alerts = await self.watchdog.check_proxmox_containers()

        # 105 stopped -> alert
        # 106 stopped -> ignored
        # 201 stopped VM -> alert
        self.assertEqual(len(alerts), 2)

        # Check container 105 alert
        ct_alert = next((a for a in alerts if "105" in a["title"]), None)
        self.assertIsNotNone(ct_alert)
        self.assertIn("docker-swarm", ct_alert["title"])
        self.assertIn("105", ct_alert["action_prompt"])

        # Check VM 201 alert
        vm_alert = next((a for a in alerts if "201" in a["title"]), None)
        self.assertIsNotNone(vm_alert)
        self.assertIn("win-desktop", vm_alert["title"])
        self.assertIn("201", vm_alert["action_prompt"])

    async def test_check_host_resources_high_disk(self):
        """Verifies threshold triggering when disk is >= 90% full."""
        sample_output = """
=== Storage & Disks ===
Filesystem      Size  Used Avail Use% Mounted on
udev            3.9G     0  3.9G   0% /dev
/dev/sdc1       1.8T  1.7T   50G  92% /
/dev/sda1       500G  200G  300G  40% /mnt/data
"""
        with patch.object(self.watchdog, "run_cmd", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (True, sample_output)
            with patch("shutil.which", return_value="/usr/local/bin/aegis-ops"):
                alerts = await self.watchdog.check_host_resources()

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["level"], "alert")
        self.assertIn("92%", alert["title"])
        self.assertIn("/dev/sdc1", alert["title"] + alert["message"])
        self.assertIn("action_prompt", alert)

    async def test_check_host_resources_normal(self):
        """Verifies no alerts when disk usage is normal."""
        sample_output = """
=== Storage & Disks ===
Filesystem      Size  Used Avail Use% Mounted on
/dev/sdc1       1.8T  103G  1.7T   6% /
"""
        with patch.object(self.watchdog, "run_cmd", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (True, sample_output)
            with patch("shutil.which", return_value="/usr/local/bin/aegis-ops"):
                alerts = await self.watchdog.check_host_resources()

        self.assertEqual(len(alerts), 0)

    async def test_run_single_check_and_notify(self):
        """Verifies check_and_notify dispatches alerts through callback."""
        fake_alert = {
            "title": "Alert 1",
            "message": "Detail 1",
            "level": "warning",
            "action_prompt": "Investigate now",
        }
        with patch.object(self.watchdog, "run_single_check", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = [fake_alert]
            await self.watchdog.check_and_notify()

        self.mock_callback.assert_awaited_once_with(fake_alert)

    async def test_lifecycle_start_stop(self):
        """Verifies background watchdog task starts and cleanly stops."""
        await self.watchdog.start()
        self.assertTrue(self.watchdog.running)
        self.assertIsNotNone(self.watchdog.task)
        self.assertFalse(self.watchdog.task.done())

        await self.watchdog.stop()
        self.assertFalse(self.watchdog.running)
        self.assertTrue(self.watchdog.task.done())
