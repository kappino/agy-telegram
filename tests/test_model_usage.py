"""
Unit tests for model switching and usage computation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agy_telegram.core.settings_manager import (
    AVAILABLE_MODELS,
    AVAILABLE_MODES,
    get_current_model,
    set_current_model,
    get_current_mode,
    set_current_mode,
)
from agy_telegram.core.usage import compute_session_usage, format_usage_html


class TestModelUsage(unittest.TestCase):
    def test_available_models_list(self):
        self.assertGreaterEqual(len(AVAILABLE_MODELS), 5)
        names = [display for _, display in AVAILABLE_MODELS]
        self.assertTrue(any("3.8 Flash" in n for n in names))
        self.assertTrue(any("Claude" in n for n in names))

    def test_available_modes_list(self):
        self.assertEqual(len(AVAILABLE_MODES), 3)
        slugs = [slug for slug, _, _ in AVAILABLE_MODES]
        self.assertIn("accept-edits", slugs)
        self.assertIn("default", slugs)
        self.assertIn("plan", slugs)

    def test_compute_session_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript_p = Path(tmpdir) / "transcript.jsonl"
            lines = [
                json.dumps({"type": "USER_INPUT", "content": "Ciao"}),
                json.dumps({"type": "PLANNER_RESPONSE", "thinking": "Penso...", "tool_calls": [{"name": "cmd", "args": {}}]}),
                json.dumps({"type": "PLANNER_RESPONSE", "content": "Risposta dell'agente qui."}),
            ]
            transcript_p.write_text("\n".join(lines) + "\n", encoding="utf-8")

            stats = compute_session_usage(transcript_p, "Gemini 3.8 Flash (High)")
            self.assertEqual(stats["model"], "Gemini 3.8 Flash (High)")
            self.assertEqual(stats["steps"], 3)
            self.assertEqual(stats["tool_calls"], 1)
            self.assertGreater(stats["est_tokens_used"], 0)
            self.assertGreater(stats["tokens_remaining"], 0)
            self.assertEqual(stats["max_context"], 1_000_000)
            self.assertIn("░", stats["bar"])

            msg = format_usage_html(stats, "test-conv-id", "accept-edits")
            self.assertIn("Gemini 3.8 Flash (High)", msg)
            self.assertIn("accept-edits", msg)
            self.assertIn("test-conv-id", msg)


if __name__ == "__main__":
    unittest.main()
