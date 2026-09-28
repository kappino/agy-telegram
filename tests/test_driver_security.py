"""
Unit tests verifying AgyDriver security rules and safe execution boundaries.
"""

import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from agy_telegram.core.agy_driver import AgyDriver


class TestAgyDriverSecurity(unittest.TestCase):
    def test_default_skip_permissions_is_false(self):
        driver = AgyDriver()
        self.assertFalse(driver.skip_permissions, "Per default skip_permissions deve essere FALSE per sicurezza!")

    @patch("asyncio.create_subprocess_exec")
    def test_execute_prompt_omits_skip_permissions_by_default(self, mock_exec):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Success output", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        driver = AgyDriver(skip_permissions=False)
        import asyncio
        asyncio.run(driver.execute_prompt(prompt="test prompt"))

        self.assertTrue(mock_exec.called)
        called_args = mock_exec.call_args[0]
        self.assertNotIn("--dangerously-skip-permissions", called_args, "Non deve mai essere presente --dangerously-skip-permissions senza autorizzazione esplicita!")

    @patch("asyncio.create_subprocess_exec")
    def test_execute_prompt_includes_skip_permissions_when_explicit(self, mock_exec):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Success output", b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        driver = AgyDriver(skip_permissions=True)
        import asyncio
        asyncio.run(driver.execute_prompt(prompt="test prompt"))

        self.assertTrue(mock_exec.called)
        called_args = mock_exec.call_args[0]
        self.assertIn("--dangerously-skip-permissions", called_args)


if __name__ == "__main__":
    unittest.main()
