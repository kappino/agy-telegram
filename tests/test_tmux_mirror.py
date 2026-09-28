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
        self.assertIn("Approva", options[0][1])
        self.assertEqual(options[1][0], "4")
        self.assertIn("Rifiuta", options[1][1])


if __name__ == "__main__":
    unittest.main()
