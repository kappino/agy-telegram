"""
Unit tests for In-Memory Diff Preview (diff_preview.py).
Verifies synthetic diff calculation without altering on-disk files,
and Telegram preview formatting (inline vs .patch document).
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from agy_telegram.core.diff_preview import (
    generate_diff_for_tool,
    format_diff_for_telegram,
    extract_latest_tool_call_from_transcript,
)


class TestDiffPreview(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_replace_file_content_diff(self):
        """Generates unified diff for single contiguous block replacement."""
        test_file = self.workspace / "sample.py"
        test_file.write_text("def hello():\n    print('old')\n", encoding="utf-8")

        args = {
            "TargetFile": str(test_file),
            "TargetContent": "    print('old')",
            "ReplacementContent": "    print('new')",
        }

        diff = generate_diff_for_tool("replace_file_content", args, workspace=self.workspace)
        self.assertIsNotNone(diff)
        self.assertIn("-    print('old')", diff)
        self.assertIn("+    print('new')", diff)

        # Ensure original file was NOT modified
        self.assertEqual(test_file.read_text(encoding="utf-8"), "def hello():\n    print('old')\n")

    def test_write_to_file_diff_new_file(self):
        """Generates unified diff for newly created file."""
        target_path = self.workspace / "new_module.py"
        args = {
            "TargetFile": str(target_path),
            "CodeContent": "def add(a, b):\n    return a + b\n",
            "Overwrite": True,
        }

        diff = generate_diff_for_tool("write_to_file", args, workspace=self.workspace)
        self.assertIsNotNone(diff)
        self.assertIn("/dev/null", diff)
        self.assertIn("+def add(a, b):", diff)

        # File must not exist on disk yet
        self.assertFalse(target_path.exists())

    def test_write_to_file_diff_append(self):
        """Generates unified diff for appending to existing file."""
        target_path = self.workspace / "log.txt"
        target_path.write_text("Line 1\n", encoding="utf-8")

        args = {
            "TargetFile": str(target_path),
            "CodeContent": "Line 2\n",
            "Append": True,
        }

        diff = generate_diff_for_tool("write_to_file", args, workspace=self.workspace)
        self.assertIsNotNone(diff)
        self.assertIn("+Line 2", diff)
        self.assertEqual(target_path.read_text(encoding="utf-8"), "Line 1\n")

    def test_multi_replace_file_content_diff(self):
        """Generates unified diff for multiple replacement chunks."""
        test_file = self.workspace / "config.py"
        test_file.write_text("DEBUG = False\nPORT = 8080\n", encoding="utf-8")

        args = {
            "TargetFile": str(test_file),
            "ReplacementChunks": [
                {"TargetContent": "DEBUG = False", "ReplacementContent": "DEBUG = True"},
                {"TargetContent": "PORT = 8080", "ReplacementContent": "PORT = 9000"},
            ],
        }

        diff = generate_diff_for_tool("multi_replace_file_content", args, workspace=self.workspace)
        self.assertIsNotNone(diff)
        self.assertIn("-DEBUG = False", diff)
        self.assertIn("+DEBUG = True", diff)
        self.assertIn("-PORT = 8080", diff)
        self.assertIn("+PORT = 9000", diff)

    def test_format_diff_inline_under_threshold(self):
        """Diff under 3000 chars must be formatted as markdown code block."""
        diff_text = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
        inline_md, patch_file = format_diff_for_telegram(diff_text)
        self.assertIsNotNone(inline_md)
        self.assertIsNone(patch_file)
        self.assertTrue(inline_md.startswith("```diff\n"))
        self.assertIn("+new", inline_md)

    def test_format_diff_patch_over_threshold(self):
        """Diff over 3000 chars must be packaged as an in-memory BytesIO .patch document."""
        large_diff = "--- a/big.py\n+++ b/big.py\n" + ("+addition line\n" * 250)
        self.assertGreater(len(large_diff), 3000)

        inline_md, patch_file = format_diff_for_telegram(large_diff, filename="big_changes")
        self.assertIsNone(inline_md)
        self.assertIsNotNone(patch_file)
        self.assertTrue(patch_file.name.endswith(".patch"))
        content = patch_file.read().decode("utf-8")
        self.assertEqual(content, large_diff)

    def test_extract_latest_tool_call_from_transcript(self):
        """Extracts the latest tool call from a synthetic transcript JSONL."""
        transcript_file = self.workspace / "transcript.jsonl"
        lines = [
            '{"type": "USER_INPUT", "content": "Update the config"}',
            '{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "replace_file_content", "args": {"TargetFile": "app.py"}}]}',
        ]
        transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = extract_latest_tool_call_from_transcript(transcript_file)
        self.assertIsNotNone(result)
        name, args = result
        self.assertEqual(name, "replace_file_content")
        self.assertEqual(args.get("TargetFile"), "app.py")
