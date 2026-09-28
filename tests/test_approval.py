"""
Unit tests for Smart Approval Engine (ApprovalManager).
Verifies evaluation of allowlist/denylist regexes and asynchronous timeout lifecycles.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from agy_telegram.core.approval import ApprovalManager, Decision, PendingApproval


class TestApproval(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auto_patterns = [
            r"^git\s+(status|diff|log|branch|show)",
            r"^(cat|head|tail|grep|find|ls|pwd|which|echo)\b",
            r"^pytest(\s+.*)?$",
            r"^npm\s+test(\s+.*)?$",
        ]
        self.hard_deny_patterns = [
            r"^rm\s+(-rf|-fr|--recursive)\s+/",
            r":\(\)\{.*\}\;:",
            r">\s*/dev/sd[a-z]",
        ]
        self.manager = ApprovalManager(
            auto_patterns=self.auto_patterns,
            hard_deny_patterns=self.hard_deny_patterns,
            timeout_seconds=1,  # Short timeout for testing
            fallback_action="reject",
        )

    def test_evaluate_auto_approve(self):
        """Diagnostic and read-only commands must match AUTO_APPROVE."""
        commands = [
            "git status",
            "git diff HEAD~1",
            "git log -n 5",
            "ls -la /tmp",
            "cat /etc/os-release",
            "pytest tests/ -v",
            "npm test",
            "pwd",
            "echo 'Hello World'",
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                decision = self.manager.evaluate(cmd)
                self.assertEqual(decision, Decision.AUTO_APPROVE, f"Expected AUTO_APPROVE for: {cmd}")

    def test_evaluate_hard_deny(self):
        """Catastrophic destructive commands must trigger immediate HARD_DENY."""
        commands = [
            "rm -rf /",
            "rm -fr /home/user",
            "rm --recursive /var",
            ":(){ :|:& };:",
            "echo test > /dev/sda",
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                decision = self.manager.evaluate(cmd)
                self.assertEqual(decision, Decision.HARD_DENY, f"Expected HARD_DENY for: {cmd}")

    def test_evaluate_manual(self):
        """General commands requiring human oversight must return MANUAL."""
        commands = [
            "python script.py",
            "systemctl restart agy-telegram",
            "git commit -m 'feat: test'",
            "apt-get install -y nginx",
            "docker run -d redis",
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                decision = self.manager.evaluate(cmd)
                self.assertEqual(decision, Decision.MANUAL, f"Expected MANUAL for: {cmd}")

    async def test_timeout_expiration_triggers_callback(self):
        """Pending approval must invoke timeout callback if user takes no action."""
        on_timeout_mock = AsyncMock()

        self.manager.register_pending(
            turn_id="turn_timeout_1",
            chat_id=100,
            message_id=200,
            command="python long_running.py",
            on_timeout=on_timeout_mock,
        )

        self.assertIsNotNone(self.manager.get_pending("turn_timeout_1"))

        # Wait for timeout (timeout_seconds=1)
        await asyncio.sleep(1.2)

        self.assertTrue(on_timeout_mock.called, "on_timeout callback must be called on expiration")
        pending_arg, action_arg = on_timeout_mock.call_args[0]
        self.assertEqual(pending_arg.turn_id, "turn_timeout_1")
        self.assertEqual(action_arg, "reject")
        self.assertIsNone(self.manager.get_pending("turn_timeout_1"))

    async def test_resolve_cancels_timeout_timer(self):
        """Resolving pending approval before expiration must cancel timer and prevent callback."""
        on_timeout_mock = AsyncMock()

        self.manager.register_pending(
            turn_id="turn_resolved_1",
            chat_id=100,
            message_id=201,
            command="python quick.py",
            on_timeout=on_timeout_mock,
        )

        resolved_entry = self.manager.resolve("turn_resolved_1")
        self.assertIsNotNone(resolved_entry)
        self.assertEqual(resolved_entry.turn_id, "turn_resolved_1")
        self.assertIsNone(self.manager.get_pending("turn_resolved_1"))

        # Wait past expiration duration
        await asyncio.sleep(1.2)

        self.assertFalse(on_timeout_mock.called, "on_timeout must NOT be called if resolved before expiration")
