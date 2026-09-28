"""
Unit tests for TmuxMirror option parsing and reliable key extraction.
"""

import unittest
from agy_telegram.core.tmux_mirror import TmuxMirror, strip_ansi


class TestTmuxMirror(unittest.TestCase):
    def setUp(self):
        self.mirror = TmuxMirror(target_session="dummy:0.0")

    def test_strip_ansi(self):
        ansi_text = "\x1B[31mHello \x1B[1mWorld\x1B[0m"
        self.assertEqual(strip_ansi(ansi_text), "Hello World")

    def test_parse_permission_options(self):
        sample_lines = [
            "Requesting permission for:",
            "  systemctl restart nginx",
            "Run this command?",
        ]
        cmd, options = self.mirror.parse_permission_options(sample_lines)
        self.assertEqual(cmd, "systemctl restart nginx")
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0][0], "1")
        self.assertIn("Approve", options[0][1])
        self.assertEqual(options[1][0], "4")
        self.assertIn("Reject", options[1][1])

    def test_parse_permission_options_fail_closed(self):
        # Prompt without a recognizable command line
        sample_lines = [
            "Some ambiguous prompt:",
            "Run this command?",
        ]
        cmd, options = self.mirror.parse_permission_options(sample_lines)
        # Fail-closed: only Reject is available, Approve is disabled
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0][0], "4")
        self.assertEqual(options[0][1], "Reject")

    def test_parse_permission_options_backward_search(self):
        # Buffer has old stale prompt at the top and a new prompt at the bottom
        sample_lines = [
            "Requesting permission for:",
            "  old_command_stale",
            "Run this command?",
            "Some intermediate output 1",
            "Some intermediate output 2",
            "Requesting permission for:",
            "  new_command_fresh",
            "Run this command?",
        ]
        cmd, options = self.mirror.parse_permission_options(sample_lines)
        self.assertEqual(cmd, "new_command_fresh")
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0][0], "1")
        self.assertEqual(options[1][0], "4")


if __name__ == "__main__":
    unittest.main()

